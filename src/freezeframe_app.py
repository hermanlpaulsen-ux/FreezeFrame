#!/usr/bin/env python3

import re
import shutil
import subprocess
import sys
import tempfile
import threading
import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal, QTimer
from PySide6.QtGui import QCloseEvent, QFont, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSlider,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mkv", ".avi"}
logger = logging.getLogger("freezeframe")


@dataclass
class FrameCountResult:
    frame_count: int
    confidence: str  # exact | counted | estimated | fallback
    source: str
    fps: float = 0.0
    duration_seconds: float = 0.0
    is_variable_frame_rate: bool = False


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
        ffprobe = self.ffprobe
        if not Path(ffprobe).is_file():
            return False
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=pix_fmt,bits_per_raw_sample",
                "-of",
                "default=nw=1:nk=1",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return False
        lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        for line in lines:
            if line.isdigit() and int(line) > 8:
                return True
        pix_fmt = lines[0] if lines else ""
        if re.search(r"p(10|12|14|16)(le|be)?$", pix_fmt):
            return True
        if re.search(r"(rgb|bgr|gbr)p?(30|36|48|64)(le|be)?$", pix_fmt):
            return True
        if re.search(r"gray(10|12|14|16)(le|be)?$", pix_fmt):
            return True
        return False

    def _probe_frame_count(self, source: Path) -> int:
        ffprobe = self.ffprobe
        if not Path(ffprobe).is_file():
            return 1
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=nb_frames,r_frame_rate,duration",
                "-of",
                "default=nw=1:nk=1",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return 1
        lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        nb_frames = 0
        fps = 0.0
        duration = 0.0
        for line in lines:
            if line.isdigit():
                nb_frames = int(line)
            elif "/" in line:
                try:
                    num, den = line.split("/", 1)
                    den_val = float(den)
                    if den_val != 0:
                        fps = float(num) / den_val
                except Exception:
                    pass
            else:
                try:
                    duration = float(line)
                except Exception:
                    pass
        if nb_frames > 0:
            return nb_frames
        if fps > 0 and duration > 0:
            return max(1, int(fps * duration))
        return 1

    def _probe_duration_seconds(self, source: Path) -> float:
        ffprobe = self.ffprobe
        if not Path(ffprobe).is_file():
            return 0.0
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return 0.0
        line = probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else ""
        try:
            return max(0.0, float(line))
        except Exception:
            return 0.0

    def _probe_fps(self, source: Path) -> float:
        ffprobe = self.ffprobe
        if not Path(ffprobe).is_file():
            return 0.0
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=r_frame_rate",
                "-of",
                "default=nw=1:nk=1",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return 0.0
        line = probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else ""
        if "/" in line:
            try:
                num, den = line.split("/", 1)
                den_val = float(den)
                if den_val != 0:
                    return float(num) / den_val
            except Exception:
                return 0.0
        try:
            return float(line)
        except Exception:
            return 0.0

    def _parse_fps(self, value: str) -> float:
        if not value or value == "N/A":
            return 0.0
        if "/" in value:
            try:
                num, den = value.split("/", 1)
                den_val = float(den)
                if den_val == 0:
                    return 0.0
                fps = float(num) / den_val
                return fps if fps > 0 else 0.0
            except Exception:
                return 0.0
        try:
            fps = float(value)
            return fps if fps > 0 else 0.0
        except Exception:
            return 0.0

    def _source_timing(self, source: Path) -> tuple[float, bool]:
        key = str(source.resolve())
        if key in self._timing_cache:
            return self._timing_cache[key]
        ffprobe = self.ffprobe
        if not Path(ffprobe).is_file():
            self._timing_cache[key] = (0.0, False)
            return self._timing_cache[key]
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,r_frame_rate",
                "-of",
                "default=nw=1:nk=1",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            fps = self._probe_fps(source)
            self._timing_cache[key] = (fps, False)
            return self._timing_cache[key]
        lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        avg_fps = self._parse_fps(lines[0]) if len(lines) > 0 else 0.0
        real_fps = self._parse_fps(lines[1]) if len(lines) > 1 else 0.0
        fps = avg_fps or real_fps
        is_vfr = avg_fps > 0 and real_fps > 0 and abs(avg_fps - real_fps) > 0.01
        self._timing_cache[key] = (fps, is_vfr)
        return self._timing_cache[key]

    def _build_export_tail(self, ext: str, target: Path) -> list[str]:
        tail: list[str] = []
        if ext == "jpg":
            jpeg_quality_map = {"High": "2", "Balanced": "5", "Small": "9"}
            tail.extend(["-q:v", jpeg_quality_map.get(self.quality_preset, "5")])
        elif ext == "png":
            png_compression_map = {"High": "2", "Balanced": "5", "Small": "9"}
            tail.extend(["-compression_level", png_compression_map.get(self.quality_preset, "5")])
        elif ext == "tiff":
            tiff_compression_map = {"High": "lzw", "Balanced": "deflate", "Small": "zlib"}
            tail.extend(["-compression_algo", tiff_compression_map.get(self.quality_preset, "deflate")])
        tail.append(str(target))
        return tail

    def _build_export_commands(self, source: Path, target: Path, ext: str) -> list[list[str]]:
        if ext == "tiff":
            source_supports_16_bit = self._source_supports_16_bit(source)
            use_tiff_16_bit = self.tiff_bit_depth.startswith("16-bit") and source_supports_16_bit
            output_format = "rgb48le" if use_tiff_16_bit else "rgb24"
        elif ext == "png":
            output_format = "rgb24"
        else:
            output_format = "yuvj420p"

        frame_idx_zero_based = max(0, self.frame_number - 1)
        fps, is_vfr = self._source_timing(source)
        tail = self._build_export_tail(ext, target)

        exact_cmd = [
            self.ffmpeg,
            "-y",
            "-hwaccel",
            "none",
            "-i",
            str(source),
            "-vf",
            f"select=eq(n\\,{frame_idx_zero_based}),format={output_format}",
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
                f"select=eq(n\\,{local_frame}),format={output_format}",
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
                f"format={output_format}",
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

    def run(self) -> None:
        total = len(self.files) * len(self.selected_formats)
        completed = 0
        failed: list[str] = []
        skipped: list[str] = []

        for file in self.files:
            if self._stop_requested:
                break
            if self.frame_number > 1:
                file_total_frames = self._probe_frame_count(file)
                if file_total_frames > 1 and file_total_frames < self.frame_number:
                    skipped.append(file.name)
                    completed += len(self.selected_formats)
                    pct = int((completed / total) * 100) if total else 0
                    self.progress_updated.emit(completed, total, pct)
                    continue
            for ext, folder_name in self.selected_formats:
                if self._stop_requested:
                    break
                preset_suffix = self.quality_preset.strip().lower().replace(" ", "-")
                target = self.output_dir / folder_name / f"{file.stem}_{preset_suffix}.{ext}"
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
        self.setWindowTitle("FreezeFrame")
        self.resize(1320, 920)
        self.setMinimumSize(1180, 840)

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

        self._build_ui()
        self._apply_style()

    def _parse_fps(self, value: str) -> float:
        if not value or value == "N/A":
            return 0.0
        if "/" in value:
            try:
                num_raw, den_raw = value.split("/", 1)
                num = float(num_raw)
                den = float(den_raw)
                if den == 0:
                    return 0.0
                fps = num / den
                return fps if fps > 0 else 0.0
            except Exception:
                return 0.0
        try:
            fps = float(value)
            return fps if fps > 0 else 0.0
        except Exception:
            return 0.0

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
        ffprobe = self.ffprobe
        if not Path(ffprobe).is_file():
            return ""
        cmd = [ffprobe, "-v", "error"]
        if extra_args:
            cmd.extend(extra_args)
        cmd.extend(
            [
                "-select_streams",
                "v:0",
                "-show_entries",
                f"stream={field}",
                "-of",
                "default=nw=1:nk=1",
                str(source),
            ]
        )
        probe = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if probe.returncode != 0:
            return ""
        return probe.stdout.strip().splitlines()[0].strip() if probe.stdout.strip() else ""

    def _probe_duration_seconds(self, source: Path) -> float:
        ffprobe = self.ffprobe
        if not Path(ffprobe).is_file():
            return 0.0
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return 0.0
        line = probe.stdout.strip().splitlines()[0] if probe.stdout.strip() else ""
        try:
            return max(0.0, float(line))
        except Exception:
            return 0.0

    def _resolve_frame_count(self, source: Path) -> FrameCountResult:
        ffprobe = self.ffprobe
        if not Path(ffprobe).is_file():
            return FrameCountResult(300, "fallback", "safe_default", 30.0, 0.0, False)

        # Step A: direct metadata
        nb_frames_raw = self._probe_stream_field(source, "nb_frames")
        avg_fps_raw = self._probe_stream_field(source, "avg_frame_rate")
        real_fps_raw = self._probe_stream_field(source, "r_frame_rate")
        duration_raw = self._probe_stream_field(source, "duration")

        nb_frames = int(nb_frames_raw) if nb_frames_raw.isdigit() else 0
        avg_fps = self._parse_fps(avg_fps_raw)
        real_fps = self._parse_fps(real_fps_raw)
        try:
            duration = float(duration_raw) if duration_raw and duration_raw != "N/A" else 0.0
        except Exception:
            duration = 0.0
        if duration <= 0:
            duration = self._probe_duration_seconds(source)
        fps_for_estimate = avg_fps or real_fps
        is_vfr = avg_fps > 0 and real_fps > 0 and abs(avg_fps - real_fps) > 0.01

        if nb_frames > 1:
            result = FrameCountResult(nb_frames, "exact", "ffprobe_nb_frames", fps_for_estimate, duration, is_vfr)
            logger.debug("Frame count resolved: %s", result)
            return result

        # Step B: counted frames
        counted_raw = self._probe_stream_field(source, "nb_read_frames", ["-count_frames"])
        if counted_raw.isdigit() and int(counted_raw) > 1:
            result = FrameCountResult(int(counted_raw), "counted", "ffprobe_nb_read_frames", fps_for_estimate, duration, is_vfr)
            logger.debug("Frame count resolved: %s", result)
            return result

        # Step C: counted packets fallback
        packet_raw = self._probe_stream_field(source, "nb_read_packets", ["-count_packets"])
        if packet_raw.isdigit() and int(packet_raw) > 1:
            result = FrameCountResult(int(packet_raw), "counted", "ffprobe_nb_read_packets", fps_for_estimate, duration, is_vfr)
            logger.debug("Frame count resolved: %s", result)
            return result

        # Step D: estimate from duration * fps
        if duration > 0 and fps_for_estimate > 0:
            estimate = max(2, int(round(duration * fps_for_estimate)))
            result = FrameCountResult(estimate, "estimated", "duration_times_fps", fps_for_estimate, duration, is_vfr)
            logger.debug("Frame count resolved: %s", result)
            return result

        # Step E: safe fallback
        result = FrameCountResult(300, "fallback", "safe_default", 30.0, duration, is_vfr)
        logger.debug("Frame count resolved: %s", result)
        return result

    def _find_ffmpeg(self) -> str:
        bundled_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        bundled_candidate = bundled_root / "ffmpeg" / "ffmpeg"
        candidates = [
            str(bundled_candidate),
            shutil.which("ffmpeg"),
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/usr/bin/ffmpeg",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if path.is_file() and path.exists():
                return str(path)
        return ""

    def _find_ffprobe(self) -> str:
        ffmpeg_path = Path(self.ffmpeg) if self.ffmpeg else None
        bundled_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        bundled_candidate = bundled_root / "ffmpeg" / "ffprobe"
        candidates = [
            str(bundled_candidate),
            str(ffmpeg_path.with_name("ffprobe")) if ffmpeg_path else "",
            shutil.which("ffprobe"),
            "/opt/homebrew/bin/ffprobe",
            "/usr/local/bin/ffprobe",
            "/usr/bin/ffprobe",
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if path.is_file() and path.exists():
                return str(path)
        return ""

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("AppRoot")
        self.setCentralWidget(central)
        shell_layout = QVBoxLayout(central)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setObjectName("AppScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        shell_layout.addWidget(scroll)

        content = QWidget()
        content.setObjectName("AppCanvas")
        scroll.setWidget(content)

        self.root_layout = QVBoxLayout(content)
        self.root_layout.setContentsMargins(16, 16, 16, 16)
        self.root_layout.setSpacing(18)
        self.root_layout.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)

        self._build_tab_header(self.root_layout, active_index=0)

        self.batch_io_card = self._make_card()
        bio = QVBoxLayout(self.batch_io_card)
        bio.setContentsMargins(24, 20, 24, 20)
        bio.setSpacing(14)
        in_title = QLabel("Input")
        in_title.setObjectName("SectionTitle")
        in_desc = QLabel("Select an input folder or input file.")
        in_desc.setObjectName("SectionDesc")
        in_row = QHBoxLayout()
        in_row.setSpacing(12)
        self.input_path_label = QLabel("No folder selected")
        self.input_path_label.setObjectName("PathField")
        self.input_path_label.setFixedHeight(42)
        self.input_path_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.input_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        in_btn = QPushButton("Add file")
        in_btn.setObjectName("OpenButton")
        in_btn.setFixedWidth(116)
        in_btn.setFixedHeight(42)
        in_btn.clicked.connect(self.choose_input_file)
        in_folder_btn = QPushButton("Add folder")
        in_folder_btn.setObjectName("OpenButton")
        in_folder_btn.setFixedWidth(116)
        in_folder_btn.setFixedHeight(42)
        in_folder_btn.clicked.connect(self.choose_input_folder)
        in_row.addWidget(self.input_path_label, 1)
        in_row.addWidget(in_btn)
        in_row.addWidget(in_folder_btn)
        in_block = QVBoxLayout()
        in_block.setSpacing(8)
        in_block.addWidget(in_title)
        in_block.addWidget(in_desc)
        in_block.addLayout(in_row)
        bio.addLayout(in_block)
        bio.addSpacing(8)

        out_title = QLabel("Output")
        out_title.setObjectName("SectionTitle")
        out_desc = QLabel("Select where output files will be saved.")
        out_desc.setObjectName("SectionDesc")
        out_row = QHBoxLayout()
        out_row.setSpacing(12)
        self.output_path_label = QLabel("No folder selected")
        self.output_path_label.setObjectName("PathField")
        self.output_path_label.setFixedHeight(42)
        self.output_path_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.output_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        out_btn = QPushButton("Add")
        out_btn.setObjectName("OpenButton")
        out_btn.setFixedWidth(116)
        out_btn.setFixedHeight(42)
        out_btn.clicked.connect(self.choose_output_folder)
        out_row.addWidget(self.output_path_label, 1)
        out_row.addWidget(out_btn)
        out_block = QVBoxLayout()
        out_block.setSpacing(8)
        out_block.addWidget(out_title)
        out_block.addWidget(out_desc)
        out_block.addLayout(out_row)
        bio.addLayout(out_block)
        self.root_layout.addWidget(self.batch_io_card)

        self.format_card = self._make_card()
        self.format_layout = QVBoxLayout(self.format_card)
        self.format_layout.setContentsMargins(24, 20, 24, 20)
        self.format_layout.setSpacing(12)
        format_title = QLabel("Options")
        format_title.setObjectName("SectionTitle")
        format_desc = QLabel("Choose one or more formats. Files are saved to format subfolders (JPEG/PNG/TIFF).")
        format_desc.setObjectName("SectionDesc")

        format_row = QHBoxLayout()
        format_row.setSpacing(32)
        format_row.setContentsMargins(0, 14, 0, 14)
        self.jpeg_cb = QCheckBox("JPEG")
        self.jpeg_cb.setChecked(True)
        self.png_cb = QCheckBox("PNG")
        self.tiff_cb = QCheckBox("TIFF")
        self.tiff_cb.stateChanged.connect(self._update_tiff_controls_visibility)
        for checkbox in (self.jpeg_cb, self.png_cb, self.tiff_cb):
            checkbox.setMinimumHeight(24)
            checkbox.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        format_row.addWidget(self.jpeg_cb)
        format_row.addWidget(self.png_cb)
        format_row.addWidget(self.tiff_cb)
        format_row.addStretch(1)
        self.tiff_label = QLabel("TIFF Bit Depth:")
        self.tiff_label.setObjectName("InlineLabel")
        self.tiff_combo = QComboBox()
        self.tiff_combo.addItems(["8-bit", "16-bit (if supported)"])
        self.tiff_combo.setFixedWidth(190)
        self.tiff_combo.setObjectName("TiffCombo")
        format_row.addWidget(self.tiff_label)
        format_row.addWidget(self.tiff_combo)

        quality_label = QLabel("Quality Preset:")
        quality_label.setObjectName("InlineLabel")
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(["High", "Balanced", "Small"])
        self.preset_combo.setCurrentText("High")
        self.preset_combo.setFixedWidth(150)
        format_row.addWidget(quality_label)
        format_row.addWidget(self.preset_combo)

        self.format_layout.addWidget(format_title)
        self.format_layout.addWidget(format_desc)
        self.format_layout.addLayout(format_row)
        self.root_layout.addWidget(self.format_card)
        self._update_tiff_controls_visibility()

        self.action_card = self._make_card()
        action_layout = QVBoxLayout(self.action_card)
        action_layout.setContentsMargins(24, 20, 24, 20)
        action_layout.setSpacing(14)

        top_row = QHBoxLayout()
        top_row.setSpacing(14)
        top_row.setAlignment(Qt.AlignmentFlag.AlignTop)
        left_col = QVBoxLayout()
        left_col.setSpacing(10)
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.action_button = QPushButton("Start")
        self.action_button.setObjectName("PrimaryButton")
        self.action_button.clicked.connect(self.start_processing)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("PrimaryButton")
        self.stop_button.clicked.connect(self.stop_processing)
        self.stop_button.hide()
        self.open_output_button = QPushButton("Open output folder")
        self.open_output_button.setObjectName("OutputButton")
        self.open_output_button.clicked.connect(self.open_output_folder)
        self.open_output_button.hide()

        button_row.addWidget(self.action_button)
        button_row.addWidget(self.stop_button)
        button_row.addWidget(self.open_output_button)

        self.status_label = QLabel("Choose input and output folders to begin.")
        self.status_label.setObjectName("StatusLabel")
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumHeight(22)
        left_col.addLayout(button_row)
        left_col.addWidget(self.status_label)
        left_col.addStretch(1)
        top_row.addLayout(left_col, 3)

        right_preview = QVBoxLayout()
        right_preview.setSpacing(6)
        self.batch_preview = QLabel("Preview not available in batch processing")
        self.batch_preview.setObjectName("PathField")
        self.batch_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.batch_preview.setMinimumHeight(120)
        self.batch_preview.setMinimumWidth(360)
        right_preview.addWidget(self.batch_preview)
        top_row.addLayout(right_preview, 2)

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

        action_layout.addLayout(top_row)
        action_layout.addSpacing(2)
        action_layout.addLayout(progress_header)
        action_layout.addWidget(self.progress_bar)
        self.root_layout.addWidget(self.action_card)

        self._build_unified_advanced_controls()

    def _make_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        return card

    def _build_unified_advanced_controls(self) -> None:
        box = self.format_layout
        box.addSpacing(8)
        box.addWidget(self._label("Single-file settings are available here when processing a selected file.", "SectionDesc"))

        row_a = QGridLayout()
        row_a.setHorizontalSpacing(12)
        row_a.setVerticalSpacing(10)
        self.single_quality = QSpinBox()
        self.single_quality.setRange(1, 12)
        self.single_quality.setValue(12)
        self.single_quality.setFixedWidth(90)
        row_a.addWidget(self._label("Quality (1-12):", "InlineLabel"), 0, 0)
        row_a.addWidget(self.single_quality, 0, 1)
        self.single_bit_depth = QComboBox()
        self.single_bit_depth.addItems(["8-bit", "16-bit", "32-bit"])
        self.single_bit_depth.setFixedWidth(110)
        self.single_bit_depth.setCurrentIndex(0)
        row_a.addWidget(self._label("Bit Depth:", "InlineLabel"), 0, 2)
        row_a.addWidget(self.single_bit_depth, 0, 3)
        self.single_res_preset = QComboBox()
        self.single_res_preset.addItems(["Original", "2160", "1080", "720", "Custom"])
        self.single_res_preset.setFixedWidth(130)
        self.single_res_preset.currentTextChanged.connect(self._update_custom_height_visibility)
        row_a.addWidget(self._label("Resolution:", "InlineLabel"), 0, 4)
        row_a.addWidget(self.single_res_preset, 0, 5)
        self.custom_height_label = self._label("Custom Height:", "InlineLabel")
        self.custom_height = QSpinBox()
        self.custom_height.setRange(64, 8192)
        self.custom_height.setSingleStep(2)
        self.custom_height.setValue(1080)
        self.custom_height.setFixedWidth(120)
        row_a.addWidget(self.custom_height_label, 0, 6)
        row_a.addWidget(self.custom_height, 0, 7)
        row_a.setColumnStretch(8, 1)
        box.addLayout(row_a)

        row_b = QHBoxLayout()
        row_b.setSpacing(10)
        self.frame_index_spin = QSpinBox()
        self.frame_index_spin.setRange(1, 1)
        self.frame_index_spin.setValue(1)
        self.frame_index_spin.setFixedWidth(120)
        row_b.addWidget(self._label("Frame #:", "InlineLabel"))
        row_b.addWidget(self.frame_index_spin)
        row_b.addStretch(1)
        self.frame_count_info = self._label("Frames: unknown", "SectionDesc")
        row_b.addWidget(self.frame_count_info)
        box.addLayout(row_b)

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
        self._update_custom_height_visibility()

        # File options are now consolidated into the same "Options" segment.

    def _build_tab_header(self, layout: QVBoxLayout, active_index: int) -> None:
        header_card = self._make_card()
        header_layout = QHBoxLayout(header_card)
        header_layout.setContentsMargins(22, 20, 22, 20)
        header_layout.setSpacing(16)

        icon = QLabel("❄")
        icon.setObjectName("HeaderIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(76, 76)

        title_wrap = QVBoxLayout()
        title_wrap.setSpacing(2)
        title_label = QLabel("FreezeFrame")
        title_label.setObjectName("Title")
        subtitle_label = QLabel("Extract frames from videos and save them your way.")
        subtitle_label.setObjectName("Subtitle")
        title_wrap.addWidget(title_label)
        title_wrap.addWidget(subtitle_label)

        header_layout.addWidget(icon)
        header_layout.addLayout(title_wrap, 1)
        layout.addWidget(header_card)

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
        self.single_res_preset.addItems(["Original", "2160", "1080", "720", "Custom"])
        self.single_res_preset.currentTextChanged.connect(self._update_custom_height_visibility)
        self.single_res_preset.setFixedWidth(130)
        row1.addWidget(self._label("Resolution:", "InlineLabel"))
        row1.addWidget(self.single_res_preset)

        self.custom_height = QSpinBox()
        self.custom_height.setRange(64, 8192)
        self.custom_height.setSingleStep(2)
        self.custom_height.setValue(1080)
        self.custom_height.setFixedWidth(120)
        self.custom_height_label = self._label("Custom Height:", "InlineLabel")
        row1.addWidget(self.custom_height_label)
        row1.addWidget(self.custom_height)
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
        self.preview_image.setObjectName("PathField")
        self.preview_image.setMinimumHeight(130)
        self.preview_image.setMaximumHeight(170)
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
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

        self._update_custom_height_visibility()

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
        self.setStyleSheet(
            """
            QMainWindow {
              background-color: #102635;
            }
            #AppRoot, #AppCanvas, #AppScroll, #AppScroll > QWidget, QScrollArea {
              background-color: #102635;
            }
            QTabWidget::pane {
              border: 1px solid #1F2A44;
              border-radius: 12px;
              top: -1px;
              background: #0A1220;
            }
            QTabBar::tab {
              background: #13233A;
              color: #9FB4D2;
              border: 1px solid #2A3E5E;
              border-bottom: none;
              border-top-left-radius: 10px;
              border-top-right-radius: 10px;
              min-width: 120px;
              padding: 8px 14px;
              margin-right: 6px;
            }
            QTabBar::tab:selected {
              background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #1FD0B2, stop:1 #2E7BFF
              );
              color: #F3FBFF;
              border-color: #2E7BFF;
            }
            QTabBar::tab:hover:!selected {
              background: #1B3352;
              color: #D5E5FF;
            }
            #Card {
              border: 1px solid #1F2A44;
              border-radius: 16px;
              background-color: #0E1624;
            }
            #HeaderIcon {
              border: 1px solid #1E90FF;
              border-radius: 14px;
              background-color: #0A2038;
              color: #1EC8FF;
              font-size: 28px;
            }
            #Title { font-size: 22px; font-weight: 700; color: #F3F7FF; }
            #Subtitle { font-size: 12px; color: #9DB0CC; }
            #SectionTitle { font-size: 15px; font-weight: 650; color: #ECF2FF; }
            #SectionDesc { font-size: 12px; color: #96A7C2; }
            #InlineLabel { color: #DCE8FF; }
            #PathField {
              border: 1px solid #223350;
              border-radius: 12px;
              padding: 8px 12px;
              background-color: #122039;
              color: #DCE8FF;
              font-size: 12px;
            }
            #StatusLabel { color: #9DB0CC; font-size: 12px; }
            #ProgressTitle { font-size: 15px; font-weight: 650; color: #ECF2FF; }
            #ProgressPct { font-size: 15px; font-weight: 650; color: #9DB0CC; }
            QPushButton {
              border: 1px solid #2A3E5E;
              border-radius: 12px;
              padding: 8px 14px;
              font-size: 12px;
              font-weight: 600;
              color: #E7EFFF;
              background-color: #18263D;
            }
            QPushButton:hover {
              background-color: #213553;
              border-color: #35629A;
            }
            QPushButton:pressed {
              background-color: #122034;
              border-color: #1F4F85;
            }
            QPushButton:disabled {
              color: #8CA0BE;
              border-color: #2A3E5E;
              background-color: #1A2436;
            }
            QPushButton#PrimaryButton {
              min-height: 54px;
              font-size: 14px;
              color: #F4FBFF;
              border: 1px solid #1AAFE2;
              background-color: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #1FD0B2, stop:1 #2E7BFF
              );
            }
            QPushButton#PrimaryButton:hover {
              background-color: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #2ADDC0, stop:1 #3D8AFF
              );
            }
            QPushButton#PrimaryButton:pressed {
              background-color: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #16B99D, stop:1 #2367D8
              );
            }
            QPushButton#PrimaryButton:disabled {
              color: #8CA0BE;
              border: 1px solid #2A3E5E;
              background-color: #1A2436;
            }
            QPushButton#OutputButton {
              min-height: 54px;
              font-size: 14px;
              color: #F3FFF8;
              border: 1px solid #1EBA78;
              background-color: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #27D491, stop:1 #16A34A
              );
            }
            QPushButton#OutputButton:hover {
              background-color: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #35E2A1, stop:1 #1DB857
              );
            }
            QPushButton#OutputButton:pressed {
              background-color: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #1EBD7F, stop:1 #13863D
              );
            }
            QPushButton#OpenButton, QPushButton#SecondaryButton {
              min-height: 32px;
              font-size: 12px;
              padding: 6px 12px;
            }
            QPushButton#HeaderTabButton {
              min-height: 30px;
              min-width: 96px;
              font-size: 12px;
              border-radius: 10px;
              padding: 6px 12px;
              background: #142843;
              border: 1px solid #2A3E5E;
              color: #AFC4E1;
            }
            QPushButton#HeaderTabButton:checked {
              background-color: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #1FD0B2, stop:1 #2E7BFF
              );
              color: #F6FCFF;
              border-color: #2E7BFF;
            }
            QCheckBox {
              font-size: 12px;
              spacing: 8px;
              color: #EAF1FF;
              min-height: 22px;
            }
            QCheckBox::indicator {
              width: 16px;
              height: 16px;
              border: 1px solid #3C5E8D;
              border-radius: 4px;
              background-color: #10203A;
            }
            QCheckBox::indicator:checked {
              border: 1px solid #22C9B3;
              background-color: #1AC7AE;
              image: none;
            }
            QComboBox {
              border: 1px solid #2A3E5E;
              border-radius: 12px;
              padding: 2px 10px;
              min-height: 24px;
              font-size: 12px;
              background-color: #14243D;
              color: #EAF1FF;
            }
            QSpinBox {
              border: 1px solid #2A3E5E;
              border-radius: 12px;
              padding: 2px 8px;
              min-height: 24px;
              font-size: 12px;
              background-color: #14243D;
              color: #EAF1FF;
              selection-background-color: #2668D5;
            }
            QSpinBox::up-button, QSpinBox::down-button {
              width: 16px;
              border: none;
              background: #1A2D49;
            }
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {
              background: #213553;
            }
            QComboBox:hover {
              border-color: #3A6AA8;
              background-color: #1A2D49;
            }
            QComboBox::drop-down {
              border: none;
              width: 22px;
            }
            QComboBox QAbstractItemView {
              background-color: #152744;
              color: #EAF1FF;
              border: 1px solid #2D476D;
              border-radius: 10px;
              padding: 4px;
              selection-background-color: #2668D5;
              outline: 0;
            }
            QLabel { font-size: 12px; color: #DCE8FF; }
            QSlider::groove:horizontal {
              border: 1px solid #2A3E5E;
              height: 6px;
              border-radius: 4px;
              background: #111E35;
            }
            QSlider::sub-page:horizontal {
              border-radius: 4px;
              background: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #1FD0B2, stop:1 #2E7BFF
              );
            }
            QSlider::handle:horizontal {
              background: #DCE8FF;
              border: 1px solid #2A3E5E;
              width: 14px;
              margin: -5px 0;
              border-radius: 7px;
            }
            QProgressBar {
              border: 1px solid #2A3E5E;
              border-radius: 9px;
              background-color: #111E35;
              min-height: 18px;
            }
            QProgressBar::chunk {
              border-radius: 8px;
              background-color: qlineargradient(
                x1:0, y1:0, x2:1, y2:0,
                stop:0 #1FD0B2, stop:1 #2E7BFF
              );
            }
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
        self.batch_preview.setPixmap(QPixmap())
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
        self.generate_preview()

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
        self.batch_preview.setPixmap(QPixmap())
        self.batch_preview.setText("Preview not available in batch processing")

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

    def _update_custom_height_visibility(self) -> None:
        show = self.single_res_preset.currentText() == "Custom"
        self.custom_height_label.setVisible(show)
        self.custom_height.setVisible(show)

    def _update_single_bit_depth_support(self) -> None:
        path_text = self.input_path_label.text().strip()
        if not path_text or path_text == "No folder selected":
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
        if hasattr(self, "preview_image") and self.preview_image.isVisible():
            return self.preview_image
        return self.batch_preview

    def _display_preview_pixmap(self, image_path: str) -> None:
        label = self._active_preview_label()
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            label.setText("Preview unavailable for selected frame")
            return
        scaled = pixmap.scaled(
            label.width() - 20,
            label.height() - 20,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        label.setPixmap(scaled)
        label.setText("")

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
        if hasattr(self, "single_file_path"):
            single_path_text = self.single_file_path.text().strip()
            if single_path_text and single_path_text != "No file selected":
                path_text = single_path_text
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
        ffprobe = self.ffprobe
        if not Path(ffprobe).is_file():
            return False
        probe = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=pix_fmt,bits_per_raw_sample",
                "-of",
                "default=nw=1:nk=1",
                str(source),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if probe.returncode != 0:
            return False
        lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
        for line in lines:
            if line.isdigit() and int(line) > 8:
                return True
        pix_fmt = lines[0] if lines else ""
        if re.search(r"p(10|12|14|16)(le|be)?$", pix_fmt):
            return True
        if re.search(r"(rgb|bgr|gbr)p?(30|36|48|64)(le|be)?$", pix_fmt):
            return True
        if re.search(r"gray(10|12|14|16)(le|be)?$", pix_fmt):
            return True
        return False

    def _update_tiff_controls_visibility(self) -> None:
        visible = self.tiff_cb.isChecked()
        self.tiff_label.setVisible(visible)
        self.tiff_combo.setVisible(visible)

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
            quality_preset=self.preset_combo.currentText(),
            tiff_bit_depth=self.tiff_combo.currentText(),
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
            subprocess.run(["open", path], check=False)

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
        if hasattr(self, "single_file_path"):
            single_path_text = self.single_file_path.text().strip()
            if single_path_text and single_path_text != "No file selected":
                path_text = single_path_text

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
    font = QFont("SF Pro Display")
    app.setFont(font)
    window = FreezeFrameWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
