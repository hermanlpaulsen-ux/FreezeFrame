from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _external_command_env() -> dict[str, str]:
    env = os.environ.copy()

    # PyInstaller can modify loader/plugin env vars; restore system defaults
    # before launching desktop tools like xdg-open/kde-open.
    ld_orig = env.get("LD_LIBRARY_PATH_ORIG")
    if ld_orig is not None:
        env["LD_LIBRARY_PATH"] = ld_orig
    else:
        env.pop("LD_LIBRARY_PATH", None)

    env.pop("QT_PLUGIN_PATH", None)
    env.pop("QML2_IMPORT_PATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def open_in_file_manager(path: str | Path) -> bool:
    target = Path(path)
    if not target.exists():
        return False

    try:
        if sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
            return True
        if sys.platform.startswith("linux"):
            subprocess.run(["xdg-open", str(target)], check=False, env=_external_command_env())
            return True
        if os.name == "nt":
            os.startfile(str(target))
            return True
    except Exception:
        return False

    return False
