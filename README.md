# FreezeFrame

Desktop app for extracting the first frame from video files and saving them as JPEG images.

## Roadmap

- Development plan: [DEVELOPMENT_PLAN.md](./DEVELOPMENT_PLAN.md)

## Download App (macOS)

- Releases: https://github.com/hermanlpaulsen-ux/FreezeFrame/releases

## Features

- Native macOS app window (no Terminal window required)
- Input and output folder pickers
- Default output suggestion: `InputFolder/Stills`
- Progress bar and per-run status
- `Start` becomes `Restart` after completion
- `Open output folder` appears after a successful run
- Close-warning while processing, with safe stop behavior

## Requirements

- macOS
- Python 3.11+ (tested with Python 3.14)
- `pyinstaller`
- Optional for bundling: `ffmpeg` installed locally (for example via Homebrew)

## Install dev dependencies

```bash
python3 -m pip install -r requirements.txt
```

## Run locally

```bash
python3 src/freezeframe_app.py
```

## Build macOS app

```bash
./build_mac_app.sh
```

Build output:

- `dist/FreezeFrame.app`

The build script tries to bundle ffmpeg from:

- `/opt/homebrew/bin/ffmpeg`
- `/usr/local/bin/ffmpeg`
- `$(command -v ffmpeg)`

If no local ffmpeg is found, the app still builds and will use system PATH resolution at runtime.

## GitHub notes

- Do not commit `build/`, `dist/`, or `.app` bundles to source control.
- Use GitHub Releases for distributing built `.app` artifacts.

Made with ChatGPT Codex.
