# GeoScan UI Architecture Redesign

Date: 2026-07-05
Status: design draft v2 for review
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

Use two strategic steps, implemented as the staged plan later in this document:

1. First, extract a stable Python application boundary from the existing Tkinter GUI.
2. Then build a modern Tauri + React shell that talks to that Python boundary.

This keeps risk controlled. The current GUI can continue to ship while the backend contract becomes stable. Once the contract is stable, a new shell can be built without changing the production pipeline.

The product direction is a **GeoScan Control Console**: a task-focused desktop console for preparing, running, monitoring, and reviewing geologic-map vectorization jobs. It should borrow `cc-switch`'s modern shell clarity, but not its exact product shape. GeoScan's signature should come from its own domain: map processing stages, conversion readiness, and reviewable outputs.

## Product Design Brief

### Product

GeoScan turns scanned geologic section maps into editable vector candidates. The app serves a non-technical or semi-technical user who wants to select a map image, run the workflow, and open a clean output package for MapGIS/QGIS review.

### Primary job

The first screen should help the user answer four questions quickly:

1. What image am I running?
2. Is this machine ready to run it?
3. Which stage is running or failing?
4. Where is the output I can review?

### Reference direction

Use `cc-switch` only as a reference for modern desktop-shell structure: clear navigation, compact settings, update awareness, and calm status feedback. Do not clone its domain model or imply GeoScan is a code-agent switcher.

### Interactivity level

The final UI should be fully interactive. During design/prototyping, fake job events are acceptable for layout validation, but implementation must connect to the real Python event contract before release.

### Design risk worth taking

Make the **stage rail** the product's memorable element. Instead of a generic progress bar, show the true GeoScan pipeline as a compact, inspectable sequence. This is distinctive because it comes from the product's actual workflow, not from decoration.

## Visual and Interaction Direction

GeoScan should read as a professional production tool, not a marketing page. The interface can be modern, but it should stay quiet, dense, and predictable.

### Design principles

- Use structure as meaning. Stage numbers, output folder names, and conversion states are real operational information, so they can drive the visual hierarchy.
- Keep the surface restrained. Use color for primary actions, active navigation, stage status, warning, success, and error only.
- Prefer inline recovery over modal interruption. A modal is acceptable for risky confirmation, but ordinary errors should explain the fix near the failing control or stage.
- Do not hide advanced capability. Put uncommon options behind progressive disclosure rather than removing them.
- Keep every candidate explicitly review-only. UI copy must never imply the app has confirmed geologic correctness.

### Token direction

These are design targets for the future shell, not mandatory values for Phase 1 Tkinter extraction:

| Role | Token | Notes |
| --- | --- | --- |
| App background | `#F6F8FA` | Neutral, not cream/sand; keeps long tool sessions readable. |
| Main surface | `#FFFFFF` | Content panels and form regions. |
| Sidebar surface | `#EDF2F4` | Slightly cooler than the main surface to separate navigation. |
| Primary ink | `#17202A` | Body text and headings. |
| Muted ink | `#5D6B78` | Secondary labels; must still pass contrast. |
| GeoScan green | `#2F6B4F` | Primary action and ready/success state. |
| Section blue | `#2563A8` | Active stage, selected nav, link-style actions. |
| Warning amber | `#B7791F` | Recoverable conversion or environment warnings. |
| Danger red | `#B42318` | Failed stages and destructive rerun confirmations. |

Typography should use a system UI stack first. Product UI does not need a decorative display face. Use a tight type scale, visible labels, and tabular numbers for counts, durations, stage indexes, candidate totals, and batch progress.

### Motion direction

Motion should convey state only:

- 150-250 ms for hover, selected, expand/collapse, and status transitions.
- Press feedback on buttons with a subtle scale target around `0.96`.
- Stage changes may cross-fade or slide slightly within the rail.
- Logs should append without jumping the whole page.
- Respect reduced-motion settings.

