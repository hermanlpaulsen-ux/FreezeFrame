from __future__ import annotations

from dataclasses import dataclass


VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v", ".mkv", ".avi"}


@dataclass
class FrameCountResult:
    frame_count: int
    confidence: str  # exact | counted | estimated | fallback
    source: str
    fps: float = 0.0
    duration_seconds: float = 0.0
    is_variable_frame_rate: bool = False
