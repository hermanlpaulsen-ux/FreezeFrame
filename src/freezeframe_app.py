#!/usr/bin/env python3

import shutil
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mkv", ".avi"}


class FirstFrameApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("FreezeFrame")
        self.root.geometry("1120x760")
        self.root.minsize(1040, 700)

        self.ffmpeg = self._find_ffmpeg()
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Choose input and output folders to begin.")
        self.progress_text_var = tk.StringVar(value="0%")
        self.progress_value = tk.DoubleVar(value=0.0)

        self.output_manually_set = False
        self.last_output_dir = ""
        self.is_processing = False
        self.active_processes: list[subprocess.Popen] = []
        self.process_lock = threading.Lock()
        self.stop_requested = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

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
        style = ttk.Style()
        style.theme_use("aqua")
        style.configure("SectionTitle.TLabel", font=("SF Pro Display", 30, "bold"))
        style.configure("CardHeader.TLabel", font=("SF Pro Display", 24, "bold"))
        style.configure("Body.TLabel", font=("SF Pro Text", 15))
        style.configure("Action.TButton", font=("SF Pro Text", 15, "bold"))
        style.configure("Browse.TButton", font=("SF Pro Text", 14, "bold"))

        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        top_card = ttk.Frame(outer, padding=(22, 18))
        top_card.pack(fill="x")
        ttk.Label(top_card, text="FreezeFrame", style="SectionTitle.TLabel").pack(anchor="w")

        self._folder_card(outer, "Input Folder", "Select the folder containing the input files.", self.input_var, self.choose_input_folder, "Open...")
        self._folder_card(outer, "Output Folder", "Select where output JPEG files will be saved.", self.output_var, self.choose_output_folder, "Open...")

        footer = ttk.Frame(outer, padding=(0, 14, 0, 0))
        footer.pack(fill="x", expand=True)
        action_row = ttk.Frame(footer)
        action_row.pack(fill="x", pady=(0, 8))

        self.action_button = ttk.Button(action_row, text="Start", style="Action.TButton", command=self.start_processing, width=22)
        self.action_button.pack(side="left")
        self.open_output_button = ttk.Button(action_row, text="Open output folder", style="Action.TButton", command=self.open_output_folder, width=22)
        self.open_output_button.pack_forget()

        ttk.Label(footer, textvariable=self.status_var, style="Body.TLabel").pack(fill="x", pady=(6, 16))
        progress_row = ttk.Frame(footer)
        progress_row.pack(fill="x")
        ttk.Label(progress_row, text="Progress", style="CardHeader.TLabel").pack(side="left")
        ttk.Label(progress_row, textvariable=self.progress_text_var, style="Body.TLabel").pack(side="right")
        self.progress = ttk.Progressbar(footer, variable=self.progress_value, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(10, 0))

    def _folder_card(self, parent, title, helper, variable, command, button_text) -> None:
        card = ttk.Frame(parent, padding=(20, 18))
        card.pack(fill="x", pady=(16, 0))
        top_row = ttk.Frame(card)
        top_row.pack(fill="x")
        text_block = ttk.Frame(top_row)
        text_block.pack(side="left", fill="both", expand=True, padx=(0, 18))
        ttk.Label(text_block, text=title, style="CardHeader.TLabel").pack(anchor="w")
        ttk.Label(text_block, text=helper, style="Body.TLabel").pack(anchor="w", pady=(4, 10))
        ttk.Button(top_row, text=button_text, style="Browse.TButton", command=command, width=11).pack(side="right", pady=(8, 0))
        ttk.Entry(card, textvariable=variable).pack(fill="x", pady=(6, 0), ipady=8)

    def _show_open_output_button(self, show: bool) -> None:
        if show:
            self.open_output_button.pack(side="left", padx=(10, 0))
        else:
            self.open_output_button.pack_forget()

    def _reset_post_run_state(self) -> None:
        self.last_output_dir = ""
        self.action_button.configure(text="Start", command=self.start_processing, state="normal")
        self._show_open_output_button(False)

    def choose_input_folder(self) -> None:
        selected = filedialog.askdirectory(title="Choose input folder")
        if not selected:
            return
        input_dir = str(Path(selected))
        self.input_var.set(input_dir)
        if not self.output_manually_set:
            self.output_var.set(str(Path(input_dir) / "Stills"))
        self._reset_post_run_state()
        self.status_var.set("Input folder changed. Ready to start.")

    def choose_output_folder(self) -> None:
        initial = self.output_var.get().strip() or self.input_var.get().strip() or str(Path.home())
        selected = filedialog.askdirectory(title="Choose output folder", initialdir=initial if Path(initial).is_dir() else str(Path.home()), mustexist=False)
        if not selected:
            return
        self.output_var.set(str(Path(selected)))
        self.output_manually_set = True
        self._reset_post_run_state()
        self.status_var.set("Output folder changed. Ready to start.")

    def _collect_files(self, input_dir: Path) -> list[Path]:
        files = [f for f in input_dir.iterdir() if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS]
        files.sort(key=lambda p: p.name.lower())
        return files

    def _set_busy_mode(self) -> None:
        self.action_button.configure(text="Processing...", state="disabled")
        self._show_open_output_button(False)

    def _set_done_mode(self) -> None:
        self.action_button.configure(text="Restart", command=self.start_processing, state="normal")
        output_path = Path(self.last_output_dir) if self.last_output_dir else None
        self._show_open_output_button(bool(output_path and output_path.is_dir()))

    def start_processing(self) -> None:
        if self.is_processing:
            return
        if not self.ffmpeg:
            messagebox.showerror("Missing ffmpeg", "ffmpeg was not found in app bundle or system paths.")
            return
        input_dir = Path(self.input_var.get().strip())
        output_text = self.output_var.get().strip()
        if not input_dir.is_dir():
            messagebox.showerror("Invalid input folder", "Choose a valid input folder.")
            return
        output_dir = Path(output_text) if output_text else (input_dir / "Stills")
        if output_dir == input_dir:
            output_dir = input_dir / "Stills"
            self.output_var.set(str(output_dir))
        output_dir.mkdir(parents=True, exist_ok=True)
        files = self._collect_files(input_dir)
        if not files:
            messagebox.showwarning("No video files", "No supported video files found in input folder.")
            return
        self.is_processing = True
        self.stop_requested = False
        self.last_output_dir = ""
        self.progress_value.set(0)
        self.progress_text_var.set("0%")
        self.status_var.set(f"Processing 0/{len(files)}...")
        self._set_busy_mode()
        threading.Thread(target=self._process_files, args=(files, output_dir), daemon=True).start()

    def _terminate_active_processes(self) -> None:
        with self.process_lock:
            processes = list(self.active_processes)
        for proc in processes:
            try:
                if proc.poll() is None:
                    proc.terminate()
            except Exception:
                pass

    def _process_files(self, files: list[Path], output_dir: Path) -> None:
        total = len(files)
        completed = 0
        failed: list[str] = []
        for file in files:
            if self.stop_requested:
                break
            target = output_dir / f"{file.stem}.jpg"
            cmd = [
                self.ffmpeg,
                "-y",
                "-hwaccel",
                "none",
                "-i",
                str(file),
                "-vf",
                "select=eq(n\\,0),format=yuvj420p",
                "-vframes",
                "1",
                "-q:v",
                "2",
                str(target),
            ]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self.process_lock:
                self.active_processes.append(proc)
            rc = proc.wait()
            with self.process_lock:
                if proc in self.active_processes:
                    self.active_processes.remove(proc)
            if self.stop_requested:
                break
            if rc != 0:
                failed.append(file.name)
            completed += 1
            pct = int((completed / total) * 100)
            self.root.after(0, self._update_progress, completed, total, pct)
        self.root.after(0, self._finish, total, completed, output_dir, failed, self.stop_requested)

    def _update_progress(self, completed: int, total: int, pct: int) -> None:
        self.progress_value.set(pct)
        self.progress_text_var.set(f"{pct}%")
        self.status_var.set(f"Processing {completed}/{total}...")

    def _finish(self, total: int, completed: int, output_dir: Path, failed: list[str], cancelled: bool) -> None:
        self.is_processing = False
        self.last_output_dir = str(output_dir)
        if cancelled:
            self.status_var.set("Processing cancelled.")
            self.progress_text_var.set(f"{int((completed / total) * 100) if total else 0}%")
            self.action_button.configure(text="Start", command=self.start_processing, state="normal")
            self._show_open_output_button(False)
            return
        self.progress_value.set(100)
        self.progress_text_var.set("100%")
        self._set_done_mode()
        if failed:
            self.status_var.set(f"Done with errors. Exported {completed - len(failed)}/{total} files.")
            messagebox.showwarning("Completed with errors", f"{len(failed)} file(s) failed:\n" + "\n".join(failed[:15]))
            return
        self.status_var.set(f"Done. Exported {completed} JPEG file(s).")
        messagebox.showinfo("Completed", f"Exported {completed} JPEG file(s) to:\n{output_dir}")

    def open_output_folder(self) -> None:
        path = self.last_output_dir or self.output_var.get().strip()
        if path and Path(path).is_dir():
            subprocess.run(["open", path], check=False)

    def on_close(self) -> None:
        if self.is_processing:
            should_close = messagebox.askyesno("Processing in progress", "A conversion is still running.\n\nClose and stop the current process?")
            if not should_close:
                return
            self.stop_requested = True
            self._terminate_active_processes()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    FirstFrameApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