Do not use AIDA page structure, cinematic page-load choreography, GSAP scroll effects, large hero sections, decorative gradient orbs, or repeated card grids.

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

```text
+--------------------------------------------------------------------------+
| Top bar: GeoScan / current workspace / update state / settings shortcut  |
+-------------------+--------------------------------+---------------------+
| Sidebar           | Main workspace                 | Run monitor         |
|                   |                                |                     |
| Single Run        | Source image                   | Environment check   |
| Batch Run         | Map ID                         | Stage rail          |
| Job History       | Output location                | Live log            |
| Settings          | Beginner options               | Result summary      |
| AI                | Expert options collapsed       | Recovery actions    |
| Updates           | Run / Stop / Open output       |                     |
+-------------------+--------------------------------+---------------------+
```

The current Tkinter GUI can approximate this with tabs and grouped frames during the transition, but the Tauri shell should use the three-zone layout directly.

Pages:

- Single Run: input image, map ID, output folder, core options, run state, output result.
- Batch Run: source folder, limit/retry settings, per-map table, summary counts.
- Settings: SECTION, W60, ogr2ogr, GDAL data, OCR interpreter, local config path, auto-detect tools.
- AI: provider, base URL, model, API key, save-key toggle, connection test, visual review action.
- Updates: current version, runtime version, available update, download/apply progress.
- Job History: local recent runs, output roots, status, report path, open-folder action.

Advanced parameters should remain available but should not dominate the first screen.

## Core Screens

### Single Run

This is the default screen.

Primary content:

- source image picker
- derived Map ID with editable override
- output parent and computed `<MAPID>_P` preview
- beginner options: input leveling, enhanced preview, conversion mode
- primary action: `Start run`
- secondary actions: `Stop safely`, `Open output`, `Reveal report`

Right-side monitor:

- environment readiness
- stage rail
- live log
- result summary
- inline recovery actions

Empty state:

- show one clear action: choose an input image
- explain that Map ID and output folder will be filled automatically
- do not show the full advanced form before an image is chosen

### Batch Run

Batch is not just Single Run repeated. It needs table-first monitoring.

Primary content:

- source folder picker
- discovered raster count
- optional limit
- retry incomplete toggle
- batch run/stop controls

Table columns:

- Map ID
- status
- line candidates
- text candidates
- conversion status
- output folder
- error summary

The table should use tabular numbers and preserve row height while updates stream in.

### Job History

Job history should help the user return to outputs without remembering folders.

Initial implementation should derive history from known output reports rather than create a new database. A local index can be added later only if report scanning becomes slow.

Rows should include:

- map ID
- last run time
- output root
- completion state
- line/text/area candidate counts
- conversion result
- report path
- open-folder action

### Settings

Settings should be organized by operational dependency:

- MapGIS conversion: SECTION, W60, dongle status
- DXF/GDAL: ogr2ogr, GDAL data
- OCR: embedded OCR state, optional external OCR interpreter
- local app data: config path, key-storage state
- update channel: current version, runtime version, release check

`Auto-detect tools` should be a prominent action with a visible result summary.

### AI

AI remains optional and review-only.

Controls:

- provider
- base URL
- model
- API key
- save encrypted key toggle
- test connection
- run visual review

The key field should always show whether the key is session-only or encrypted locally. Logs and events must show only redacted keys.

### Updates

Updates should explain the chosen route:

- no update
- lightweight engine update
- full installer required
- network failure
- checksum failure

The user should know whether the update preserves local settings before applying it.

## Stage Rail

The stage rail is the main signature component.

Stages:

| Stage | Label | Source of truth |
| --- | --- | --- |
| `00_INPUT_FREEZE` | Input freeze | frozen pixel-unit TIFF and source hash |
| `04_LINE_WORKFLOW` | Line candidates | line GeoJSON and candidate count |
| `05_TEXT_WORKFLOW` | Text candidates | OCR/fallback GeoJSON and text count |
| `DXF_EXPORT` | Exchange files | line/text/area DXF or Shapefile status |
| `08_SECTION_W60` | MapGIS conversion | SECTION/W60 pipeline status |
| `MAPGIS_LOAD_READY` | Load-ready package | final deliverable folder |

