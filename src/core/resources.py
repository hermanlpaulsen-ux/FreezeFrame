from __future__ import annotations

import sys
from pathlib import Path


def resource_path(relative_path: str) -> Path:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base_dir / relative_path


SPIN_UP_ICON = resource_path("assets/spin-up.svg").as_posix()
SPIN_DOWN_ICON = resource_path("assets/spin-down.svg").as_posix()
