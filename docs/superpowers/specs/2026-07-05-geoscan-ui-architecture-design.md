# GeoScan UI Architecture Redesign

Date: 2026-07-05
Status: design draft for review
Scope: UI architecture and migration plan only. No implementation is included in this document.

## Background

GeoScan is a Python-first desktop tool for semi-automatic vectorization of scanned geologic section maps. The current GUI is a Tkinter/ttk application in `src/geoscan/production_gui.py`. It is already functional, packaged, and connected to the existing production pipeline, settings, updater, AI review, single-map runs, and batch runs.

The issue is not that Tkinter cannot launch the workflow. The issue is that the current GUI file has grown into a mixed controller, view, settings editor, job runner, log router, validation layer, and update surface. That makes future UI work harder than it should be:

- UI changes risk touching production behavior.
- New user-facing workflows are likely to add more state and threading logic to one file.
- A more modern interface cannot be cleanly built while the Python GUI owns the runtime contract.
- Replacing the whole system would create unnecessary risk because the Python vectorization and MapGIS bridge are the valuable, already-tested core.

The previous packaging discussion treated `cc-switch` as a useful reference for application shell, update ergonomics, and modern frontend structure. It is not a reason to rewrite GeoScan's Python core in Rust or TypeScript.

## Goals

1. Make the UI architecture easier to evolve without destabilizing vectorization, OCR, DXF export, MapGIS conversion, or update behavior.
2. Allow a future modern shell similar in feel to `cc-switch`: sidebar navigation, clean task pages, live logs, task history, settings, and update checks.
3. Keep Python as the engine for all domain logic.
4. Preserve the current release assumptions: full offline install for non-technical users, local settings under `%LOCALAPPDATA%\GeoScan`, no plaintext API keys, and the existing engine/runtime update model.
5. Keep the first migration step small enough to verify with current tests before any visual rewrite.

## Non-Goals

- Do not rewrite vectorization, OCR, line repair, text extraction, DXF export, MapGIS bridge, updater, or settings storage in another language.
- Do not add map data, proprietary MapGIS resources, private writer modules, or generated run outputs to this repository.
- Do not change the public/private source boundary.
- Do not replace the installer and updater in the first phase.
- Do not design a marketing landing page. The first screen should remain the working application.

## Recommendation

Use a two-phase migration:

1. First, extract a stable Python application boundary from the existing Tkinter GUI.
2. Then build a modern Tauri + React shell that talks to that Python boundary.

This keeps risk controlled. The current GUI can continue to ship while the backend contract becomes stable. Once the contract is stable, a new shell can be built without changing the production pipeline.

## Alternatives Considered

### Option A: Keep Tkinter and only beautify it

This is the fastest path, but it does not solve the architectural problem. It would improve spacing, colors, and labels, but the UI would still be constrained by a large Tkinter controller and traditional form layout.

Use this only for small emergency UX fixes.

### Option B: Split the Tkinter GUI into modules first

This is the safest first implementation phase and should be done regardless of the future shell. It creates clear interfaces for state, validation, background jobs, logging, settings, updater actions, and batch rows.

This should be the first engineering step.

### Option C: Build a Tauri + React shell over the Python engine

This best matches the desired `cc-switch`-like direction. It enables a modern desktop interface without rewriting the domain core. The tradeoff is more moving parts: a web frontend, a Tauri shell, a Python engine process, and an event protocol between them.

This should happen after Option B creates the backend contract.

### Option D: Move to PySide6/Qt

This keeps everything in Python and gives a stronger desktop toolkit than Tkinter. It is simpler than Tauri technically, but it still ties the interface to Python widget code and does not match the `cc-switch` interaction model as well as React.

This is a fallback if Tauri packaging becomes too costly.

## Target Architecture

```text
Desktop shell
  - Current: Tkinter/ttk
  - Future: Tauri + React

Application boundary
  - Form/state model
  - Validation
  - Settings read/write
  - Job start/stop
  - Progress/log/result events
  - Update check/install commands

Python engine
  - production_program
  - batch_runner
  - ai_vision_review
  - updater
  - app_settings
  - env_probe
  - mapgis67_bridge

Domain outputs
  - <MAPID>_P
  - MAPGIS_LOAD_READY
  - DXF/GeoJSON/WL/WT when produced
  - reports and logs
```

The important boundary is between the desktop shell and the application boundary. The shell should not import or call low-level production modules directly. It should submit a validated command and receive structured events.

## Proposed Python Modules

