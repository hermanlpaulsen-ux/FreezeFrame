#!/usr/bin/env python3

import re
import subprocess
import sys
import tempfile
import threading
import logging
from pathlib import Path

from PySide6.QtCore import QEvent, QSettings, QThread, Qt, Signal, QTimer
from PySide6.QtGui import QCloseEvent, QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSlider,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.models import FrameCountResult, VIDEO_EXTENSIONS
from core.resources import resource_path, spin_icon_paths
from ffmpeg.service import (
    find_ffmpeg,
    find_ffprobe,
    parse_fps,
    probe_duration_seconds,
    probe_stream_field,
    resolve_frame_count,
    source_supports_16_bit,
    source_timing,
)
from platform_utils import open_in_file_manager
from ui.design_tokens import Theme, UI, current_theme, get_palette, set_theme


logger = logging.getLogger("freezeframe")

SHADOW_MARGIN = 22
TITLE_BAR_HEIGHT = 46


class _PreviewLabel(QLabel):
    """Preview surface that keeps its source image fitted to whatever space it is given."""

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._source_pixmap: QPixmap | None = None

    def set_source_pixmap(self, pixmap: "QPixmap | None") -> None:
        self._source_pixmap = pixmap if pixmap is not None and not pixmap.isNull() else None
        self._rescale()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale()

    def _rescale(self) -> None:
        if self._source_pixmap is None:
            return
        target = self.contentsRect().size()
        if target.width() <= 0 or target.height() <= 0:
            return
        scaled = self._source_pixmap.scaled(
            target,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        super().setPixmap(scaled)


class _TitleBar(QWidget):
    """Custom draggable title bar used in place of the native OS chrome."""

    def __init__(self, owner_window: "FreezeFrameWindow") -> None:
        super().__init__()
        self._owner_window = owner_window
        self.setMouseTracking(True)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._owner_window.windowHandle()
            if handle is not None:
                handle.startSystemMove()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner_window._toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class FrameExportWorker(QThread):
    progress_updated = Signal(int, int, int)
    finished_with_result = Signal(int, int, str, list, list, bool)

    def __init__(
        self,
        ffmpeg: str,
        ffprobe: str,
        files: list[Path],
        output_dir: Path,
        selected_formats: list[tuple[str, str]],
        quality_preset: str,
        tiff_bit_depth: str,
        quality_level: int | None = None,
        resize_height: int | None = None,
        frame_number: int = 1,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.files = files
        self.output_dir = output_dir
        self.selected_formats = selected_formats
        self.quality_preset = quality_preset
        self.tiff_bit_depth = tiff_bit_depth
        self.quality_level = quality_level
        self.resize_height = resize_height if resize_height and resize_height > 0 else None
        self.frame_number = max(1, frame_number)
        self._stop_requested = False
        self._active_process: subprocess.Popen | None = None
        self._timing_cache: dict[str, tuple[float, bool]] = {}

    def request_stop(self) -> None:
        self._stop_requested = True
        if self._active_process and self._active_process.poll() is None:
            try:
                self._active_process.terminate()
            except Exception:
                pass

    def _source_supports_16_bit(self, source: Path) -> bool:
        return source_supports_16_bit(self.ffprobe, source)

    def _probe_frame_count(self, source: Path) -> int:
        return self._resolve_frame_count(source).frame_count

    def _probe_stream_field(self, source: Path, field: str, extra_args: list[str] | None = None) -> str:
        return probe_stream_field(self.ffprobe, source, field, extra_args)

    def _resolve_frame_count(self, source: Path) -> FrameCountResult:
        return resolve_frame_count(self.ffprobe, source)

    def _probe_duration_seconds(self, source: Path) -> float:
        return probe_duration_seconds(self.ffprobe, source)

    def _probe_fps(self, source: Path) -> float:
        fps_raw = self._probe_stream_field(source, "r_frame_rate")
        return parse_fps(fps_raw)

    def _parse_fps(self, value: str) -> float:
        return parse_fps(value)

    def _source_timing(self, source: Path) -> tuple[float, bool]:
        key = str(source.resolve())
        if key in self._timing_cache:
            return self._timing_cache[key]
        fps, is_vfr = source_timing(self.ffprobe, source)
        self._timing_cache[key] = (fps, is_vfr)
        return self._timing_cache[key]

    def _effective_quality_level(self) -> int:
        if self.quality_level is not None:
            return max(1, min(12, int(self.quality_level)))
        if self.quality_preset == "High":
            return 12
        if self.quality_preset == "Balanced":
            return 7
        return 3

    def _build_export_tail(self, ext: str, target: Path) -> list[str]:
        tail: list[str] = []
        quality_level = self._effective_quality_level()
        if ext == "jpg":
            # MJPEG: lower q is better quality and bigger files.
            jpeg_q = int(round(31 - ((quality_level - 1) * (29 / 11))))
            jpeg_q = max(2, min(31, jpeg_q))
            tail.extend(["-q:v", str(jpeg_q)])
        elif ext == "png":
            # PNG compression is lossless; this tunes output size/time.
            png_compression = int(round(9 - ((quality_level - 1) * (8 / 11))))
            png_compression = max(1, min(9, png_compression))
            tail.extend(["-compression_level", str(png_compression)])
        elif ext == "tiff":
            tiff_compression_map = {"High": "lzw", "Balanced": "deflate", "Small": "zlib"}
            tail.extend(["-compression_algo", tiff_compression_map.get(self.quality_preset, "deflate")])
        tail.append(str(target))
        return tail

    def _build_export_commands(self, source: Path, target: Path, ext: str) -> list[list[str]]:
        supports_16_bit = self._source_supports_16_bit(source)
        request_16_bit = self.tiff_bit_depth.startswith("16-bit")
        if ext == "tiff":
            use_tiff_16_bit = request_16_bit and supports_16_bit
            output_format = "rgb48le" if use_tiff_16_bit else "rgb24"
        elif ext == "png":
            output_format = "rgb48le" if (request_16_bit and supports_16_bit) else "rgb24"
        else:
            output_format = "yuvj420p"

        frame_idx_zero_based = max(0, self.frame_number - 1)
        fps, is_vfr = self._source_timing(source)
        tail = self._build_export_tail(ext, target)

        scale_expr = f",scale=-2:{self.resize_height}:flags=lanczos" if self.resize_height else ""
        exact_cmd = [
            self.ffmpeg,
            "-y",
            "-hwaccel",
            "none",
            "-i",
            str(source),
            "-vf",
            f"select=eq(n\\,{frame_idx_zero_based}){scale_expr},format={output_format}",
            "-vframes",
            "1",
        ]
        exact_cmd.extend(tail)

        cmds: list[list[str]] = []
        if fps > 0 and not is_vfr:
            target_seconds = max(0.0, frame_idx_zero_based / fps)
            preroll_seconds = 2.0
            seek_seconds = max(0.0, target_seconds - preroll_seconds)
            local_frame = max(0, int(round((target_seconds - seek_seconds) * fps)))
            cfr_cmd = [
                self.ffmpeg,
                "-y",
                "-hwaccel",
                "none",
                "-ss",
                f"{seek_seconds:.6f}",
                "-i",
                str(source),
                "-vf",
                f"select=eq(n\\,{local_frame}){scale_expr},format={output_format}",
                "-vframes",
                "1",
            ]
            cfr_cmd.extend(tail)
            cmds.append(cfr_cmd)
        if fps > 0:
            target_seconds = max(0.0, frame_idx_zero_based / fps)
            ts_cmd = [
                self.ffmpeg,
                "-y",
                "-hwaccel",
                "none",
                "-ss",
                f"{target_seconds:.6f}",
                "-i",
                str(source),
                "-vframes",
                "1",
                "-vf",
                f"{'scale=-2:' + str(self.resize_height) + ':flags=lanczos,' if self.resize_height else ''}format={output_format}",
            ]
            ts_cmd.extend(tail)
            cmds.append(ts_cmd)
        cmds.append(exact_cmd)
        return cmds

    def _run_cmd(self, cmd: list[str]) -> int:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._active_process = proc
        rc = proc.wait()
        self._active_process = None
        return rc

    def _output_variant_suffix(self) -> str:
        quality_tag = f"q{self._effective_quality_level():02d}"
        resolution_tag = f"h{self.resize_height}" if self.resize_height else "orig"
        bit_depth_raw = self.tiff_bit_depth.strip().split("-", 1)[0]
        bit_depth_digits = "".join(ch for ch in bit_depth_raw if ch.isdigit()) or "8"
        bit_depth_tag = f"bd{bit_depth_digits}"
        return f"{quality_tag}_{resolution_tag}_{bit_depth_tag}"

    def run(self) -> None:
        total = len(self.files) * len(self.selected_formats)
        completed = 0
        failed: list[str] = []
        skipped: list[str] = []

        for file in self.files:
            if self._stop_requested:
                break
            if self.frame_number > 1:
                frame_result = self._resolve_frame_count(file)
                file_total_frames = frame_result.frame_count
                if frame_result.confidence in ("exact", "counted") and file_total_frames < self.frame_number:
                    skipped.append(f"{file.name} (frame {self.frame_number} > {file_total_frames})")
                    completed += len(self.selected_formats)
                    pct = int((completed / total) * 100) if total else 0
                    self.progress_updated.emit(completed, total, pct)
                    continue
            for ext, folder_name in self.selected_formats:
                if self._stop_requested:
                    break
                variant_suffix = self._output_variant_suffix()
                target = self.output_dir / folder_name / f"{file.stem}_{variant_suffix}.{ext}"
                rc = 1
                for cmd in self._build_export_commands(file, target, ext):
                    rc = self._run_cmd(cmd)
                    if rc == 0 and target.is_file():
                        break

                if self._stop_requested:
                    break
                if rc != 0:
                    failed.append(f"{file.name} [{folder_name}]")
                completed += 1
                pct = int((completed / total) * 100) if total else 0
                self.progress_updated.emit(completed, total, pct)

        self.finished_with_result.emit(
            total,
            completed,
            str(self.output_dir),
            failed,
            skipped,
            self._stop_requested,
        )


class PreviewWorker(QThread):
    finished_with_preview = Signal(int, int, str)
    failed = Signal(int, int, str)

    def __init__(
        self,
        request_id: int,
        ffmpeg: str,
        source: Path,
        frame_number: int,
        output_path: Path,
        fps: float = 0.0,
        is_variable_frame_rate: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.request_id = request_id
        self.ffmpeg = ffmpeg
        self.source = source
        self.frame_number = max(1, frame_number)
        self.output_path = output_path
        self.fps = fps if fps and fps > 0 else 0.0
        self.is_variable_frame_rate = is_variable_frame_rate
        self._stop_requested = False
        self._active_process: subprocess.Popen | None = None
        self.timeout_seconds = 20

    def request_stop(self) -> None:
        self._stop_requested = True
        if self._active_process and self._active_process.poll() is None:
            try:
                self._active_process.terminate()
            except Exception:
                pass

    def _run_preview_command(self, cmd: list[str]) -> bool:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self._active_process = proc
            rc = proc.wait(timeout=self.timeout_seconds)
            self._active_process = None
        except subprocess.TimeoutExpired:
            self.request_stop()
            return False
        except Exception:
            self._active_process = None
            return False

        if self._stop_requested:
            return False

        return rc == 0 and self.output_path.is_file()

    def _build_exact_global_frame_cmd(self) -> list[str]:
        zero_based_frame = self.frame_number - 1
        return [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-hwaccel",
            "none",
            "-i",
            str(self.source),
            "-an",
            "-vsync",
            "0",
            "-vf",
            f"select=eq(n\\,{zero_based_frame}),scale=640:-2:flags=fast_bilinear,format=yuvj420p",
            "-frames:v",
            "1",
            "-q:v",
            "10",
            str(self.output_path),
        ]

    def _build_direct_timestamp_seek_cmd(self) -> list[str] | None:
        if self.fps <= 0:
            return None
        zero_based_frame = self.frame_number - 1
        target_seconds = max(0.0, zero_based_frame / self.fps)
        return [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-hwaccel",
            "none",
            "-ss",
            f"{target_seconds:.6f}",
            "-i",
            str(self.source),
            "-an",
            "-frames:v",
            "1",
            "-vf",
            "scale=640:-2:flags=fast_bilinear,format=yuvj420p",
            "-q:v",
            "10",
            str(self.output_path),
        ]

    def _build_cfr_seek_preroll_cmd(self) -> list[str] | None:
        if self.fps <= 0 or self.is_variable_frame_rate:
            return None
        zero_based_frame = self.frame_number - 1
        target_seconds = max(0.0, zero_based_frame / self.fps)
        preroll_seconds = 2.0
        seek_seconds = max(0.0, target_seconds - preroll_seconds)
        local_frame = max(0, int(round((target_seconds - seek_seconds) * self.fps)))
        return [
            self.ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-hwaccel",
            "none",
            "-ss",
            f"{seek_seconds:.6f}",
            "-i",
            str(self.source),
            "-an",
            "-vsync",
            "0",
            "-vf",
            f"select=eq(n\\,{local_frame}),scale=640:-2:flags=fast_bilinear,format=yuvj420p",
            "-frames:v",
            "1",
            "-q:v",
            "10",
            str(self.output_path),
        ]

    def run(self) -> None:
        strategies: list[tuple[str, list[str]]] = []
        cfr_preroll = self._build_cfr_seek_preroll_cmd()
        if cfr_preroll is not None:
            strategies.append(("cfr_seek_preroll", cfr_preroll))
        direct_seek = self._build_direct_timestamp_seek_cmd()
        if direct_seek is not None:
            strategies.append(("direct_timestamp_seek", direct_seek))
        strategies.append(("exact_global_frame", self._build_exact_global_frame_cmd()))

        for strategy_name, cmd in strategies:
            if self._stop_requested:
                return
            try:
                if self.output_path.is_file():
                    self.output_path.unlink()
            except Exception:
                pass
            logger.debug(
                "Preview strategy attempted: %s frame=%s fps=%.3f vfr=%s",
                strategy_name,
                self.frame_number,
                self.fps,
                self.is_variable_frame_rate,
            )
            if self._run_preview_command(cmd):
                logger.debug("Preview strategy succeeded: %s frame=%s", strategy_name, self.frame_number)
                self.finished_with_preview.emit(self.request_id, self.frame_number, str(self.output_path))
                return
            logger.debug("Preview strategy failed: %s frame=%s", strategy_name, self.frame_number)

        if not self._stop_requested:
            self.failed.emit(self.request_id, self.frame_number, "Preview unavailable after fast seek and exact fallback")


class FreezeFrameWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._settings = QSettings()
        saved_theme = self._settings.value("appearance/theme", Theme.LIGHT)
        set_theme(saved_theme)
        self.setWindowTitle("FreezeFrame")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Window
            | Qt.WindowType.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self._resize_edges = None
        self._resize_start_geo = None
        self._resize_start_pos = None
        self.resize(1180, 760)
        app_icon_path = resource_path("assets/FreezeFrame_icon_1024.png")
        if app_icon_path.is_file():
            self.setWindowIcon(QIcon(str(app_icon_path)))

        self.ffmpeg = self._find_ffmpeg()
        self.ffprobe = self._find_ffprobe()
        self.worker: FrameExportWorker | None = None
        self.single_worker: FrameExportWorker | None = None
        self.is_processing = False
        self.is_single_processing = False
        self.output_manually_set = False
        self.last_output_dir = ""
        self.preview_request_id = 0
        self.preview_worker: PreviewWorker | None = None
        self.preview_cache: dict[tuple[str, int], str] = {}
        self.preview_cache_order: list[tuple[str, int]] = []
        self.max_preview_cache_items = 80
        self.is_scrubbing = False
        self.current_frame_result: FrameCountResult | None = None
        self.current_preview_aspect = 16 / 9

        self._build_ui()
        self._apply_style()
        self._update_preview_viewport_size()
        content_min = self.centralWidget().minimumSizeHint()
        self.setMinimumSize(content_min.width() + 12, content_min.height() + 12)

    def _parse_fps(self, value: str) -> float:
        return parse_fps(value)

    def _probe_stream_fields(self, source: Path, fields: str) -> list[str]:
        ffprobe = self.ffprobe
        if not Path(ffprobe).is_file():
            return []
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                f"stream={fields}",
                "-of",
                "default=nw=1:nk=1",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return []
        return [line.strip() for line in probe.stdout.splitlines() if line.strip()]

    def _probe_stream_field(self, source: Path, field: str, extra_args: list[str] | None = None) -> str:
        return probe_stream_field(self.ffprobe, source, field, extra_args)

    def _probe_duration_seconds(self, source: Path) -> float:
        return probe_duration_seconds(self.ffprobe, source)

    def _resolve_frame_count(self, source: Path) -> FrameCountResult:
        result = resolve_frame_count(self.ffprobe, source)
        logger.debug("Frame count resolved: %s", result)
        return result

    def _find_ffmpeg(self) -> str:
        return find_ffmpeg()

    def _find_ffprobe(self) -> str:
        return find_ffprobe(self.ffmpeg)

    def _build_ui(self) -> None:
        shadow_layer = QWidget()
        shadow_layer.setObjectName("ShadowLayer")
        shadow_layer.setMouseTracking(True)
        shadow_layer.installEventFilter(self)
        self.setCentralWidget(shadow_layer)
        self._shadow_layer = shadow_layer

        outer_layout = QVBoxLayout(shadow_layer)
        outer_layout.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN, SHADOW_MARGIN)
        outer_layout.setSpacing(0)
        self._outer_layout = outer_layout

        window_frame = QFrame()
        window_frame.setObjectName("WindowFrame")
        window_frame.setMouseTracking(True)
        outer_layout.addWidget(window_frame)
        self.window_frame = window_frame

        self._shadow_effect = QGraphicsDropShadowEffect(window_frame)
        self._shadow_effect.setBlurRadius(56)
        self._shadow_effect.setOffset(0, 16)
        self._shadow_effect.setColor(QColor(10, 18, 32, 130))
        window_frame.setGraphicsEffect(self._shadow_effect)

        frame_layout = QVBoxLayout(window_frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)

        self._build_title_bar(frame_layout)

        content = QWidget()
        content.setObjectName("AppCanvas")
        frame_layout.addWidget(content, 1)

        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(UI.SPACE_LG, UI.SPACE_MD, UI.SPACE_LG, UI.SPACE_LG)
        content_layout.setSpacing(UI.SPACE_LG)

        left_col = QVBoxLayout()
        left_col.setSpacing(UI.SPACE_MD)
        content_layout.addLayout(left_col, 5)

        right_col = QVBoxLayout()
        right_col.setSpacing(UI.SPACE_MD)
        content_layout.addLayout(right_col, 4)

        self.batch_io_card = self._make_card()
        bio = QVBoxLayout(self.batch_io_card)
        self._configure_card_layout(bio)
        in_row = QHBoxLayout()
        in_row.setSpacing(UI.SPACE_MD)
        self.input_path_label = self._make_path_label("No folder selected")
        in_btn = self._make_secondary_button("Add file")
        in_btn.setFixedWidth(UI.PICKER_BUTTON_WIDTH)
        in_btn.clicked.connect(self.choose_input_file)
        in_folder_btn = self._make_secondary_button("Add folder")
        in_folder_btn.setFixedWidth(UI.PICKER_BUTTON_WIDTH)
        in_folder_btn.clicked.connect(self.choose_input_folder)
        in_row.addWidget(self.input_path_label, 1)
        in_row.addWidget(in_btn)
        in_row.addWidget(in_folder_btn)
        in_container = self._make_subblock("Blue")
        in_block = QVBoxLayout(in_container)
        in_block.setContentsMargins(UI.SPACE_MD, UI.SPACE_XS, UI.SPACE_XS, UI.SPACE_XS)
        in_block.setSpacing(8)
        in_block.addLayout(self._section_header("Input", "IN", "Blue"))
        in_block.addLayout(in_row)
        bio.addWidget(in_container)
        bio.addSpacing(UI.SPACE_SM)

        out_row = QHBoxLayout()
        out_row.setSpacing(UI.SPACE_MD)
        self.output_path_label = self._make_path_label("No folder selected")
        out_btn = self._make_secondary_button("Add")
        out_btn.setFixedWidth(UI.PICKER_BUTTON_WIDTH)
        out_btn.clicked.connect(self.choose_output_folder)
        out_row.addWidget(self.output_path_label, 1)
        out_row.addWidget(out_btn)
        out_container = self._make_subblock("Violet")
        out_block = QVBoxLayout(out_container)
        out_block.setContentsMargins(UI.SPACE_MD, UI.SPACE_XS, UI.SPACE_XS, UI.SPACE_XS)
        out_block.setSpacing(8)
        out_block.addLayout(self._section_header("Output", "OUT", "Violet"))
        out_block.addLayout(out_row)
        bio.addWidget(out_container)
        left_col.addWidget(self.batch_io_card)

        self.format_card = self._make_card(accent="Teal")
        self.format_layout = QVBoxLayout(self.format_card)
        self._configure_card_layout(self.format_layout)

        format_row = QHBoxLayout()
        format_row.setSpacing(UI.SPACE_MD)
        format_row.setContentsMargins(0, UI.SPACE_XS, 0, UI.SPACE_XS)
        self.jpeg_cb = QToolButton()
        self.jpeg_cb.setText("JPEG")
        self.jpeg_cb.setCheckable(True)
        self.jpeg_cb.setChecked(True)
        self.png_cb = QToolButton()
        self.png_cb.setText("PNG")
        self.png_cb.setCheckable(True)
        self.tiff_cb = QToolButton()
        self.tiff_cb.setText("TIFF")
        self.tiff_cb.setCheckable(True)
        for checkbox in (self.jpeg_cb, self.png_cb, self.tiff_cb):
            checkbox.setObjectName("FormatChip")
            checkbox.setMinimumHeight(UI.HEIGHT_COMPACT)
            checkbox.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        format_row.addWidget(self.jpeg_cb)
        format_row.addWidget(self.png_cb)
        format_row.addWidget(self.tiff_cb)
        format_row.addStretch(1)

        self.format_layout.addLayout(self._section_header("Options", "OPT", "Teal"))
        self.format_layout.addLayout(format_row)
        left_col.addWidget(self.format_card)

        self.action_card = self._make_card()
        action_layout = QVBoxLayout(self.action_card)
        self._configure_card_layout(action_layout)

        button_row = QHBoxLayout()
        button_row.setSpacing(UI.SPACE_SM)
        self.action_button = self._make_primary_button("Start")
        self.action_button.clicked.connect(self.start_processing)
        self.stop_button = self._make_primary_button("Stop")
        self.stop_button.clicked.connect(self.stop_processing)
        self.stop_button.hide()
        self.open_output_button = self._make_primary_button("Open output folder")
        self.open_output_button.setObjectName("OutputButton")
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.open_output_button.hide()

        button_row.addWidget(self.action_button)
        button_row.addWidget(self.stop_button)
        button_row.addWidget(self.open_output_button)

        self.status_label = QLabel("Choose input and output folders to begin.")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(UI.HEIGHT_COMPACT)

        progress_header = QHBoxLayout()
        progress_title = QLabel("Progress")
        progress_title.setObjectName("ProgressTitle")
        self.progress_pct = QLabel("0%")
        self.progress_pct.setObjectName("ProgressPct")
        progress_header.addWidget(progress_title)
        progress_header.addStretch(1)
        progress_header.addWidget(self.progress_pct)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)

        action_layout.addLayout(button_row)
        action_layout.addWidget(self.status_label)
        action_layout.addLayout(progress_header)
        action_layout.addWidget(self.progress_bar)
        left_col.addWidget(self.action_card)
        left_col.addStretch(1)

        self.preview_card = self._make_card(accent="Amber")
        pv = QVBoxLayout(self.preview_card)
        self._configure_card_layout(pv)
        pv.addLayout(self._section_header("Preview", "PRE", "Amber"))
        self.batch_preview = _PreviewLabel("Preview not available in batch processing")
        self._configure_preview_label(self.batch_preview)
        pv.addWidget(self.batch_preview, 1)
        right_col.addWidget(self.preview_card, 1)

        self._build_unified_advanced_controls()

    def _section_header(self, title_text: str, badge_text: str, badge_key: str) -> QHBoxLayout:
        badge = QLabel(badge_text)
        badge.setObjectName(f"Badge{badge_key}")
        badge.setFixedSize(32, 32)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel(title_text)
        title.setObjectName("SectionTitle")
        row = QHBoxLayout()
        row.setSpacing(UI.SPACE_MD)
        row.addWidget(badge)
        row.addWidget(title)
        row.addStretch(1)
        return row

    def _make_card(self, accent: str | None = None) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        if accent:
            card.setProperty("accent", accent)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(20, 30, 50, 40))
        card.setGraphicsEffect(shadow)
        return card

    def _make_subblock(self, accent: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("SubBlock")
        frame.setProperty("accent", accent)
        return frame

    def _configure_card_layout(self, layout: QVBoxLayout) -> None:
        layout.setContentsMargins(UI.SPACE_LG, UI.SPACE_MD, UI.SPACE_LG, UI.SPACE_MD)
        layout.setSpacing(UI.SPACE_SM)

    def _make_primary_button(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("PrimaryButton")
        btn.setFixedHeight(UI.HEIGHT_PRIMARY + 8)
        glow = QGraphicsDropShadowEffect(btn)
        glow.setBlurRadius(24)
        glow.setOffset(0, 6)
        glow.setColor(QColor(46, 107, 255, 90))
        btn.setGraphicsEffect(glow)
        return btn

    def _make_secondary_button(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setObjectName("OpenButton")
        btn.setFixedHeight(UI.HEIGHT_CONTROL)
        return btn

    def _make_path_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("PathField")
        label.setFixedHeight(UI.HEIGHT_CONTROL)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    def _configure_preview_label(self, label: QLabel) -> None:
        label.setObjectName("PreviewSurface")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setMinimumWidth(180)
        label.setMinimumHeight(100)
        label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        label.setScaledContents(False)
        label.setWordWrap(False)

    def _update_preview_viewport_size(self) -> None:
        # Preview viewport is capped to the available space inside the preview card.
        container = getattr(self, "preview_card", None)
        if container is not None and container.width() > 0:
            padding = UI.SPACE_XL * 2
            max_w = max(220, container.width() - padding)
            max_h = max(140, container.height() - padding - 40)
        else:
            max_w = max(260, self.width() // 2)
            max_h = max(146, int(max_w * 9 / 16))
        aspect = self.current_preview_aspect if self.current_preview_aspect > 0 else (16 / 9)

        target_w = max_w
        target_h = int(target_w / aspect)
        if target_h > max_h:
            target_h = max_h
            target_w = int(target_h * aspect)

        target_w = max(180, target_w)
        target_h = max(100, target_h)

        for label_name in ("preview_image",):
            label = getattr(self, label_name, None)
            if isinstance(label, QLabel):
                label.setFixedSize(target_w, target_h)

    def _build_unified_advanced_controls(self) -> None:
        box = self.format_layout

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.single_quality = QSpinBox()
        self.single_quality.setRange(1, 12)
        self.single_quality.setValue(12)
        self.single_quality.setFixedWidth(72)
        grid.addWidget(self._label("Quality:", "InlineLabel"), 0, 0)
        grid.addWidget(self.single_quality, 0, 1)
        self.single_bit_depth = QComboBox()
        self.single_bit_depth.addItems(["8-bit", "16-bit", "32-bit"])
        self.single_bit_depth.setFixedWidth(100)
        self.single_bit_depth.setCurrentIndex(0)
        self._disable_unsupported_bit_depth_option(self.single_bit_depth)
        grid.addWidget(self._label("Bit Depth:", "InlineLabel"), 0, 2)
        grid.addWidget(self.single_bit_depth, 0, 3)

        self.single_res_preset = QComboBox()
        self.single_res_preset.addItems(["Original", "2160", "1080", "720"])
        self.single_res_preset.setFixedWidth(100)
        grid.addWidget(self._label("Resolution:", "InlineLabel"), 1, 0)
        grid.addWidget(self.single_res_preset, 1, 1)
        self.frame_index_spin = QSpinBox()
        self.frame_index_spin.setRange(1, 1)
        self.frame_index_spin.setValue(1)
        self.frame_index_spin.setFixedWidth(90)
        grid.addWidget(self._label("Frame #:", "InlineLabel"), 1, 2)
        grid.addWidget(self.frame_index_spin, 1, 3)
        grid.setColumnStretch(4, 1)
        box.addLayout(grid)

        self.frame_count_info = self._label("Frames: unknown", "SectionDesc")
        box.addWidget(self.frame_count_info)

        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setRange(1, 1)
        self.frame_slider.setValue(1)
        self.frame_slider.setSingleStep(1)
        self.frame_slider.setPageStep(10)
        self.frame_slider.valueChanged.connect(self._sync_frame_spin_from_slider)
        self.frame_index_spin.valueChanged.connect(self._sync_frame_slider_from_spin)
        self.frame_slider.sliderPressed.connect(self._on_slider_pressed)
        self.frame_slider.sliderReleased.connect(self._on_slider_released)
        self.frame_index_spin.setEnabled(False)
        self.frame_slider.setEnabled(False)
        box.addWidget(self.frame_slider)
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(120)
        self.preview_timer.timeout.connect(self.generate_preview)

    def _build_title_bar(self, layout: QVBoxLayout) -> None:
        bar = _TitleBar(self)
        bar.setObjectName("TitleBar")
        bar.setFixedHeight(TITLE_BAR_HEIGHT)
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(18, 0, 10, 0)
        bar_layout.setSpacing(10)

        icon = QLabel("")
        icon.setObjectName("TitleBarIcon")
        icon.setFixedSize(22, 22)
        logo_path = resource_path("assets/FreezeFrame_icon_1024.png")
        if logo_path.is_file():
            pixmap = QPixmap(str(logo_path))
            if not pixmap.isNull():
                icon.setPixmap(
                    pixmap.scaled(
                        22,
                        22,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
        if not icon.pixmap():
            icon.setText("❄")

        title_label = QLabel("FreezeFrame")
        title_label.setObjectName("TitleBarText")

        bar_layout.addWidget(icon)
        bar_layout.addWidget(title_label)
        bar_layout.addStretch(1)

        theme_button = QToolButton()
        theme_button.setObjectName("ThemeToggle")
        theme_button.setCheckable(True)
        theme_button.setChecked(current_theme() == Theme.DARK)
        theme_button.setText("☾" if current_theme() == Theme.LIGHT else "☀")
        theme_button.setToolTip(
            "Switch to dark mode" if current_theme() == Theme.LIGHT else "Switch to light mode"
        )
        theme_button.setFixedSize(30, 30)
        theme_button.clicked.connect(self._on_theme_toggle)
        bar_layout.addWidget(theme_button)
        self._theme_toggle_button = theme_button

        min_button = QToolButton()
        min_button.setObjectName("WinBtnMin")
        min_button.setText("–")
        min_button.setToolTip("Minimize")
        min_button.setFixedSize(28, 28)
        min_button.clicked.connect(self.showMinimized)

        self._max_button = QToolButton()
        self._max_button.setObjectName("WinBtnMax")
        self._max_button.setText("▢")
        self._max_button.setToolTip("Maximize")
        self._max_button.setFixedSize(28, 28)
        self._max_button.clicked.connect(self._toggle_maximize)

        close_button = QToolButton()
        close_button.setObjectName("WinBtnClose")
        close_button.setText("×")
        close_button.setToolTip("Close")
        close_button.setFixedSize(28, 28)
        close_button.clicked.connect(self.close)

        for button in (min_button, self._max_button, close_button):
            bar_layout.addWidget(button)

        self._title_bar = bar
        layout.addWidget(bar)

    def _toggle_maximize(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            is_max = self.isMaximized()
            margin = 0 if is_max else SHADOW_MARGIN
            self._outer_layout.setContentsMargins(margin, margin, margin, margin)
            self.window_frame.setProperty("maximized", is_max)
            self.window_frame.style().unpolish(self.window_frame)
            self.window_frame.style().polish(self.window_frame)
            if hasattr(self, "_max_button"):
                self._max_button.setText("❐" if is_max else "▢")
                self._max_button.setToolTip("Restore" if is_max else "Maximize")

    def _resize_edge_at(self, pos) -> "Qt.Edges | None":
        w = self._shadow_layer.width()
        h = self._shadow_layer.height()
        margin = SHADOW_MARGIN
        left = pos.x() <= margin
        right = pos.x() >= w - margin
        top = pos.y() <= margin
        bottom = pos.y() >= h - margin
        if not (left or right or top or bottom):
            return None
        edges = Qt.Edge(0)
        if top:
            edges |= Qt.Edge.TopEdge
        if bottom:
            edges |= Qt.Edge.BottomEdge
        if left:
            edges |= Qt.Edge.LeftEdge
        if right:
            edges |= Qt.Edge.RightEdge
        return edges

    def _cursor_for_edges(self, edges) -> Qt.CursorShape:
        has_top = bool(edges & Qt.Edge.TopEdge)
        has_bottom = bool(edges & Qt.Edge.BottomEdge)
        has_left = bool(edges & Qt.Edge.LeftEdge)
        has_right = bool(edges & Qt.Edge.RightEdge)
        if (has_top and has_left) or (has_bottom and has_right):
            return Qt.CursorShape.SizeFDiagCursor
        if (has_top and has_right) or (has_bottom and has_left):
            return Qt.CursorShape.SizeBDiagCursor
        if has_left or has_right:
            return Qt.CursorShape.SizeHorCursor
        return Qt.CursorShape.SizeVerCursor

    def eventFilter(self, obj, event) -> bool:
        if obj is getattr(self, "_shadow_layer", None) and not self.isMaximized():
            if event.type() == QEvent.Type.MouseMove:
                if self._resize_edges is not None and (event.buttons() & Qt.MouseButton.LeftButton):
                    self._perform_manual_resize(event.globalPosition().toPoint())
                    return True
                edges = self._resize_edge_at(event.position().toPoint())
                self._shadow_layer.setCursor(
                    self._cursor_for_edges(edges) if edges else Qt.CursorShape.ArrowCursor
                )
            elif event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                edges = self._resize_edge_at(event.position().toPoint())
                if edges:
                    self._resize_edges = edges
                    self._resize_start_geo = self.geometry()
                    self._resize_start_pos = event.globalPosition().toPoint()
                    return True
            elif event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                if self._resize_edges is not None:
                    self._resize_edges = None
                    self._resize_start_geo = None
                    self._resize_start_pos = None
                    return True
        return super().eventFilter(obj, event)

    def _perform_manual_resize(self, global_pos) -> None:
        if self._resize_start_geo is None or self._resize_start_pos is None:
            return
        delta = global_pos - self._resize_start_pos
        geo = self._resize_start_geo
        x, y, w, h = geo.x(), geo.y(), geo.width(), geo.height()
        min_w, min_h = self.minimumWidth(), self.minimumHeight()
        edges = self._resize_edges
        new_x, new_y, new_w, new_h = x, y, w, h
        if edges & Qt.Edge.LeftEdge:
            new_w = max(min_w, w - delta.x())
            new_x = x + (w - new_w)
        if edges & Qt.Edge.RightEdge:
            new_w = max(min_w, w + delta.x())
        if edges & Qt.Edge.TopEdge:
            new_h = max(min_h, h - delta.y())
            new_y = y + (h - new_h)
        if edges & Qt.Edge.BottomEdge:
            new_h = max(min_h, h + delta.y())
        self.setGeometry(new_x, new_y, new_w, new_h)

    def _on_theme_toggle(self) -> None:
        new_theme = Theme.DARK if current_theme() == Theme.LIGHT else Theme.LIGHT
        set_theme(new_theme)
        self._settings.setValue("appearance/theme", new_theme)
        self._apply_style()
        self._theme_toggle_button.setChecked(new_theme == Theme.DARK)
        self._theme_toggle_button.setText("☀" if new_theme == Theme.DARK else "☾")
        self._theme_toggle_button.setToolTip(
            "Switch to light mode" if new_theme == Theme.DARK else "Switch to dark mode"
        )

    def _switch_main_tab(self, index: int) -> None:
        self.tabs.setCurrentIndex(index)
        self._sync_tab_header_buttons()

    def _sync_tab_header_buttons(self) -> None:
        current = self.tabs.currentIndex()
        for batch_btn, single_btn in getattr(self, "_header_tab_buttons", []):
            batch_btn.setChecked(current == 0)
            single_btn.setChecked(current == 1)

    def _build_single_tab(self) -> None:
        layout = QVBoxLayout(self.single_tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(14)
        self._build_tab_header(layout, active_index=1)

        io_card = self._make_card()
        io_layout = QVBoxLayout(io_card)
        io_layout.setContentsMargins(22, 18, 22, 18)
        io_layout.setSpacing(14)
        io_layout.addWidget(self._label("Input File", "SectionTitle"))
        io_layout.addWidget(self._label("Select one video file.", "SectionDesc"))
        row = QHBoxLayout()
        self.single_file_path = QLabel("No file selected")
        self.single_file_path.setObjectName("PathField")
        self.single_file_path.setMinimumHeight(40)
        self.single_file_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pick_file_btn = QPushButton("Add")
        pick_file_btn.setObjectName("OpenButton")
        pick_file_btn.setFixedWidth(116)
        pick_file_btn.setFixedHeight(38)
        pick_file_btn.clicked.connect(self.choose_single_file)
        row.addWidget(self.single_file_path, 1)
        row.addWidget(pick_file_btn)
        io_layout.addLayout(row)

        io_layout.addSpacing(2)
        io_layout.addWidget(self._label("Output Folder", "SectionTitle"))
        io_layout.addWidget(self._label("Select where output files will be saved.", "SectionDesc"))
        row2 = QHBoxLayout()
        self.single_output_path = QLabel("No folder selected")
        self.single_output_path.setObjectName("PathField")
        self.single_output_path.setMinimumHeight(40)
        self.single_output_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pick_out_btn = QPushButton("Add")
        pick_out_btn.setObjectName("OpenButton")
        pick_out_btn.setFixedWidth(116)
        pick_out_btn.setFixedHeight(38)
        pick_out_btn.clicked.connect(self.choose_single_output)
        row2.addWidget(self.single_output_path, 1)
        row2.addWidget(pick_out_btn)
        io_layout.addLayout(row2)
        layout.addWidget(io_card)

        top_split = QHBoxLayout()
        top_split.setSpacing(10)

        opts_card = self._make_card()
        opts = QVBoxLayout(opts_card)
        opts.setContentsMargins(18, 14, 18, 14)
        opts.setSpacing(8)
        opts.addWidget(self._label("Output Formats", "SectionTitle"))
        opts.addWidget(self._label("Choose one or more formats and frame settings for this file.", "SectionDesc"))

        fmt_row = QHBoxLayout()
        self.s_jpeg = QCheckBox("JPEG")
        self.s_jpeg.setChecked(True)
        self.s_png = QCheckBox("PNG")
        self.s_tiff = QCheckBox("TIFF")
        for cb in (self.s_jpeg, self.s_png, self.s_tiff):
            cb.setMinimumHeight(24)
        fmt_row.addWidget(self.s_jpeg)
        fmt_row.addWidget(self.s_png)
        fmt_row.addWidget(self.s_tiff)
        fmt_row.addStretch(1)
        opts.addLayout(fmt_row)

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        self.single_quality = QSpinBox()
        self.single_quality.setRange(1, 12)
        self.single_quality.setValue(12)
        self.single_quality.setFixedWidth(90)
        row1.addWidget(self._label("Quality (1-12):", "InlineLabel"))
        row1.addWidget(self.single_quality)

        self.single_res_preset = QComboBox()
        self.single_res_preset.addItems(["Original", "2160", "1080", "720"])
        self.single_res_preset.setFixedWidth(130)
        row1.addWidget(self._label("Resolution:", "InlineLabel"))
        row1.addWidget(self.single_res_preset)
        row1.addStretch(1)
        opts.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        self.single_bit_depth = QComboBox()
        self.single_bit_depth.addItems(["8-bit", "16-bit", "32-bit"])
        self.single_bit_depth.setFixedWidth(110)
        row2.addWidget(self._label("Bit Depth:", "InlineLabel"))
        row2.addWidget(self.single_bit_depth)

        self.frame_index_spin = QSpinBox()
        self.frame_index_spin.setRange(1, 1)
        self.frame_index_spin.setValue(1)
        self.frame_index_spin.setFixedWidth(120)
        row2.addWidget(self._label("Frame #:", "InlineLabel"))
        row2.addWidget(self.frame_index_spin)
        row2.addStretch(1)
        opts.addLayout(row2)

        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setRange(1, 1)
        self.frame_slider.setValue(1)
        self.frame_slider.setSingleStep(1)
        self.frame_slider.setPageStep(10)
        self.frame_slider.valueChanged.connect(self._sync_frame_spin_from_slider)
        self.frame_index_spin.valueChanged.connect(self._sync_frame_slider_from_spin)
        opts.addWidget(self.frame_slider)
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.setInterval(120)
        self.preview_timer.timeout.connect(self.generate_preview)
        top_split.addWidget(opts_card, 3)

        preview_card = self._make_card()
        pv = QVBoxLayout(preview_card)
        pv.setContentsMargins(16, 14, 16, 14)
        pv.setSpacing(8)
        top = QHBoxLayout()
        top.addWidget(self._label("Preview", "SectionTitle"))
        top.addStretch(1)
        pv.addLayout(top)
        self.preview_image = QLabel("No preview generated")
        self._configure_preview_label(self.preview_image)
        pv.addWidget(self.preview_image)

        self.single_action_card = self._make_card()
        single_action_layout = QVBoxLayout(self.single_action_card)
        single_action_layout.setContentsMargins(22, 18, 22, 18)
        single_action_layout.setSpacing(10)
        process_top = QHBoxLayout()
        process_top.setSpacing(10)
        single_left = QVBoxLayout()
        single_left.setSpacing(8)
        single_btn_row = QHBoxLayout()
        single_btn_row.setSpacing(8)
        self.single_start_button = QPushButton("Start")
        self.single_start_button.setObjectName("PrimaryButton")
        self.single_start_button.clicked.connect(self.start_single_processing)
        self.single_stop_button = QPushButton("Stop")
        self.single_stop_button.setObjectName("PrimaryButton")
        self.single_stop_button.clicked.connect(self.stop_single_processing)
        self.single_stop_button.hide()
        single_btn_row.addWidget(self.single_start_button)
        single_btn_row.addWidget(self.single_stop_button)
        self.single_status_label = QLabel("Choose file and output folder to begin.")
        self.single_status_label.setObjectName("StatusLabel")
        single_left.addLayout(single_btn_row)
        single_left.addWidget(self.single_status_label)
        process_top.addLayout(single_left, 3)
        process_top.addWidget(preview_card, 2)
        single_action_layout.addLayout(process_top)
        sph = QHBoxLayout()
        sph.addWidget(self._label("Progress", "ProgressTitle"))
        sph.addStretch(1)
        self.single_progress_pct = QLabel("0%")
        self.single_progress_pct.setObjectName("ProgressPct")
        sph.addWidget(self.single_progress_pct)
        self.single_progress_bar = QProgressBar()
        self.single_progress_bar.setRange(0, 100)
        self.single_progress_bar.setValue(0)
        self.single_progress_bar.setTextVisible(False)
        single_action_layout.addLayout(sph)
        single_action_layout.addWidget(self.single_progress_bar)
        layout.addLayout(top_split)
        layout.addWidget(self.single_action_card)

        

    def _label(self, text: str, object_name: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName(object_name)
        return lbl

    def _build_folder_card(
        self,
        title: str,
        description: str,
        button_text: str,
        callback,
    ) -> tuple[QFrame, QLabel]:
        card = self._make_card()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 18, 22, 18)
        layout.setSpacing(10)

        top_row = QHBoxLayout()
        top_row.setSpacing(14)

        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("SectionTitle")
        desc_label = QLabel(description)
        desc_label.setObjectName("SectionDesc")
        text_col.addWidget(title_label)
        text_col.addWidget(desc_label)

        open_button = QPushButton(button_text)
        open_button.setObjectName("OpenButton")
        open_button.clicked.connect(callback)
        open_button.setFixedWidth(116)
        open_button.setFixedHeight(38)

        top_row.addLayout(text_col, 1)
        top_row.addWidget(open_button)

        path_label = QLabel("No folder selected")
        path_label.setObjectName("PathField")
        path_label.setWordWrap(False)
        path_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        path_label.setMinimumHeight(40)
        path_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        layout.addLayout(top_row)
        layout.addWidget(path_label)
        return card, path_label

    def _apply_style(self) -> None:
        QApplication.setStyle("Fusion")
        palette = get_palette()
        spin_up_icon, spin_down_icon = spin_icon_paths(current_theme())
        self.setStyleSheet(
            f"""
            QMainWindow {{
              background: transparent;
            }}
            #ShadowLayer {{
              background: transparent;
            }}
            #WindowFrame {{
              background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                stop:0 {palette.BG_APP_GRADIENT_TOP}, stop:1 {palette.BG_APP_GRADIENT_BOTTOM});
              border: 1px solid {palette.BORDER_SUBTLE};
              border-radius: 22px;
            }}
            #WindowFrame[maximized="true"] {{
              border-radius: 0px;
            }}
            #AppCanvas {{
              background: transparent;
            }}
            #TitleBar {{
              background: transparent;
              border-bottom: 1px solid {palette.BORDER_SUBTLE};
            }}
            #TitleBarIcon {{ background: transparent; font-size: 15px; }}
            #TitleBarText {{
              font-size: 14px;
              font-weight: 800;
              letter-spacing: 0.3px;
              color: {palette.TEXT_PRIMARY};
              background: transparent;
            }}
            #Card {{
              border: none;
              border-top: 3px solid transparent;
              border-radius: {UI.RADIUS_CARD}px;
              background-color: {palette.BG_CARD};
            }}
            #Card[accent="Teal"] {{ border-top-color: {palette.BADGE_TEAL_FG}; }}
            #Card[accent="Amber"] {{ border-top-color: {palette.BADGE_AMBER_FG}; }}
            #Card[accent="Blue"] {{ border-top-color: {palette.BADGE_BLUE_FG}; }}
            #Card[accent="Violet"] {{ border-top-color: {palette.BADGE_VIOLET_FG}; }}
            #SubBlock {{
              border-left: 3px solid {palette.BORDER_SUBTLE};
              border-radius: 3px;
              background: transparent;
            }}
            #SubBlock[accent="Blue"] {{ border-left-color: {palette.BADGE_BLUE_FG}; }}
            #SubBlock[accent="Violet"] {{ border-left-color: {palette.BADGE_VIOLET_FG}; }}
            #BadgeBlue, #BadgeViolet, #BadgeTeal, #BadgeAmber {{
              border-radius: 11px;
              font-size: 10px;
              font-weight: 800;
              letter-spacing: 0.5px;
            }}
            #BadgeBlue {{ background-color: {palette.BADGE_BLUE_BG}; color: {palette.BADGE_BLUE_FG}; }}
            #BadgeViolet {{ background-color: {palette.BADGE_VIOLET_BG}; color: {palette.BADGE_VIOLET_FG}; }}
            #BadgeTeal {{ background-color: {palette.BADGE_TEAL_BG}; color: {palette.BADGE_TEAL_FG}; }}
            #BadgeAmber {{ background-color: {palette.BADGE_AMBER_BG}; color: {palette.BADGE_AMBER_FG}; }}
            #ThemeToggle {{
              border: 1px solid {palette.BORDER_CONTROL};
              border-radius: 15px;
              background-color: {palette.BG_CONTROL};
              color: {palette.TEXT_PRIMARY};
              font-size: 14px;
            }}
            #ThemeToggle:hover {{
              background-color: {palette.BG_CONTROL_HOVER};
              border-color: {palette.BORDER_FOCUS};
            }}
            #ThemeToggle:pressed, #ThemeToggle:checked {{
              background-color: {palette.BG_CONTROL_ACTIVE};
            }}
            QToolButton#WinBtnMin, QToolButton#WinBtnMax, QToolButton#WinBtnClose {{
              border: none;
              border-radius: 14px;
              background: transparent;
              color: {palette.TEXT_SECONDARY};
              font-size: 15px;
              font-weight: 600;
            }}
            QToolButton#WinBtnMin:hover, QToolButton#WinBtnMax:hover {{
              background-color: {palette.BG_CONTROL_HOVER};
              color: {palette.TEXT_PRIMARY};
            }}
            QToolButton#WinBtnClose:hover {{
              background-color: #E24C4C;
              color: #FFFFFF;
            }}
            #SectionTitle {{ font-size: {UI.FONT_SECTION + 2}px; font-weight: 800; letter-spacing: 0.2px; color: {palette.TEXT_PRIMARY}; }}
            #SectionDesc {{ font-size: {UI.FONT_SMALL}px; color: {palette.TEXT_SECONDARY}; }}
            #InlineLabel {{ font-size: {UI.FONT_SMALL}px; font-weight: 500; color: {palette.TEXT_SECONDARY}; }}
            #PathField {{
              border: 1px solid {palette.BORDER_CONTROL};
              border-radius: {UI.RADIUS_CONTROL}px;
              padding: {UI.SPACE_SM}px {UI.SPACE_MD}px;
              background-color: {palette.BG_CONTROL};
              color: {palette.TEXT_PRIMARY};
              font-size: {UI.FONT_SMALL}px;
            }}
            #PreviewSurface {{
              border: 1px solid #232B45;
              border-radius: {UI.RADIUS_MEDIA}px;
              padding: {UI.SPACE_MD}px;
              background-color: #10152A;
              color: #7C88AC;
              font-size: {UI.FONT_SMALL}px;
            }}
            #StatusLabel {{ color: {palette.TEXT_SECONDARY}; font-size: {UI.FONT_SMALL}px; }}
            #ProgressTitle {{ font-size: {UI.FONT_SECTION}px; font-weight: 650; color: {palette.TEXT_PRIMARY}; }}
            #ProgressPct {{ font-size: {UI.FONT_SECTION}px; font-weight: 650; color: {palette.TEXT_SECONDARY}; }}
            QPushButton {{
              border: 1px solid {palette.BORDER_CONTROL};
              border-radius: {UI.RADIUS_CONTROL}px;
              padding: {UI.SPACE_SM}px {UI.SPACE_MD}px;
              font-size: {UI.FONT_BUTTON}px;
              font-weight: 600;
              color: {palette.TEXT_PRIMARY};
              background-color: {palette.BG_CONTROL};
            }}
            QPushButton:hover {{
              background-color: {palette.BG_CONTROL_HOVER};
              border-color: {palette.BORDER_FOCUS};
            }}
            QPushButton:pressed {{
              background-color: {palette.BG_CONTROL_ACTIVE};
              border-color: {palette.BORDER_FOCUS};
            }}
            QPushButton:disabled {{
              color: {palette.TEXT_MUTED};
              border-color: {palette.BORDER_CONTROL};
              background-color: {palette.DISABLED_BG};
            }}
            QPushButton#PrimaryButton {{
              min-height: {UI.HEIGHT_PRIMARY + 8}px;
              font-size: {UI.FONT_BUTTON + 1}px;
              font-weight: 700;
              border: none;
              border-radius: {UI.RADIUS_CONTROL + 4}px;
              color: #FFFFFF;
              background-color: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {palette.ACCENT_ALT}, stop:1 {palette.ACCENT});
            }}
            QPushButton#PrimaryButton:hover {{
              background-color: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {palette.ACCENT_GRADIENT_HOVER_START}, stop:1 {palette.ACCENT_HOVER});
            }}
            QPushButton#PrimaryButton:pressed {{
              background-color: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {palette.ACCENT_GRADIENT_PRESSED_START}, stop:1 {palette.ACCENT_GRADIENT_PRESSED_END});
            }}
            QPushButton#OutputButton {{
              min-height: {UI.HEIGHT_PRIMARY}px;
              font-size: {UI.FONT_BUTTON}px;
              font-weight: 700;
              color: #FFFFFF;
              border: 1px solid {palette.SUCCESS};
              background-color: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {palette.SUCCESS_HOVER}, stop:1 {palette.SUCCESS_PRESSED});
            }}
            QPushButton#OpenButton, QPushButton#SecondaryButton {{
              min-height: {UI.HEIGHT_CONTROL}px;
              max-height: {UI.HEIGHT_CONTROL}px;
              font-size: {UI.FONT_BUTTON}px;
              padding: 0 {UI.SPACE_MD}px;
            }}
            QCheckBox {{
              font-size: {UI.FONT_SMALL}px;
              spacing: {UI.SPACE_SM}px;
              color: {palette.TEXT_PRIMARY};
              min-height: {UI.HEIGHT_COMPACT}px;
            }}
            QCheckBox::indicator {{
              width: 16px; height: 16px; border-radius: 4px;
              border: 1px solid {palette.CHECKBOX_BORDER}; background-color: {palette.CHECKBOX_BG};
            }}
            QCheckBox::indicator:checked {{ border: 1px solid {palette.CHECKBOX_CHECKED_BORDER}; background-color: {palette.CHECKBOX_CHECKED_BG}; image: none; }}
            QToolButton#FormatChip {{
              border: 1px solid {palette.BORDER_CONTROL};
              border-radius: {UI.RADIUS_PILL}px;
              background-color: {palette.BG_CONTROL};
              padding: 4px 18px;
              font-weight: 600;
              color: {palette.TEXT_SECONDARY};
            }}
            QToolButton#FormatChip:hover {{ background-color: {palette.BG_CONTROL_HOVER}; border-color: {palette.BORDER_FOCUS}; }}
            QToolButton#FormatChip:checked {{
              border: 1px solid {palette.ACCENT};
              background-color: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {palette.ACCENT_ALT}, stop:1 {palette.ACCENT});
              color: #FFFFFF;
            }}
            QComboBox, QSpinBox {{
              border: 1px solid {palette.BORDER_CONTROL};
              border-radius: {UI.RADIUS_CONTROL}px;
              min-height: {UI.HEIGHT_CONTROL}px;
              font-size: {UI.FONT_SMALL}px;
              background-color: {palette.BG_CONTROL};
              color: {palette.TEXT_PRIMARY};
            }}
            QComboBox {{ padding: 2px {UI.SPACE_MD}px; }}
            QSpinBox {{
              padding: 2px {UI.SPACE_SM}px;
              padding-right: 26px;
              selection-background-color: {palette.SELECTION_BG};
              min-height: {UI.HEIGHT_CONTROL}px;
              max-height: {UI.HEIGHT_CONTROL}px;
            }}
            QComboBox:hover {{ border-color: {palette.BORDER_FOCUS}; background-color: {palette.CONTROL_HOVER_ALT}; }}
            QSpinBox::up-button {{
              subcontrol-origin: border;
              subcontrol-position: top right;
              width: 20px;
              height: 20px;
              border: none;
              border-left: 1px solid {palette.BORDER_CONTROL};
              border-bottom: 1px solid {palette.BORDER_CONTROL};
              border-top-right-radius: {UI.RADIUS_CONTROL}px;
              background: {palette.CONTROL_HOVER_ALT};
            }}
            QSpinBox::down-button {{
              subcontrol-origin: border;
              subcontrol-position: bottom right;
              width: 20px;
              height: 20px;
              border: none;
              border-left: 1px solid {palette.BORDER_CONTROL};
              border-bottom-right-radius: {UI.RADIUS_CONTROL}px;
              background: {palette.CONTROL_HOVER_ALT};
            }}
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {palette.SPIN_BUTTON_HOVER}; }}
            QSpinBox::up-button:pressed, QSpinBox::down-button:pressed {{ background: {palette.SPIN_BUTTON_PRESSED}; }}
            QSpinBox::up-arrow {{
              image: url("{spin_up_icon}");
              width: 8px;
              height: 5px;
            }}
            QSpinBox::down-arrow {{
              image: url("{spin_down_icon}");
              width: 8px;
              height: 5px;
            }}
            QComboBox::drop-down {{ border: none; width: 22px; }}
            QComboBox QAbstractItemView {{
              background-color: {palette.POPUP_BG};
              color: {palette.TEXT_PRIMARY};
              border: 1px solid {palette.POPUP_BORDER};
              border-radius: {UI.RADIUS_CONTROL}px;
              padding: {UI.SPACE_XS}px;
              selection-background-color: {palette.SELECTION_BG};
              outline: 0;
            }}
            QLabel {{ font-size: {UI.FONT_SMALL}px; color: {palette.TEXT_PRIMARY}; }}
            QSlider::groove:horizontal {{
              border: 1px solid {palette.BORDER_CONTROL};
              height: 4px; border-radius: 2px; background: {palette.TRACK_BG};
            }}
            QSlider::sub-page:horizontal {{
              border-radius: 2px;
              background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {palette.ACCENT_ALT}, stop:1 {palette.ACCENT});
            }}
            QSlider::handle:horizontal {{
              background: {palette.ACCENT};
              border: 1px solid {palette.BORDER_CONTROL};
              width: 16px; margin: -6px 0; border-radius: 8px;
            }}
            QProgressBar {{
              border: 1px solid {palette.BORDER_CONTROL};
              border-radius: 9px; background-color: {palette.TRACK_BG}; min-height: 18px;
            }}
            QProgressBar::chunk {{
              border-radius: 8px;
              background-color: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 {palette.ACCENT_ALT}, stop:1 {palette.ACCENT});
            }}
            """
        )

    def _set_input_path(self, selected: str) -> None:
        input_path = str(Path(selected))
        self.input_path_label.setText(input_path)
        if not self.output_manually_set:
            p = Path(input_path)
            default_out = (p.parent if p.is_file() else p) / "Stills"
            self.output_path_label.setText(str(default_out))
        self._reset_post_run_state()
        self.status_label.setText("Input changed. Ready to start.")

    def choose_input_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose input file",
            str(Path.home()),
            "Video Files (*.mov *.mp4 *.m4v *.mkv *.avi)",
        )
        if not selected:
            return
        self._set_input_path(selected)
        source = Path(selected)
        self.batch_preview.setText("Generating preview...")
        self.batch_preview.set_source_pixmap(None)
        frame_result = self._resolve_frame_count(source)
        self.current_frame_result = frame_result
        max_frame = max(2, frame_result.frame_count)
        self.frame_slider.setEnabled(True)
        self.frame_index_spin.setEnabled(True)
        self.frame_slider.setRange(1, max_frame)
        self.frame_index_spin.setRange(1, max_frame)
        self.frame_slider.setValue(1)
        self.frame_index_spin.setValue(1)
        if frame_result.confidence in ("estimated", "fallback"):
            self.frame_count_info.setText(f"Frames: about {max_frame} ({frame_result.source})")
        else:
            self.frame_count_info.setText(f"Frames: {max_frame} ({frame_result.source})")
        self._update_single_bit_depth_support()
        QTimer.singleShot(0, self.generate_preview)

    def choose_input_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose input folder")
        if not selected:
            return
        self._set_input_path(selected)
        self.frame_slider.blockSignals(True)
        self.frame_index_spin.blockSignals(True)
        self.frame_slider.setRange(1, 1000)
        self.frame_index_spin.setRange(1, 1000)
        self.frame_slider.setValue(1)
        self.frame_index_spin.setValue(1)
        self.frame_slider.blockSignals(False)
        self.frame_index_spin.blockSignals(False)
        self.frame_slider.setEnabled(True)
        self.frame_index_spin.setEnabled(True)
        self.current_frame_result = None
        self.frame_count_info.setText("Frames: batch mode (1-1000)")
        self.batch_preview.set_source_pixmap(None)
        self.batch_preview.setText("Preview not available in batch processing")
        self.status_label.setText("Batch mode: selected frame # is applied to every file in this folder.")

    def choose_single_file(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose input video",
            str(Path.home()),
            "Video Files (*.mov *.mp4 *.m4v *.mkv *.avi)",
        )
        if not selected:
            return
        p = Path(selected)
        self.single_file_path.setText(str(p))
        if self.single_output_path.text().strip() in ("", "No folder selected"):
            self.single_output_path.setText(str(p.parent / "Stills"))
        self.preview_image.setPixmap(QPixmap())
        self.preview_image.setText("Generating preview...")
        frame_result = self._resolve_frame_count(p)
        self.current_frame_result = frame_result
        max_frame = max(2, frame_result.frame_count)
        self.frame_slider.setRange(1, max_frame)
        self.frame_index_spin.setRange(1, max_frame)
        self.frame_slider.setValue(1)
        self.frame_index_spin.setValue(1)
        self.frame_slider.setEnabled(True)
        self.frame_index_spin.setEnabled(True)
        if hasattr(self, "frame_count_info"):
            if frame_result.confidence in ("estimated", "fallback"):
                self.frame_count_info.setText(f"Frames: about {max_frame} ({frame_result.source})")
            else:
                self.frame_count_info.setText(f"Frames: {max_frame} ({frame_result.source})")
        self._update_single_bit_depth_support()
        self.generate_preview()

    def choose_single_output(self) -> None:
        initial = self.single_output_path.text().strip()
        if initial == "No folder selected":
            initial = str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Choose output folder", initial)
        if not selected:
            return
        self.single_output_path.setText(str(Path(selected)))

    def _disable_unsupported_bit_depth_option(self, combo: QComboBox) -> None:
        model = combo.model()
        item32 = model.item(2)
        if item32 is not None:
            item32.setEnabled(False)
            item32.setForeground(Qt.GlobalColor.gray)

    def _update_single_bit_depth_support(self) -> None:
        if hasattr(self, "single_file_path"):
            path_text = self.single_file_path.text().strip()
        else:
            path_text = self.input_path_label.text().strip()
        if not path_text or path_text in ("No file selected", "No folder selected"):
            return
        source = Path(path_text)
        if not source.is_file():
            self.single_bit_depth.setCurrentIndex(0)
            model = self.single_bit_depth.model()
            for idx in (1, 2):
                item = model.item(idx)
                if item is not None:
                    item.setEnabled(False)
                    item.setForeground(Qt.GlobalColor.gray)
            return
        supports_16 = self._source_supports_16_bit(source)
        model = self.single_bit_depth.model()
        item16 = model.item(1)
        item32 = model.item(2)
        if item16 is not None:
            item16.setEnabled(supports_16)
            item16.setForeground(Qt.GlobalColor.white if supports_16 else Qt.GlobalColor.gray)
        if item32 is not None:
            item32.setEnabled(False)
            item32.setForeground(Qt.GlobalColor.gray)
        if not supports_16 and self.single_bit_depth.currentIndex() > 0:
            self.single_bit_depth.setCurrentIndex(0)

    def _sync_frame_spin_from_slider(self, value: int) -> None:
        if self.frame_index_spin.value() != value:
            self.frame_index_spin.blockSignals(True)
            self.frame_index_spin.setValue(value)
            self.frame_index_spin.blockSignals(False)
        self._queue_preview_update()

    def _sync_frame_slider_from_spin(self, value: int) -> None:
        if self.frame_slider.value() != value:
            self.frame_slider.blockSignals(True)
            self.frame_slider.setValue(value)
            self.frame_slider.blockSignals(False)
        self._queue_preview_update()

    def _on_slider_pressed(self) -> None:
        self.is_scrubbing = True

    def _on_slider_released(self) -> None:
        self.is_scrubbing = False
        self.generate_preview()

    def _active_preview_label(self) -> QLabel:
        return self.batch_preview

    def _display_preview_pixmap(self, image_path: str) -> None:
        label = self._active_preview_label()
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            label.set_source_pixmap(None)
            label.setText("Preview unavailable for selected frame")
            return
        if pixmap.height() > 0:
            self.current_preview_aspect = pixmap.width() / pixmap.height()
        label.setText("")
        label.set_source_pixmap(pixmap)

    def _preview_cache_key(self, source: Path, frame_number: int) -> tuple[str, int]:
        return (str(source.resolve()), int(frame_number))

    def _get_cached_preview(self, source: Path, frame_number: int) -> str | None:
        key = self._preview_cache_key(source, frame_number)
        cached = self.preview_cache.get(key)
        if cached and Path(cached).is_file():
            return cached
        if key in self.preview_cache:
            self.preview_cache.pop(key, None)
        return None

    def _store_cached_preview(self, source: Path, frame_number: int, image_path: str) -> None:
        key = self._preview_cache_key(source, frame_number)
        if key not in self.preview_cache:
            self.preview_cache_order.append(key)
        self.preview_cache[key] = image_path
        while len(self.preview_cache_order) > self.max_preview_cache_items:
            old_key = self.preview_cache_order.pop(0)
            old_path = self.preview_cache.pop(old_key, None)
            if old_path:
                try:
                    Path(old_path).unlink(missing_ok=True)
                except Exception:
                    pass

    def _queue_preview_update(self) -> None:
        path_text = self.input_path_label.text().strip()
        if not path_text or path_text in ("No folder selected", "No file selected"):
            return
        if not Path(path_text).is_file():
            return
        self.preview_timer.setInterval(250 if self.is_scrubbing else 80)
        self.preview_timer.start()

    def choose_output_folder(self) -> None:
        initial = self.output_path_label.text().strip()
        if initial == "No folder selected":
            initial = self.input_path_label.text().strip()
        if initial == "No folder selected":
            initial = str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Choose output folder", initial)
        if not selected:
            return
        self.output_path_label.setText(str(Path(selected)))
        self.output_manually_set = True
        self._reset_post_run_state()
        self.status_label.setText("Output folder changed. Ready to start.")

    def _reset_post_run_state(self) -> None:
        self.last_output_dir = ""
        self.action_button.setText("Start")
        self.action_button.setEnabled(True)
        self.stop_button.hide()
        self.open_output_button.hide()

    def _selected_formats(self) -> list[tuple[str, str]]:
        formats: list[tuple[str, str]] = []
        if self.jpeg_cb.isChecked():
            formats.append(("jpg", "JPEG"))
        if self.png_cb.isChecked():
            formats.append(("png", "PNG"))
        if self.tiff_cb.isChecked():
            formats.append(("tiff", "TIFF"))
        return formats

    def _collect_files(self, input_dir: Path) -> list[Path]:
        files = [f for f in input_dir.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
        files.sort(key=lambda p: p.name.lower())
        return files

    def _source_supports_16_bit(self, source: Path) -> bool:
        return source_supports_16_bit(self.ffprobe, source)

    def _update_tiff_controls_visibility(self) -> None:
        visible = self.tiff_cb.isChecked()
        # Kept for backward compatibility with previous UI wiring.
        return

    def start_processing(self) -> None:
        if self.is_processing:
            return
        if not self.ffmpeg:
            QMessageBox.critical(self, "Missing ffmpeg", "ffmpeg was not found in app bundle or system paths.")
            return

        input_text = self.input_path_label.text().strip()
        output_text = self.output_path_label.text().strip()
        if input_text == "No folder selected":
            input_text = ""
        if output_text == "No folder selected":
            output_text = ""

        input_path = Path(input_text)
        if not input_path.exists():
            QMessageBox.critical(self, "Invalid input", "Choose a valid input file or folder.")
            return

        output_dir = Path(output_text) if output_text else ((input_path.parent if input_path.is_file() else input_path) / "Stills")
        if output_dir == input_path and input_path.is_dir():
            output_dir = input_path / "Stills"
            self.output_path_label.setText(str(output_dir))
        output_dir.mkdir(parents=True, exist_ok=True)

        selected_formats = self._selected_formats()
        if not selected_formats:
            QMessageBox.critical(self, "No format selected", "Choose at least one output format: JPEG, PNG, or TIFF.")
            return

        for _, folder_name in selected_formats:
            (output_dir / folder_name).mkdir(parents=True, exist_ok=True)

        if input_path.is_file():
            if input_path.suffix.lower() not in VIDEO_EXTENSIONS:
                QMessageBox.warning(self, "Unsupported input file", "Selected file is not a supported video format.")
                return
            files = [input_path]
        else:
            files = self._collect_files(input_path)
        frame_number = self.frame_index_spin.value()
        if not files:
            QMessageBox.warning(self, "No video files", "No supported video files found in input.")
            return

        total_jobs = len(files) * len(selected_formats)
        self.is_processing = True
        self.last_output_dir = ""
        self.progress_bar.setValue(0)
        self.progress_pct.setText("0%")
        self.status_label.setText(f"Processing 0/{total_jobs}...")
        self.action_button.setText("Restart")
        self.action_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.stop_button.show()
        self.open_output_button.hide()

        self.worker = FrameExportWorker(
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
            files=files,
            output_dir=output_dir,
            selected_formats=selected_formats,
            quality_preset=self._map_quality_1_12_to_preset(self.single_quality.value()),
            tiff_bit_depth=self.single_bit_depth.currentText(),
            quality_level=self.single_quality.value(),
            resize_height=self._resolution_to_height(self.single_res_preset.currentText()),
            frame_number=frame_number,
            parent=self,
        )
        self.worker.progress_updated.connect(self._on_progress)
        self.worker.finished_with_result.connect(self._on_finished)
        self.worker.start()

    def _on_progress(self, completed: int, total: int, pct: int) -> None:
        self.progress_bar.setValue(pct)
        self.progress_pct.setText(f"{pct}%")
        self.status_label.setText(f"Processing {completed}/{total}...")

    def _on_finished(self, total: int, completed: int, output_dir: str, failed: list, skipped: list, cancelled: bool) -> None:
        self.is_processing = False
        self.worker = None
        self.last_output_dir = output_dir
        self.stop_button.hide()

        if cancelled:
            pct = int((completed / total) * 100) if total else 0
            self.progress_pct.setText(f"{pct}%")
            self.status_label.setText("Processing cancelled.")
            self.action_button.setText("Start")
            self.action_button.setEnabled(True)
            self.open_output_button.hide()
            return

        self.progress_bar.setValue(100)
        self.progress_pct.setText("100%")
        self.action_button.setText("Restart")
        self.action_button.setEnabled(True)

        output_path = Path(self.last_output_dir) if self.last_output_dir else None
        show_open = bool(output_path and output_path.is_dir())
        self.open_output_button.setVisible(show_open)

        if failed or skipped:
            exported_ok = max(0, completed - len(failed) - (len(skipped) * len(self._selected_formats())))
            self.status_label.setText(
                f"Done with issues. Exported {exported_ok}/{total} files. Skipped files: {len(skipped)}."
            )
            message = []
            if failed:
                message.append(f"{len(failed)} export job(s) failed.")
                message.append("\n".join(failed[:15]))
            if skipped:
                message.append(f"Skipped files: {len(skipped)}")
                message.append("\n".join(skipped[:15]))
            QMessageBox.warning(self, "Completed with issues", "\n\n".join(message))
            return
        self.status_label.setText(f"Done. Exported {completed} image file(s).")
        QMessageBox.information(self, "Completed", f"Exported {completed} image file(s) to:\n{output_dir}")

    def open_output_folder(self) -> None:
        path = self.last_output_dir or self.output_path_label.text().strip()
        if path and path != "No folder selected" and Path(path).is_dir():
            open_in_file_manager(path)

    def stop_processing(self) -> None:
        if not self.is_processing or not self.worker:
            return
        self.stop_button.setEnabled(False)
        self.status_label.setText("Stopping process...")
        self.worker.request_stop()

    def start_single_processing(self) -> None:
        if self.is_single_processing:
            return
        if not self.ffmpeg:
            QMessageBox.critical(self, "Missing ffmpeg", "ffmpeg was not found in app bundle or system paths.")
            return
        source_text = self.single_file_path.text().strip()
        out_text = self.single_output_path.text().strip()
        if not source_text or source_text == "No file selected":
            QMessageBox.warning(self, "No input file", "Choose an input file first.")
            return
        source = Path(source_text)
        if not source.is_file():
            QMessageBox.warning(self, "Invalid file", "Selected input file was not found.")
            return
        output_dir = Path(out_text) if out_text and out_text != "No folder selected" else source.parent / "Stills"
        output_dir.mkdir(parents=True, exist_ok=True)

        selected_formats: list[tuple[str, str]] = []
        if self.s_jpeg.isChecked():
            selected_formats.append(("jpg", "JPEG"))
        if self.s_png.isChecked():
            selected_formats.append(("png", "PNG"))
        if self.s_tiff.isChecked():
            selected_formats.append(("tiff", "TIFF"))
        if not selected_formats:
            QMessageBox.warning(self, "No format selected", "Choose at least one output format.")
            return

        for _, folder in selected_formats:
            (output_dir / folder).mkdir(parents=True, exist_ok=True)

        preset = self._map_quality_1_12_to_preset(self.single_quality.value())
        tiff_depth = self.single_bit_depth.currentText()
        resize_height = self._resolution_to_height(self.single_res_preset.currentText())

        self.is_single_processing = True
        self.single_start_button.setEnabled(False)
        self.single_stop_button.setEnabled(True)
        self.single_stop_button.show()
        self.single_status_label.setText("Processing 0/1...")
        self.single_progress_bar.setValue(0)
        self.single_progress_pct.setText("0%")
        self.single_worker = FrameExportWorker(
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
            files=[source],
            output_dir=output_dir,
            selected_formats=selected_formats,
            quality_preset=preset,
            tiff_bit_depth=tiff_depth,
            quality_level=self.single_quality.value(),
            resize_height=resize_height,
            frame_number=self.frame_index_spin.value(),
            parent=self,
        )
        self.single_worker.progress_updated.connect(self._on_single_progress)
        self.single_worker.finished_with_result.connect(self._on_single_finished)
        self.single_worker.start()

    def _map_quality_1_12_to_preset(self, value: int) -> str:
        if value >= 9:
            return "High"
        if value >= 5:
            return "Balanced"
        return "Small"

    def _resolution_to_height(self, value: str) -> int | None:
        if value == "Original":
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _on_single_progress(self, completed: int, total: int, pct: int) -> None:
        self.single_progress_bar.setValue(pct)
        self.single_progress_pct.setText(f"{pct}%")
        self.single_status_label.setText(f"Processing {completed}/{total}...")

    def _on_single_finished(self, total: int, completed: int, output_dir: str, failed: list, skipped: list, cancelled: bool) -> None:
        self.is_single_processing = False
        self.single_worker = None
        self.single_start_button.setEnabled(True)
        self.single_stop_button.hide()
        if cancelled:
            pct = int((completed / total) * 100) if total else 0
            self.single_progress_pct.setText(f"{pct}%")
            self.single_status_label.setText("Processing cancelled.")
            return
        self.single_progress_bar.setValue(100)
        self.single_progress_pct.setText("100%")
        if failed or skipped:
            self.single_status_label.setText("Done with errors.")
            skipped_note = f"\nSkipped files: {len(skipped)}" if skipped else ""
            QMessageBox.warning(self, "Single-file export", f"{len(failed)} export(s) failed.{skipped_note}")
            return
        self.single_status_label.setText("Done. Exported single file.")
        QMessageBox.information(self, "Single-file export", f"Exported to:\n{output_dir}")

    def stop_single_processing(self) -> None:
        if not self.is_single_processing or not self.single_worker:
            return
        self.single_stop_button.setEnabled(False)
        self.single_status_label.setText("Stopping process...")
        self.single_worker.request_stop()

    def generate_preview(self) -> None:
        path_text = self.input_path_label.text().strip()

        if not path_text or path_text in ("No folder selected", "No file selected"):
            return
        source = Path(path_text)
        if not source.is_file():
            return
        if not self.ffmpeg:
            QMessageBox.critical(self, "Missing ffmpeg", "ffmpeg was not found in app bundle or system paths.")
            return

        frame_index = max(1, self.frame_slider.value())
        cached = self._get_cached_preview(source, frame_index)
        if cached:
            self._display_preview_pixmap(cached)
            return

        self.preview_request_id += 1
        request_id = self.preview_request_id

        if self.preview_worker and self.preview_worker.isRunning():
            self.preview_worker.request_stop()
            self.preview_worker.wait(200)

        preview_path = Path(tempfile.gettempdir()) / f"freezeframe_preview_{request_id}_{frame_index}.jpg"
        label = self._active_preview_label()
        existing = label.pixmap()
        if existing is None or existing.isNull():
            label.setText("Generating preview...")
        else:
            label.setToolTip("Generating preview...")

        self.preview_worker = PreviewWorker(
            request_id=request_id,
            ffmpeg=self.ffmpeg,
            source=source,
            frame_number=frame_index,
            output_path=preview_path,
            fps=self.current_frame_result.fps if self.current_frame_result else 0.0,
            is_variable_frame_rate=self.current_frame_result.is_variable_frame_rate if self.current_frame_result else False,
            parent=self,
        )
        self.preview_worker.finished_with_preview.connect(
            lambda rid, frame, path, s=source: self._on_preview_ready(rid, s, frame, path)
        )
        self.preview_worker.failed.connect(self._on_preview_failed)
        self.preview_worker.finished.connect(self._on_preview_worker_finished)
        self.preview_worker.start()

    def _on_preview_ready(self, request_id: int, source: Path, frame_number: int, image_path: str) -> None:
        if request_id != self.preview_request_id:
            return
        self._store_cached_preview(source, frame_number, image_path)
        self._display_preview_pixmap(image_path)
        self._active_preview_label().setToolTip("")

    def _on_preview_failed(self, request_id: int, frame_number: int, message: str) -> None:
        if request_id != self.preview_request_id:
            return
        label = self._active_preview_label()
        existing = label.pixmap()
        if existing is None or existing.isNull():
            label.setText(message)
        else:
            # Keep last good preview visible; report issue non-disruptively.
            label.setToolTip(message)
            self.status_label.setText(f"Preview warning: {message} (frame {frame_number})")

    def _on_preview_worker_finished(self) -> None:
        worker = self.sender()
        if worker is self.preview_worker:
            self.preview_worker = None

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_preview_viewport_size()
        QTimer.singleShot(0, self._update_preview_viewport_size)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.preview_worker and self.preview_worker.isRunning():
            self.preview_worker.request_stop()
            self.preview_worker.wait(500)
        if self.is_processing and self.worker:
            should_close = QMessageBox.question(
                self,
                "Processing in progress",
                "A conversion is still running.\n\nClose and stop the current process?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if should_close != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.request_stop()
            self.worker.wait(2000)
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setOrganizationName("FreezeFrame")
    app.setApplicationName("FreezeFrame")
    app_icon_path = resource_path("assets/FreezeFrame_icon_1024.png")
    if app_icon_path.is_file():
        app.setWindowIcon(QIcon(str(app_icon_path)))
    font = QFont("SF Pro Display")
    app.setFont(font)
    window = FreezeFrameWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
