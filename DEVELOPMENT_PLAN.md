# FreezeFrame Development Plan

This plan translates product goals into an implementation roadmap with clear phases and deliverables.

## Product Goals

- Add both batch folder processing and per-file processing workflows.
- Make frame selection explicit and easy (manual frame choice + preview).
- Add watch-folder automation.
- Support multiple output image formats with quality presets.
- Allow format-specific output destinations.
- Redesign UI for a more modern and scalable workflow.
- Ship Windows and Linux versions in addition to macOS.

## Guiding Principles

- Keep the current fast path simple: one-click export for common use.
- Add advanced controls progressively (not all at once in primary UI).
- Build a backend processing layer independent of UI toolkit to support cross-platform releases.
- Keep ffmpeg bundled where possible for predictable behavior.

## Phase 0: Foundation Refactor

Objective: prepare the codebase for feature growth and cross-platform packaging.

Deliverables:

- Split app into modules:
  - `src/ui/` for interface
  - `src/core/` for job models and orchestration
  - `src/ffmpeg/` for command generation and probing
  - `src/io/` for folder/file/output routing
- Add typed config model for app settings and processing options.
- Add structured logging and per-job error reports.
- Add basic unit-test harness for command construction and path routing.

Acceptance criteria:

- Existing behavior remains unchanged.
- App can run with same current feature set after refactor.

## Phase 1: Processing Modes (Batch + Per-file)

Objective: support both folder-wide runs and targeted file runs.

Deliverables:

- Processing mode selector:
  - Batch mode: process all supported files in selected folder
  - Per-file mode: choose one or multiple files
- File queue model with status per item (`pending`, `running`, `done`, `failed`, `cancelled`).
- Skip/overwrite behavior options.

Acceptance criteria:

- User can process a single file without scanning full folder.
- Batch and per-file modes share the same execution engine.

## Phase 2: Frame Selection + Preview

Objective: allow user to control exactly which frame is exported.

Deliverables:

- Frame source options:
  - First frame (default)
  - Timestamp input (`hh:mm:ss.ms`)
  - Frame number input
- Preview panel for selected media:
  - Render still from selected timestamp/frame
  - Prev/next frame nudging
- Persist last-used frame selection mode in settings.

Acceptance criteria:

- Preview matches exported result for same settings.
- Invalid frame/timestamp inputs are validated and explained.

## Phase 3: Watch Folder Automation

Objective: automatically process new files dropped into a folder.

Deliverables:

- Watch folder toggle and settings:
  - Source folder
  - Debounce/stability delay (e.g. wait for file write to complete)
  - Include subfolders (optional, default off)
- Auto-run jobs for new matching files.
- Duplicate detection by filename + size + modified timestamp hash.

Acceptance criteria:

- Newly added files are processed reliably once.
- Incomplete/copying files are not processed until stable.

## Phase 4: Output Format System

Objective: support multiple image outputs and quality control.

Deliverables:

- Output format selection:
  - JPEG
  - PNG
  - WebP (optional toggle if bundled ffmpeg supports it)
- Quality presets per format:
  - `High`, `Balanced`, `Small`
  - Advanced manual override in settings
- Format-specific output folder mapping:
  - Example: `.../Stills/JPEG`, `.../Stills/PNG`
  - Optional custom folder per format

Acceptance criteria:

- User can choose one or multiple output formats per run.
- Files are exported to correct format-specific destinations.

## Phase 5: New UI

Objective: modernize interaction model and support advanced workflows.

Deliverables:

- New layout:
  - Left panel: source/mode/options
  - Center: file queue + progress
  - Right/bottom: preview and logs
- Better task feedback:
  - Per-file progress and failures
  - Retry failed items
- Settings screen for defaults and watch-folder preferences.

Acceptance criteria:

- All Phase 1–4 features are accessible without clutter.
- Core quick-start flow remains <= 3 clicks.

## Phase 6: Windows and Linux

Objective: deliver stable cross-platform builds.

Deliverables:

- Platform abstraction for:
  - Open-folder actions
  - Path normalization and separators
  - File watcher backend
- Packaging:
  - Windows: `.exe` (PyInstaller)
  - Linux: AppImage or native package target
- Bundled ffmpeg strategy:
  - macOS, Windows, Linux build artifacts with known compatible binaries.

Acceptance criteria:

- Same project file/settings behave consistently across OSes.
- Smoke-test suite passes on all three platforms.

## Suggested Issue Breakdown (Initial Backlog)

1. Refactor code into `core/ui/ffmpeg/io` modules.
2. Introduce processing mode selector + queue model.
3. Add ffprobe metadata layer for duration/frame estimation.
4. Implement timestamp/frame selection and validation.
5. Add preview render workflow.
6. Implement watch-folder service and duplicate guard.
7. Add format abstraction and quality presets.
8. Implement format-specific output routing.
9. Deliver redesigned UI shell with queue + preview panes.
10. Add cross-platform adapters and CI matrix build.

## Risks and Mitigations

- ffmpeg capability drift across platforms:
  - Mitigation: pin bundled versions and keep compatibility matrix.
- UI complexity growth:
  - Mitigation: progressive disclosure (basic vs advanced panels).
- Watch-folder race conditions:
  - Mitigation: file stability checks + retry logic.

## Release Strategy

- `v1.1`: Phase 0 + Phase 1
- `v1.2`: Phase 2
- `v1.3`: Phase 3 + Phase 4
- `v2.0`: Phase 5 + initial Windows/Linux support
