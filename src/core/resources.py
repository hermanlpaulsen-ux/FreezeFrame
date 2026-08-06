from __future__ import annotations

import sys
from pathlib import Path


def resource_path(relative_path: str) -> Path:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base_dir / relative_path


def spin_icon_paths(theme: str) -> tuple[str, str]:
    suffix = "dark" if theme == "dark" else "light"
    up = resource_path(f"assets/spin-up-{suffix}.svg").as_posix()
    down = resource_path(f"assets/spin-down-{suffix}.svg").as_posix()
    return up, down
