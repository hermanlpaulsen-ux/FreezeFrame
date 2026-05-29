from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

from core.models import FrameCountResult


def find_ffmpeg() -> str:
    bundled_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
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


def find_ffprobe(ffmpeg_path: str) -> str:
    ffmpeg = Path(ffmpeg_path) if ffmpeg_path else None
    bundled_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    bundled_candidate = bundled_root / "ffmpeg" / "ffprobe"
    candidates = [
        str(bundled_candidate),
        str(ffmpeg.with_name("ffprobe")) if ffmpeg else "",
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


def parse_fps(value: str) -> float:
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


def probe_stream_field(ffprobe: str, source: Path, field: str, extra_args: list[str] | None = None) -> str:
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


def probe_duration_seconds(ffprobe: str, source: Path) -> float:
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


def resolve_frame_count(ffprobe: str, source: Path) -> FrameCountResult:
    if not Path(ffprobe).is_file():
        return FrameCountResult(300, "fallback", "safe_default", 30.0, 0.0, False)

    nb_frames_raw = probe_stream_field(ffprobe, source, "nb_frames")
    avg_fps_raw = probe_stream_field(ffprobe, source, "avg_frame_rate")
    real_fps_raw = probe_stream_field(ffprobe, source, "r_frame_rate")
    duration_raw = probe_stream_field(ffprobe, source, "duration")

    nb_frames = int(nb_frames_raw) if nb_frames_raw.isdigit() else 0
    avg_fps = parse_fps(avg_fps_raw)
    real_fps = parse_fps(real_fps_raw)
    try:
        duration = float(duration_raw) if duration_raw and duration_raw != "N/A" else 0.0
    except Exception:
        duration = 0.0
    if duration <= 0:
        duration = probe_duration_seconds(ffprobe, source)

    fps_for_estimate = avg_fps or real_fps
    is_vfr = avg_fps > 0 and real_fps > 0 and abs(avg_fps - real_fps) > 0.01

    if nb_frames > 1:
        return FrameCountResult(nb_frames, "exact", "ffprobe_nb_frames", fps_for_estimate, duration, is_vfr)

    counted_raw = probe_stream_field(ffprobe, source, "nb_read_frames", ["-count_frames"])
    if counted_raw.isdigit() and int(counted_raw) > 1:
        return FrameCountResult(int(counted_raw), "counted", "ffprobe_nb_read_frames", fps_for_estimate, duration, is_vfr)

    packet_raw = probe_stream_field(ffprobe, source, "nb_read_packets", ["-count_packets"])
    if packet_raw.isdigit() and int(packet_raw) > 1:
        return FrameCountResult(int(packet_raw), "counted", "ffprobe_nb_read_packets", fps_for_estimate, duration, is_vfr)

    if duration > 0 and fps_for_estimate > 0:
        estimate = max(2, int(round(duration * fps_for_estimate)))
        return FrameCountResult(estimate, "estimated", "duration_times_fps", fps_for_estimate, duration, is_vfr)

    return FrameCountResult(300, "fallback", "safe_default", 30.0, duration, is_vfr)


def source_supports_16_bit(ffprobe: str, source: Path) -> bool:
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


def source_timing(ffprobe: str, source: Path) -> tuple[float, bool]:
    if not Path(ffprobe).is_file():
        return (0.0, False)
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
        fps_raw = probe_stream_field(ffprobe, source, "r_frame_rate")
        return (parse_fps(fps_raw), False)
    lines = [line.strip() for line in probe.stdout.splitlines() if line.strip()]
    avg_fps = parse_fps(lines[0]) if len(lines) > 0 else 0.0
    real_fps = parse_fps(lines[1]) if len(lines) > 1 else 0.0
    fps = avg_fps or real_fps
    is_vfr = avg_fps > 0 and real_fps > 0 and abs(avg_fps - real_fps) > 0.01
    return (fps, is_vfr)
