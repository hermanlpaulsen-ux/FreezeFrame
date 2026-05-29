# FreezeFrame Development Plan

This plan translates product goals into an implementation roadmap with clear phases and deliverables.

## Product Goals

- Support both folder-wide processing and single-file processing from one unified input flow.
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

## Phase 1: Processing Modes (Unified)

Objective: support both folder-wide runs and targeted file runs.

Deliverables:

- Unified input model:
  - `Add folder` => process all supported files in selected folder (batch)
  - `Add file` => process only selected file (single-file run)
- File queue model with status per item (`pending`, `running`, `done`, `failed`, `cancelled`).
- Skip/overwrite behavior options.

Acceptance criteria:

- User can process a single file without scanning full folder.
- Batch and single-file runs share one execution engine and one process/status area.

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

- New layout and UX shell:
  - Input/output cards
  - Format + quality controls
  - Action row + bottom progress
  - Queue/preview/log panels as advanced view
- Better task feedback:
  - Per-file progress and failures
  - Retry failed items
- Settings screen for defaults and watch-folder preferences.

Acceptance criteria:

- All Phase 1–4 features are accessible without clutter.
- Core quick-start flow remains <= 3 clicks.

### Phase 5A: Qt Native UI Track (Current)

Objective: deliver a polished, offline native desktop app with modern controls and scalable architecture.

Deliverables:

- Migration from Tkinter to PySide6/Qt for native desktop rendering and richer styling control.
- Modern card-based UI shell:
  - Input folder card with Finder picker
  - Output folder card with Finder picker
  - Output formats (JPEG/PNG/TIFF)
  - Quality preset selector
  - TIFF bit-depth selector shown contextually when TIFF is enabled
  - Progress and status panel
- Refined visual system:
  - Blue/teal accent palette
  - Hover/pressed states on interactive controls
  - Improved sizing/spacing and control density
- Existing run-state behaviors preserved:
  - `Start` -> `Restart` after completion
  - `Open output folder` appears only when a completed run has a valid output path
  - Close warning while processing is active
- Fully offline runtime with bundled ffmpeg strategy unchanged.

Acceptance criteria:

- Native app launches and runs without terminal window.
- UI follows design direction and remains readable in macOS dark mode.
- Start action validates folders and format selection.
- TIFF bit-depth control only appears when TIFF is checked, aligned on the same row as quality.
- Progress/status update from 0% to 100% during real processing.

### Phase 5B: File Options + Preview Controls (Current)

Objective: expose advanced file-level controls in the unified window while preserving simple batch workflow.

Deliverables:

- Unified input/output card:
  - `Add file` and `Add folder` on the same input row
  - Output folder picker (default `Stills` near source)
- Output format controls (JPEG/PNG/TIFF) with per-run options.
- Resolution scaling:
  - `Original` (source size)
  - Preset output heights (`2160`, `1080`, `720`) with aspect-ratio preserving width.
- Custom bit depth controls for all selected formats:
  - 8-bit, 16-bit, 32-bit options
  - Capability-aware enablement based on source + codec support.
- Quality granularity:
  - Replace coarse preset with a Photoshop-style level `1–12`.
  - Map 1–12 internally to ffmpeg args per format.
- Preview panel:
  - Small still preview from selected frame.
  - Auto-refresh when frame number/slider changes.
- Reliable frame-range resolution:
  - Exact `nb_frames` when available
  - `ffprobe -count_frames`/`-count_packets` fallbacks
  - Duration*FPS estimate fallback with confidence labeling.
- Responsive exact-frame preview pipeline:
  - Preview runs in worker thread (no UI blocking)
  - Stale preview requests are cancelled while scrubbing
  - Per-frame preview cache for instant back/forward reuse
  - Fast seek strategies before exact fallback (`CFR preroll` -> `timestamp seek` -> `exact global frame`)
- Faster later-frame export path:
  - Export uses fast-seek strategies first for speed on high frame numbers
  - Exact global-frame extraction remains final fallback for robustness

Acceptance criteria:

- User can export a selected frame from a single file with format/quality/bit-depth/scale controls in one pass.
- Batch and single-file processing use the same effective output settings model.
- Quality level persists visually and maps consistently to actual export behavior.
- Unsupported bit-depth choices are disabled or clearly explained.
- Preview renders quickly and matches export frame choice.

## Next Fixes

- Batch frame selection consistency:
  - Ensure user-selected frame is consistently applied across every file in folder runs.
  - Add explicit UI copy that batch exports use a shared frame index for all files.
  - Add validation for out-of-range frame indices in batch mode (skip/adjust with clear status).

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

### Phase 6A Status Update (Completed)

Completed implementation:

- Linux packaging script added (`build_linux_app.sh`) with mandatory ffmpeg/ffprobe embedding.
- Linux build now fails fast if either ffmpeg or ffprobe is missing at build time.
- Open-folder action now uses platform-aware launch behavior:
  - macOS: `open`
  - Linux: `xdg-open` with sanitized environment to avoid Qt/PyInstaller library conflicts.
- Linux smoke tests verified:
  - App launch stability
  - Embedded ffmpeg/ffprobe presence in bundle
  - Frame export pipeline using bundled binaries.

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

## Current Sprint Focus

1. Implement two-tab architecture (`Batch` + `Single File`) with no regression in batch flow.
2. Build single-file advanced options: custom height, bit depth, 1–12 quality, and preview.
3. Keep process safety and UX consistency (start/stop/restart/open-output patterns).

## UI Follow-Ups

1. Improve title bar treatment on macOS (transparent/blended native approach).
2. Add final spacing and typography pass for format controls across all window sizes.
3. Add tab-specific onboarding hints so users understand Batch vs Single File intent instantly.

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
