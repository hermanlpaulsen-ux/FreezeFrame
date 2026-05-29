#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="FreezeFrame"
ENTRYPOINT="${ROOT_DIR}/src/freezeframe_app.py"
ICON_PNG="${ROOT_DIR}/src/assets/FreezeFrame_icon_1024.png"

if [[ ! -f "${ENTRYPOINT}" ]]; then
  echo "Missing entrypoint: ${ENTRYPOINT}"
  exit 1
fi

FFMPEG_CANDIDATES=(
  "$(command -v ffmpeg 2>/dev/null || true)"
  "/usr/bin/ffmpeg"
  "/usr/local/bin/ffmpeg"
  "/snap/bin/ffmpeg"
)
FFPROBE_CANDIDATES=(
  "$(command -v ffprobe 2>/dev/null || true)"
  "/usr/bin/ffprobe"
  "/usr/local/bin/ffprobe"
  "/snap/bin/ffprobe"
)

FFMPEG_BIN=""
FFPROBE_BIN=""
for candidate in "${FFMPEG_CANDIDATES[@]}"; do
  if [[ -n "${candidate}" && -x "${candidate}" ]]; then
    FFMPEG_BIN="${candidate}"
    break
  fi
done
for candidate in "${FFPROBE_CANDIDATES[@]}"; do
  if [[ -n "${candidate}" && -x "${candidate}" ]]; then
    FFPROBE_BIN="${candidate}"
    break
  fi
done

if [[ -z "${FFMPEG_BIN}" || -z "${FFPROBE_BIN}" ]]; then
  echo "Error: ffmpeg and ffprobe must be available at build time."
  echo "The Linux build intentionally embeds both binaries so end users do not need to install them."
  echo "Resolved ffmpeg: ${FFMPEG_BIN:-<missing>}"
  echo "Resolved ffprobe: ${FFPROBE_BIN:-<missing>}"
  exit 1
fi

PYI_ARGS=(
  --windowed
  --noconfirm
  --onedir
  --name "${APP_NAME}"
  --exclude-module unittest
  --exclude-module test
  --exclude-module tests
  --exclude-module pydoc
  --exclude-module doctest
  --exclude-module distutils
  --exclude-module setuptools
  --exclude-module pip
  --exclude-module wheel
  --exclude-module tkinter.test
  --exclude-module asyncio
  --exclude-module concurrent
  --exclude-module multiprocessing
  --exclude-module xmlrpc
  --exclude-module http
  --exclude-module email
  --exclude-module zoneinfo
  --exclude-module sqlite3
  --exclude-module ssl
)

if [[ -f "${ICON_PNG}" ]]; then
  PYI_ARGS+=(--icon "${ICON_PNG}")
fi
if [[ -d "${ROOT_DIR}/src/assets" ]]; then
  PYI_ARGS+=(--add-data "${ROOT_DIR}/src/assets:assets")
fi

echo "Bundling ffmpeg from: ${FFMPEG_BIN}"
echo "Bundling ffprobe from: ${FFPROBE_BIN}"
PYI_ARGS+=(--add-binary "${FFMPEG_BIN}:ffmpeg")
PYI_ARGS+=(--add-binary "${FFPROBE_BIN}:ffmpeg")

cd "${ROOT_DIR}"
PYTHON_BIN=""
if [[ -x "${ROOT_DIR}/.uvenv/bin/python" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.uvenv/bin/python"
elif [[ -x "${ROOT_DIR}/.venv313/bin/python" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.venv313/bin/python"
elif [[ -x "${ROOT_DIR}/.venv312/bin/python" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.venv312/bin/python"
else
  PYTHON_BIN="python3"
fi

echo "Using Python: ${PYTHON_BIN}"
"${PYTHON_BIN}" -m PyInstaller "${PYI_ARGS[@]}" "${ENTRYPOINT}"

echo
echo "Build complete:"
echo "${ROOT_DIR}/dist/${APP_NAME}"
