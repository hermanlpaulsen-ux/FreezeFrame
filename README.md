# FreezeFrame

Desktop app for extracting the first frame from video files and saving them as images.

## Roadmap

- Development plan: [DEVELOPMENT_PLAN.md](./DEVELOPMENT_PLAN.md)

## Download App (macOS)

- Releases: https://github.com/hermanlpaulsen-ux/FreezeFrame/releases

## Features

- Native macOS app window (no Terminal window required)
- Input and output folder pickers
- Default output suggestion: `InputFolder/Stills`
- Multi-format output: `JPEG`, `PNG`, `TIFF`
- Format-specific output subfolders under output path:
  - `.../Stills/JPEG`
  - `.../Stills/PNG`
  - `.../Stills/TIFF`
- Quality presets (resolution unchanged):
  - `High`: lowest compression artifacts, larger files
  - `Balanced`: default quality/size tradeoff
  - `Small`: stronger compression, smaller files
- Bit-depth behavior:
  - `JPEG`: always 8-bit
  - `PNG`: always 8-bit
  - `TIFF`: selectable `8-bit` or `16-bit (if supported by source)`
- Output naming:
  - Mirrors source filename and appends preset suffix
  - Example: `my_video_high.jpg`, `my_video_balanced.png`, `my_video_small.tiff`
- Progress bar and per-run status
- `Start` becomes `Restart` after completion
- `Open output folder` appears after a successful run
- Close-warning while processing, with safe stop behavior

## Requirements

- Python 3.11+ (tested with Python 3.14)
- `pyinstaller`
- For packaging builds: local `ffmpeg` and `ffprobe` available on the build machine (both are embedded into distributable builds)

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

## Build Linux app

```bash
./build_linux_app.sh
```

Build output:

- `dist/FreezeFrame/`

Linux build behavior:

- The build fails if either `ffmpeg` or `ffprobe` is missing on the build machine.
- Both binaries are embedded into the app bundle so end users do not need to pre-install them.

## GitHub notes

- Do not commit `build/`, `dist/`, or `.app` bundles to source control.
- Use GitHub Releases for distributing built `.app` artifacts.

Made with ChatGPT Codex.