### `geoscan.desktop_state`

Owns dataclasses and defaults used by any UI:

- `GuiFormState`
- default map ID derivation
- default output root preview
- conversion from form state to `ProgramConfig`, `BatchConfig`, and `AiVisionConfig`

This is mostly existing code moved out of `production_gui.py`.

### `geoscan.desktop_validation`

Validates user-facing state before a job starts:

- required input image exists
- map ID is valid
- output parent is usable
- line repair and export-source combinations are valid
- batch source directory exists and contains supported rasters
- AI settings are complete when AI actions are requested
- timeout fields parse correctly

This module should return user-facing error objects or strings rather than showing message boxes.

### `geoscan.desktop_jobs`

Owns job orchestration:

- single-map run
- batch run
- AI connection test
- AI visual analysis
- update check/download/apply
- cancellation signals

It should emit events instead of writing directly to widgets.

### `geoscan.desktop_events`

Defines a small event contract shared by Tkinter and future Tauri:

```json
{"type":"log","level":"info","message":"开始运行"}
{"type":"status","state":"running","label":"文字识别"}
{"type":"progress","stage":"line_workflow","current":1,"total":6}
{"type":"batch_row","map_id":"T01_0006","status":"completed","line_candidates":120}
{"type":"result","kind":"single_run","output_root":"E:\\work\\T01_0006_P"}
{"type":"error","title":"运行失败","message":"SECTION 转换未完成"}
```

Events must avoid plaintext API keys and must not mark geological candidates as checked.

### `geoscan.desktop_settings`

Wraps the existing `app_settings` behavior for UI use:

- read machine settings
- save machine settings
- load encrypted API key
- save encrypted API key only when explicitly requested
- apply settings to environment for the current process

This module should preserve the current `%LOCALAPPDATA%\GeoScan\config` boundary.

### `geoscan.production_gui`

After extraction, this becomes a thin Tkinter shell:

- builds widgets
- binds controls to form state
- calls validation and job services
- renders events into log text, progress state, tree rows, and dialogs

It should no longer own production behavior.

## Future Tauri + React Shell

The future shell should be a separate app layer, not a replacement for the Python engine.

Recommended layout:

```text
ui/
  package.json
  src/
    app/
    pages/
      SingleRunPage.tsx
      BatchRunPage.tsx
      JobHistoryPage.tsx
      SettingsPage.tsx
      AiPage.tsx
      UpdatesPage.tsx
    components/
    state/
  src-tauri/
```

The Tauri backend should start the packaged Python engine command and stream JSONL events from stdout. The React frontend should render those events.

The Python side should expose commands such as:

```powershell
GeoScan.exe engine run-single --request request.json --events jsonl
GeoScan.exe engine run-batch --request request.json --events jsonl
GeoScan.exe engine test-ai --request request.json --events jsonl
GeoScan.exe engine check-update --events jsonl
```

Exact command names can change during implementation. The design requirement is that the protocol is structured and testable.

## User Experience Direction

The application should feel like a focused production tool, not a landing page.

Recommended first viewport:

- left navigation with clear sections
- primary workspace for the selected workflow
- persistent status/log area
- prominent run/stop/open-output actions
- environment readiness shown near the action area

Pages:

- Single Run: input image, map ID, output folder, core options, run state, output result.
- Batch Run: source folder, limit/retry settings, per-map table, summary counts.
- Settings: SECTION, W60, ogr2ogr, GDAL data, OCR interpreter, local config path, auto-detect tools.
- AI: provider, base URL, model, API key, save-key toggle, connection test, visual review action.
- Updates: current version, runtime version, available update, download/apply progress.
- Job History: local recent runs, output roots, status, report path, open-folder action.

Advanced parameters should remain available but should not dominate the first screen.

## Data Flow

### Single-map run

1. UI builds form state.
2. Validation returns either a user-facing error or a valid request.
3. Job runner applies machine settings to environment.
4. Dongle preflight runs when conversion mode is `cli`.
5. `run_production_program` runs with cancellation support.
6. Job runner emits log, status, result, warning, or error events.
7. UI renders events and offers to open the output folder.

### Batch run

1. UI validates the batch source folder and limit.
2. `discover_source_rasters` returns input rasters.
3. Job runner builds `BatchConfig`.
4. `run_batch` emits per-map progress rows.
5. UI renders the table and summary counts.

### Update check