Stage states:

- `pending`
- `running`
- `completed`
- `warning`
- `failed`
- `skipped`
- `blocked`
- `cancelled`

Each stage should expose:

- short status label
- details panel
- key output paths
- counts when available
- recovery action when there is one

Examples:

- If conversion mode is `prepare`, the MapGIS conversion stage is `skipped`, not failed.
- If SECTION fails after DXF export, `DXF_EXPORT` can be `completed` while `08_SECTION_W60` is `failed`.
- If the user stops safely mid-run, incomplete downstream stages are `cancelled` or `blocked`, and the output is not presented as load-ready.

## Preflight Gate

Preflight should happen before expensive work starts.

Checks:

| Check | Blocks run? | UI behavior |
| --- | --- | --- |
| input image exists | yes | field error near source picker |
| output parent writable | yes | field error with choose-folder action |
| Map ID valid | yes | inline correction |
| SECTION found for `cli` | yes by default | link to Settings and auto-detect |
| W60 found for `cli` | yes by default | link to Settings and auto-detect |
| ogr2ogr available when DXF export is enabled | yes | link to Settings and auto-detect |
| dongle process running for `cli` | warning with confirmation | offer retry, switch to `prepare`, or continue with risk |
| OCR engine available | warning | explain fallback or external OCR setting |
| AI provider complete when AI action is requested | yes for AI action only | field errors in AI page |

Preflight should produce one compact readiness summary:

```text
Ready to run
Needs attention
Blocked
```

The run button should make the reason visible when disabled.

## Beginner and Expert Modes

The default mode should be beginner-friendly.

Beginner mode shows:

- choose input image
- Map ID
- output folder
- input leveling
- enhanced preview
- conversion mode
- start/stop/open output
- readiness and stage rail

Expert mode adds:

- target WL/WT/WP names
- line engine
- line repair
- export source
- text candidate override
- OCR interpreter
- timeout
- include areas
- reset/fresh rerun
- AI enhancement and visual review controls

The mode should not create two separate behavior paths. It is only progressive disclosure over the same request model.

## Interaction States

Every screen should define these states before implementation:

### Single Run states

- no source selected
- ready
- ready with warnings
- blocked by validation
- running
- stop requested
- completed with load-ready package
- completed without WT/WL because conversion was skipped
- incomplete because conversion failed
- failed before output package
- cancelled safely

### Batch states

- no source folder selected
- no rasters found
- ready
- running
- stop requested after current map
- completed
- completed with failures
- cancelled before queue finished

### Update states

- idle
- checking
- update available as engine package
- update available as installer
- downloading
- verifying checksum
- applying
- restart required
- failed with retry

### Settings states

- never configured
- auto-detect running
- detected and unsaved
- saved
- saved but tool path missing
- non-ASCII path warning when relevant

## Error and Recovery Matrix

Errors should point to a next action.

| Situation | Message intent | Recovery action |
| --- | --- | --- |
| output folder already exists | explain fresh rerun vs new output | enable fresh rerun or choose new parent |
| missing SECTION/W60 | explain conversion cannot make WT/WL | open Settings, auto-detect, or switch to `prepare` |
| dongle not running | explain `cli` likely fails late | retry detection, switch to `prepare`, or continue with explicit risk |
| ogr2ogr missing | explain DXF export cannot complete | auto-detect or set path |
| AI connection fails | show endpoint/model context without key | edit AI settings or retry |
| user stops run | explain incomplete output cannot be used as final | fresh-rerun same map later |
| conversion fails after DXF | distinguish usable DXF from missing WT/WL | open output folder, inspect logs, rerun conversion |

The UI should never flatten these into a generic "run failed" state.

## Component Quality Rules

These rules apply to the future React/Tauri UI and to any interim UI polish:

