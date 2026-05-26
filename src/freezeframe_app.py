#!/usr/bin/env python3

import re
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mkv", ".avi"}


class FrameExportWorker(QThread):
    progress_updated = Signal(int, int, int)
    finished_with_result = Signal(int, int, str, list, bool)

    def __init__(
        self,
        ffmpeg: str,
        files: list[Path],
        output_dir: Path,
        selected_formats: list[tuple[str, str]],
        quality_preset: str,
        tiff_bit_depth: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.files = files
        self.output_dir = output_dir
        self.selected_formats = selected_formats
        self.quality_preset = quality_preset
        self.tiff_bit_depth = tiff_bit_depth
        self._stop_requested = False
        self._active_process: subprocess.Popen | None = None

    def request_stop(self) -> None:
        self._stop_requested = True
        if self._active_process and self._active_process.poll() is None:
            try:
                self._active_process.terminate()
            except Exception:
                pass

    def _source_supports_16_bit(self, source: Path) -> bool:
        ffprobe = str(Path(self.ffmpeg).with_name("ffprobe"))
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

    def _build_export_command(self, source: Path, target: Path, ext: str) -> list[str]:
        if ext == "tiff":
            source_supports_16_bit = self._source_supports_16_bit(source)
            use_tiff_16_bit = self.tiff_bit_depth.startswith("16-bit") and source_supports_16_bit
            output_format = "rgb48le" if use_tiff_16_bit else "rgb24"
        elif ext == "png":
            output_format = "rgb24"
        else:
            output_format = "yuvj420p"

        cmd = [
            self.ffmpeg,
            "-y",
            "-hwaccel",
            "none",
            "-i",
            str(source),
            "-vf",
            f"select=eq(n\\,0),format={output_format}",
            "-vframes",
            "1",
        ]
        if ext == "jpg":
            jpeg_quality_map = {"High": "2", "Balanced": "5", "Small": "9"}
            cmd.extend(["-q:v", jpeg_quality_map.get(self.quality_preset, "5")])
        elif ext == "png":
            png_compression_map = {"High": "2", "Balanced": "5", "Small": "9"}
            cmd.extend(["-compression_level", png_compression_map.get(self.quality_preset, "5")])
        elif ext == "tiff":
            tiff_compression_map = {"High": "lzw", "Balanced": "deflate", "Small": "zlib"}
            cmd.extend(["-compression_algo", tiff_compression_map.get(self.quality_preset, "deflate")])
        cmd.append(str(target))
        return cmd

    def run(self) -> None:
        total = len(self.files) * len(self.selected_formats)
        completed = 0
        failed: list[str] = []

        for file in self.files:
            if self._stop_requested:
                break
            for ext, folder_name in self.selected_formats:
                if self._stop_requested:
                    break
                preset_suffix = self.quality_preset.strip().lower().replace(" ", "-")
                target = self.output_dir / folder_name / f"{file.stem}_{preset_suffix}.{ext}"
                cmd = self._build_export_command(file, target, ext)
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._active_process = proc
                rc = proc.wait()
                self._active_process = None

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
            self._stop_requested,
        )


class FreezeFrameWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("FreezeFrame")
        self.resize(1200, 850)
        self.setMinimumSize(1040, 760)

        self.ffmpeg = self._find_ffmpeg()
        self.worker: FrameExportWorker | None = None
        self.is_processing = False
        self.output_manually_set = False
        self.last_output_dir = ""

        self._build_ui()
        self._apply_style()

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

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        self.root_layout = QVBoxLayout(central)
        self.root_layout.setContentsMargins(28, 24, 28, 24)
        self.root_layout.setSpacing(14)

        self.header_card = self._make_card()
        header_layout = QHBoxLayout(self.header_card)
        header_layout.setContentsMargins(22, 20, 22, 20)
        header_layout.setSpacing(16)

        icon = QLabel("❄")
        icon.setObjectName("HeaderIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(76, 76)

        title_wrap = QVBoxLayout()
        title_wrap.setSpacing(2)
        self.title_label = QLabel("FreezeFrame")
        self.title_label.setObjectName("Title")
        self.subtitle_label = QLabel("Extract frames from videos and save them your way.")
        self.subtitle_label.setObjectName("Subtitle")
        title_wrap.addWidget(self.title_label)
        title_wrap.addWidget(self.subtitle_label)

        header_layout.addWidget(icon)
        header_layout.addLayout(title_wrap, 1)
        self.root_layout.addWidget(self.header_card)

        self.input_card, self.input_path_label = self._build_folder_card(
            "Input Folder",
            "Select the folder containing the input files.",
            "Add",
            self.choose_input_folder,
        )
        self.root_layout.addWidget(self.input_card)

        self.output_card, self.output_path_label = self._build_folder_card(
            "Output Folder",
            "Select where output files will be saved.",
            "Add",
            self.choose_output_folder,
        )
        self.root_layout.addWidget(self.output_card)

        self.format_card = self._make_card()
        format_layout = QVBoxLayout(self.format_card)
        format_layout.setContentsMargins(22, 18, 22, 18)
        format_layout.setSpacing(10)
        format_title = QLabel("Output Formats")
        format_title.setObjectName("SectionTitle")
        format_desc = QLabel("Choose one or more formats. Files are saved to format subfolders (JPEG/PNG/TIFF).")
        format_desc.setObjectName("SectionDesc")

        format_row = QHBoxLayout()
        format_row.setSpacing(28)
        format_row.setContentsMargins(0, 12, 0, 12)
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

        format_layout.addWidget(format_title)
        format_layout.addWidget(format_desc)
        format_layout.addLayout(format_row)
        self.root_layout.addWidget(self.format_card)
        self._update_tiff_controls_visibility()

        self.action_card = self._make_card()
        action_layout = QVBoxLayout(self.action_card)
        action_layout.setContentsMargins(22, 18, 22, 18)
        action_layout.setSpacing(10)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
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

        button_row.addWidget(self.action_button, 1)
        button_row.addWidget(self.stop_button, 1)
        button_row.addWidget(self.open_output_button, 1)

        self.status_label = QLabel("Choose input and output folders to begin.")
        self.status_label.setObjectName("StatusLabel")

        action_layout.addLayout(button_row)
        action_layout.addWidget(self.status_label)
        self.root_layout.addWidget(self.action_card)

        self.progress_card = self._make_card()
        progress_layout = QVBoxLayout(self.progress_card)
        progress_layout.setContentsMargins(22, 16, 22, 16)
        progress_layout.setSpacing(10)

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

        progress_layout.addLayout(progress_header)
        progress_layout.addWidget(self.progress_bar)
        self.root_layout.addWidget(self.progress_card)

    def _make_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("Card")
        return card

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
              background-color: #0A0F1A;
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

    def choose_input_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose input folder")
        if not selected:
            return
        input_dir = str(Path(selected))
        self.input_path_label.setText(input_dir)
        if not self.output_manually_set:
            self.output_path_label.setText(str(Path(input_dir) / "Stills"))
        self._reset_post_run_state()
        self.status_label.setText("Input folder changed. Ready to start.")

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

        input_dir = Path(input_text)
        if not input_dir.is_dir():
            QMessageBox.critical(self, "Invalid input folder", "Choose a valid input folder.")
            return

        output_dir = Path(output_text) if output_text else (input_dir / "Stills")
        if output_dir == input_dir:
            output_dir = input_dir / "Stills"
            self.output_path_label.setText(str(output_dir))
        output_dir.mkdir(parents=True, exist_ok=True)

        selected_formats = self._selected_formats()
        if not selected_formats:
            QMessageBox.critical(self, "No format selected", "Choose at least one output format: JPEG, PNG, or TIFF.")
            return

        for _, folder_name in selected_formats:
            (output_dir / folder_name).mkdir(parents=True, exist_ok=True)

        files = self._collect_files(input_dir)
        if not files:
            QMessageBox.warning(self, "No video files", "No supported video files found in input folder.")
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
            files=files,
            output_dir=output_dir,
            selected_formats=selected_formats,
            quality_preset=self.preset_combo.currentText(),
            tiff_bit_depth=self.tiff_combo.currentText(),
            parent=self,
        )
        self.worker.progress_updated.connect(self._on_progress)
        self.worker.finished_with_result.connect(self._on_finished)
        self.worker.start()

    def _on_progress(self, completed: int, total: int, pct: int) -> None:
        self.progress_bar.setValue(pct)
        self.progress_pct.setText(f"{pct}%")
        self.status_label.setText(f"Processing {completed}/{total}...")

    def _on_finished(self, total: int, completed: int, output_dir: str, failed: list, cancelled: bool) -> None:
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

        if failed:
            self.status_label.setText(f"Done with errors. Exported {completed - len(failed)}/{total} files.")
            QMessageBox.warning(self, "Completed with errors", f"{len(failed)} file(s) failed:\n" + "\n".join(failed[:15]))
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

    def closeEvent(self, event: QCloseEvent) -> None:
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