1. UI asks job runner to check updates.
2. Existing `updater.py` checks GitHub Releases.
3. If an engine update matches the local runtime, use the lightweight engine path.
4. Otherwise fall back to the full installer.
5. UI shows the chosen route clearly.

## Error Handling

Errors should be converted once, near the application boundary, into user-facing messages.

Examples:

- Existing output folder without fresh rerun: explain how to rerun or change output.
- Missing SECTION/W60/ogr2ogr: route user to Settings and auto-detect.
- Missing dongle service: warn before starting expensive work.
- Conversion incomplete: do not claim WT/WL success; point to DXF and logs.
- AI connection failure: show sanitized endpoint/model context, never the full API key.
- Cancellation: mark output as incomplete and tell the user to fresh-rerun if needed.

## Testing Strategy

Phase 1 tests should focus on the extracted Python boundary:

- form state to `ProgramConfig`
- form state to `BatchConfig`
- form state to `AiVisionConfig`
- validation errors for common bad inputs
- completion message generation
- event serialization contains no secrets
- cancellation produces an incomplete warning
- batch row events preserve map ID, status, candidate counts, and conversion status

Existing tests in `tests/test_production_gui.py` should be moved or adapted so the behavior is covered without requiring a live Tkinter window.

For the future Tauri shell:

- unit-test request building in React
- integration-test Tauri command invocation with a fake JSONL engine
- smoke-test packaged app launch
- verify real engine commands on Windows before release

## Migration Plan

### Phase 1: Extract the Python application boundary

Expected changes:

- Add `desktop_state`, `desktop_validation`, `desktop_events`, `desktop_jobs`, and `desktop_settings`.
- Move existing pure functions and dataclasses out of `production_gui.py`.
- Keep Tkinter behavior visually unchanged.
- Keep all existing packaging behavior unchanged.

Verification:

- `pytest tests/test_production_gui.py`
- `pytest tests/test_updater.py`
- targeted manual launch of the Tkinter GUI

### Phase 2: Add JSONL engine commands

Expected changes:

- Add command entry points for single run, batch run, AI test, AI analysis, and update check.
- Serialize events to stdout.
- Accept request JSON files rather than long command-line flag lists for UI-driven runs.

Verification:

- unit tests for request parsing and event output
- local dry-run with a synthetic request
- secret redaction tests

### Phase 3: Build the Tauri + React shell

Expected changes:

- Add `ui/` with Tauri and React.
- Implement pages around the JSONL engine contract.
- Keep Tkinter GUI available until the new shell passes acceptance.

Verification:

- frontend build
- Tauri dev launch
- fake-engine UI tests
- Windows packaged smoke test

### Phase 4: Packaging integration

Expected changes:

- Decide whether the final deliverable is still PyInstaller-first, Tauri-first, or a bundled hybrid.
- Preserve full offline install.
- Preserve engine/runtime update semantics or replace them only with an equivalent tested path.

Verification:

- installer build
- update check against a test release
- config persistence across upgrade
- private-module exclusion check before release

## Acceptance Criteria

The design is successfully implemented only when:

- `production_gui.py` is a thin shell rather than the owner of business behavior.
- Single-run, batch-run, AI, settings, and update flows can be exercised through a shared application boundary.
- The event protocol is documented and covered by tests.
- No plaintext API keys appear in logs, events, settings, reports, or test snapshots.
- Existing Tkinter GUI behavior remains usable during the migration.
- The future Tauri shell can run the Python engine without importing private modules or rewriting core algorithms.
- Packaging still excludes private modules and does not include map data or generated outputs.

## Open Decisions

1. Whether the first new UI prototype should use the real engine command or a fake JSONL engine.
   - Recommendation: start with a fake engine for layout and interaction, then connect the real engine after the protocol stabilizes.
2. Whether to keep Tkinter installed after the Tauri shell becomes primary.
   - Recommendation: keep it for one release as a fallback, then remove if the new shell is stable.
3. Whether job history should be derived from existing output reports or stored in a new local index.
   - Recommendation: derive from output reports first; add a local index only if performance or discoverability becomes a real issue.

## Implementation Guardrails

- Make surgical changes in Phase 1; do not redesign the visual layout while extracting the boundary.
- Preserve current user-facing Chinese messages unless the extraction requires clearer shared wording.
- Do not change versioning, installer naming, updater asset names, or runtime version semantics during Phase 1.
- Do not move private MapGIS writer logic into public modules.
- Do not treat passing tests as proof of vectorization quality; manual MapGIS acceptance remains separate.
- Keep AI review-only and never write `checked=yes`.