- interactive hit areas at least 40 x 40 px
- visible focus indicators
- hover, active, disabled, loading, warning, error, and success states for shared controls
- tabular numbers for candidate counts, stage indexes, durations, file sizes, and batch totals
- no text-only icon buttons unless they have accessible labels/tooltips
- no layout shift when logs or batch rows update
- no `transition: all`; specify exact animated properties
- do not rely on color alone for stage status; include label/icon/text
- advanced panels must preserve user-entered values when collapsed
- long paths should wrap or middle-truncate with tooltip/copy action

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
- preflight readiness summaries
- stage event mapping from production reports
- completion message generation
- event serialization contains no secrets
- cancellation produces an incomplete warning
- batch row events preserve map ID, status, candidate counts, and conversion status

Existing tests in `tests/test_production_gui.py` should be moved or adapted so the behavior is covered without requiring a live Tkinter window.

For the future Tauri shell:

- unit-test request building in React
- integration-test Tauri command invocation with a fake JSONL engine
- UI tests for no-source, blocked, running, warning, failed, completed, and cancelled states
- accessibility checks for focus order, labels, and keyboard operation
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
- Emit preflight events, stage-rail events, warning events, result events, and recovery-action hints.
- Keep event names stable and documented so both Tkinter and Tauri can consume them.

Verification:

- unit tests for request parsing and event output
- stage mapping tests for `prepare`, `none`, successful `cli`, failed `cli`, cancellation, and skipped conversion
- local dry-run with a synthetic request
- secret redaction tests

### Phase 3: Prototype the control console shell

Expected changes:

- Build a local React/Tauri prototype around a fake JSONL engine.
- Implement the three-zone layout: navigation, main workspace, run monitor.
- Implement the stage rail, preflight summary, beginner/expert disclosure, and key empty/error states.
- Keep the prototype visually restrained and tool-like; do not add marketing hero sections or decorative motion.

Verification:

- fake-engine UI tests
- screenshot checks at desktop and smaller widths
- keyboard navigation through primary controls
- state coverage for no source, blocked, running, failed, warning, completed, and cancelled

### Phase 4: Connect the Tauri + React shell to the real engine

Expected changes:

- Add `ui/` with Tauri and React.
- Replace fake events with the real JSONL engine command.
- Implement pages around the JSONL engine contract: Single Run, Batch Run, Job History, Settings, AI, Updates.
- Keep Tkinter GUI available until the new shell passes acceptance.

Verification:

- frontend build
- Tauri dev launch
- fake-engine and real-engine UI tests
- real single-map dry run where safe
- Windows packaged smoke test

### Phase 5: Packaging integration

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
- The stage rail accurately represents skipped, failed, completed, blocked, and cancelled workflow stages.
- Preflight prevents avoidable expensive failures and gives the user clear recovery actions.
- Beginner mode can complete a normal single-map run without exposing expert-only controls.
- Expert mode still exposes the advanced controls available in the current GUI.
- Batch rows update without layout shift and retain useful counts/statuses.
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
4. Whether the right-side run monitor should be permanently visible or collapsible on small screens.
   - Recommendation: keep it visible on desktop, collapse it behind a status drawer only on constrained widths.
5. Whether the stage rail should show area/WP work as a permanent stage.
   - Recommendation: keep area output inside the exchange-files stage until area processing becomes a default path.

## Implementation Guardrails

- Make surgical changes in Phase 1; do not redesign the visual layout while extracting the boundary.
- Preserve current user-facing Chinese messages unless the extraction requires clearer shared wording.
- Do not start the Tauri implementation until the stage event contract is stable enough to drive a fake-engine prototype.
- Keep visual design restrained: no AIDA page structure, no cinematic hero, no decorative GSAP scroll scenes, no generic card wall.
- Do not change versioning, installer naming, updater asset names, or runtime version semantics during Phase 1.
- Do not move private MapGIS writer logic into public modules.
- Do not treat passing tests as proof of vectorization quality; manual MapGIS acceptance remains separate.
- Keep AI review-only and never write `checked=yes`.
