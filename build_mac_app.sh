#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="FreezeFrame"
ENTRYPOINT="${ROOT_DIR}/src/freezeframe_app.py"

if [[ ! -f "${ENTRYPOINT}" ]]; then
  echo "Missing entrypoint: ${ENTRYPOINT}"
  exit 1
fi

FFMPEG_CANDIDATES=(
  "/opt/homebrew/bin/ffmpeg"
  "/usr/local/bin/ffmpeg"
  "$(command -v ffmpeg 2>/dev/null || true)"
)
FFPROBE_CANDIDATES=(
  "/opt/homebrew/bin/ffprobe"
  "/usr/local/bin/ffprobe"
  "$(command -v ffprobe 2>/dev/null || true)"
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

PYI_ARGS=(
  --windowed
  --noconfirm
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

if [[ -n "${FFMPEG_BIN}" ]]; then
  echo "Bundling ffmpeg from: ${FFMPEG_BIN}"
  PYI_ARGS+=(--add-binary "${FFMPEG_BIN}:ffmpeg")
else
  echo "No local ffmpeg found; building without bundled ffmpeg."
fi
if [[ -n "${FFPROBE_BIN}" ]]; then
  echo "Bundling ffprobe from: ${FFPROBE_BIN}"
  PYI_ARGS+=(--add-binary "${FFPROBE_BIN}:ffmpeg")
else
  echo "No local ffprobe found; building without bundled ffprobe."
fi

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
echo "${ROOT_DIR}/dist/${APP_NAME}.app"
