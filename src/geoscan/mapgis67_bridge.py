from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from geoscan.candidates import read_json as _read_json, write_json as _write_json
from geoscan.env_probe import program_candidates
from geoscan.section_batch import prepare_section_batch_input
from geoscan.section_collect import (
    collect_section_batch_mapgis_files,
    parse_conversion_list,
)


DEFAULT_SECTION_EXE = Path(r"D:\mapgis67\program\section.exe")
DEFAULT_W60_CONV_EXE = Path(r"D:\mapgis67\program\W60_Conv.exe")
DEFAULT_SECTION_BOOTSTRAP_FILE = Path(__file__).resolve().parents[0] / "section_bootstrap" / "SECTION_BOOTSTRAP.WT"
SECTION_OPEN_FILE_MENU_PATHS = (
    ("文件", "打开工程或文件"),
    ("文件", "打开工程和文件"),
    ("文件", "打开文件"),
    ("文件", "打开"),
)
SECTION_DXF_MENU_PATH = ("1辅助工具", "打开外部数据", "批量转换dxf")
W60_DXF_MENU_PATH = ("输入", "成批转换DXF")
W60_DXF_MENU_COMMAND_ID = 4056
# Optional WP area route: load the Shapefile exchange package, then save it
# out as a MapGIS .WP (the same two steps MANUAL_W60_STEPS.md describes).
W60_LOAD_SHAPE_MENU_PATH = ("输入", "装入SHAPE文件")
W60_SAVE_WP_MENU_PATH = ("文件", "换名存区")
# Command IDs observed in the real W60_Conv menu tree (automation
# diagnostics dump); used as fallback when caption matching fails.
W60_LOAD_SHAPE_COMMAND_ID = 32801
W60_SAVE_WP_COMMAND_ID = 4019
W60_SHAPE_LOAD_SETTLE_SECONDS = 4
SECTION_STARTUP_SETTLE_SECONDS = 5
SECTION_BATCH_CONVERT_PRECONDITION = (
    "SECTION must open or load any MapGIS file before the DXF batch-conversion menu is visible. "
    "The bridge opens a fixed bootstrap .WT/.WL/.WP/.MPJ file first, then uses "
    "1辅助工具 -> 打开外部数据 -> 批量转换dxf. Launching section.exe directly may show only "
    "文件/查看/设置/帮助 and hide the batch menu. If SECTION still lacks the menu, use "
    "W60_Conv.exe 输入 -> 成批转换DXF and then run verify_batch/collect_ready."
)
MAPGIS_EXTENSIONS = (".mpj", ".wl", ".wt", ".wp")
REQUIRED_LINE_TEXT_SUFFIXES = (".wl", ".wt")


class SectionAutomationError(RuntimeError):
    def __init__(self, *, stage: str, message: str, diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.stage = stage
        self.diagnostics = diagnostics or {}


def _resolve_section_exe(section_exe: Path | None) -> tuple[Path, list[str]]:
    if section_exe is not None and section_exe.is_file():
        return section_exe, [str(section_exe)]

    discovered = [path for path in program_candidates("section") if path.is_file()]
    if discovered:
        return discovered[0], [str(path) for path in discovered]

    return section_exe or DEFAULT_SECTION_EXE, [str(path) for path in program_candidates("section")]


def _resolve_w60_conv_exe(w60_conv_exe: Path | None) -> tuple[Path, list[str]]:
    if w60_conv_exe is not None and w60_conv_exe.is_file():
        return w60_conv_exe, [str(w60_conv_exe)]

    discovered = [path for path in program_candidates("w60_conv") if path.is_file()]
    if discovered:
        return discovered[0], [str(path) for path in discovered]

    return w60_conv_exe or DEFAULT_W60_CONV_EXE, [str(path) for path in program_candidates("w60_conv")]


FAILURE_CATEGORY_BOOTSTRAP = "bootstrap"
FAILURE_CATEGORY_MENU = "menu"
FAILURE_CATEGORY_DXF_DIALOG = "dxf_dialog"
FAILURE_CATEGORY_OUTPUT_VERIFICATION = "output_verification"
FAILURE_CATEGORY_PRECONDITION = "precondition"


def classify_bridge_failure(status: str, stage: str | None = None) -> str | None:
    """Map a convert status/automation stage to one of the four failure buckets.

    Buckets: bootstrap open failure, menu trigger failure, batch DXF dialog
    failure, output verification failure (plus precondition for missing
    inputs/exes). Returns None for non-failure statuses.
    """
    if status in {"verified", "dry_run", "not_started"}:
        return None
    if status == "missing_bootstrap_file":
        return FAILURE_CATEGORY_BOOTSTRAP
    if status in {"invalid_batch", "missing_section_exe", "missing_w60_conv_exe", "missing_batch_dxf"}:
        return FAILURE_CATEGORY_PRECONDITION
    if status == "output_verification_failed":
        return FAILURE_CATEGORY_OUTPUT_VERIFICATION
    if stage:
        if stage.startswith("open_bootstrap_"):
            return FAILURE_CATEGORY_BOOTSTRAP
        if "menu_not_found" in stage or stage.endswith("menu_failed"):
            return FAILURE_CATEGORY_MENU
        if "dialog" in stage:
            return FAILURE_CATEGORY_DXF_DIALOG
    return "unknown"


def _directory_snapshot(directory: Path) -> dict[str, dict[str, Any]]:
    """Recursive file snapshot (relative path -> size/mtime) for post-trigger diffs."""
    snapshot: dict[str, dict[str, Any]] = {}
    if not directory.is_dir():
        return snapshot
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        snapshot[str(path.relative_to(directory))] = {
            "bytes": stat.st_size,
            "mtime": stat.st_mtime,
        }
    return snapshot


def _diff_directory_snapshots(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    created = sorted(name for name in after if name not in before)
    removed = sorted(name for name in before if name not in after)
    modified = sorted(
        name
        for name in after
        if name in before
        and (
            after[name]["bytes"] != before[name]["bytes"]
            or after[name]["mtime"] != before[name]["mtime"]
        )
    )
    return {
        "created": created,
        "modified": modified,
        "removed": removed,
        "created_mapgis_outputs": [
            name for name in created if Path(name).suffix.lower() in {".wl", ".wt", ".wp"}
        ],
        "before_file_count": len(before),
        "after_file_count": len(after),
    }


def _post_wait_window_state(pid: int | None, hwnd: int | None) -> dict[str, Any] | None:
    """Window/dialog/menu state captured when verification fails after a trigger."""
    if pid is None:
        return None
    try:
        return _collect_section_window_diagnostics(
            pid,
            hwnd or 0,
            stage="output_verification_failed",
        )
    except Exception as exc:  # pragma: no cover - depends on GUI/pywin32 state.
        return {"diagnostic_error": f"{type(exc).__name__}: {exc}"}


def _section_manifest_path(batch_dir: Path) -> Path:
    return batch_dir / "section_batch_manifest.json"


def _load_section_manifest(batch_dir: Path) -> list[dict[str, Any]]:
    manifest_path = _section_manifest_path(batch_dir)
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    records = _read_json(manifest_path)
    if not isinstance(records, list):
        raise RuntimeError(f"SECTION batch manifest must be a list: {manifest_path}")
    return records


def _target_suffix(target_name: str) -> str:
    return Path(target_name).suffix.lower()


def _is_required_line_text_target(target_name: str) -> bool:
    return _target_suffix(target_name) in REQUIRED_LINE_TEXT_SUFFIXES


def _inspect_prepared_batch(batch_dir: Path) -> dict[str, Any]:
    records = _load_section_manifest(batch_dir)
    missing_inputs = []
    inspected_records = []
    for record in records:
        section_input = Path(record["section_input"])
        exists = section_input.is_file()
        size = section_input.stat().st_size if exists else 0
        if not exists or size <= 0:
            missing_inputs.append(str(section_input))
        inspected_records.append({**record, "section_input_exists": exists, "section_input_bytes": size})

    report = {
        "action": "prepare_batch",
        "ok": not missing_inputs and bool(records),
        "reused": False,
        "batch_dir": str(batch_dir),
        "section_batch_manifest": str(_section_manifest_path(batch_dir)),
        "prepared_dxf_count": len(records),
        "missing_inputs": missing_inputs,
        "records": inspected_records,
    }
    _write_json(batch_dir / "mapgis67_bridge_prepare_report.json", report)
    return report


def prepare_batch(*, source_dir: Path, batch_dir: Path) -> dict[str, Any]:
    records = prepare_section_batch_input(source_dir, batch_dir)
    report = _inspect_prepared_batch(batch_dir)
    report["source_dir"] = str(source_dir)
    report["records"] = [{**inspected, **record} for inspected, record in zip(report["records"], records)]
    _write_json(batch_dir / "mapgis67_bridge_prepare_report.json", report)
    return report


def _mapgis_file_counts(files: Iterable[Path]) -> dict[str, int]:
    counts = {extension: 0 for extension in MAPGIS_EXTENSIONS}
    for path in files:
        extension = path.suffix.lower()
        if extension in counts:
            counts[extension] += 1
    return counts


def _summarize_section_output(record: dict[str, Any]) -> dict[str, Any]:
    target_name = str(record.get("target_name", ""))
    expected_suffix = _target_suffix(target_name)
    required = _is_required_line_text_target(target_name)
    section_input = Path(record["section_input"])
    output_dir = Path(record.get("section_output_dir") or section_input.with_suffix(""))
    files = sorted(path for path in output_dir.rglob("*") if path.is_file()) if output_dir.is_dir() else []
    root_output = section_input.with_suffix(Path(target_name).suffix or expected_suffix)
    if root_output.is_file() and root_output not in files:
        files.append(root_output)
    mapgis_files = [path for path in files if path.suffix.lower() in MAPGIS_EXTENSIONS]
    expected_files = [
        path
        for path in mapgis_files
        if path.suffix.lower() == expected_suffix and path.stat().st_size > 0
    ]
    mpj_files = [path for path in mapgis_files if path.suffix.lower() == ".mpj" and path.stat().st_size > 0]
    root_output_ok = root_output.is_file() and root_output.stat().st_size > 0 and root_output.suffix.lower() == expected_suffix
    ok = (output_dir.is_dir() and bool(mpj_files) and (not required or bool(expected_files))) or (
        required and root_output_ok
    )
    if ok:
        status = "ok_root_output" if required and root_output_ok and not mpj_files else "ok"
    elif not output_dir.is_dir():
        status = "missing_output_dir"
    elif not mpj_files:
        status = "missing_mpj"
    elif required:
        status = f"missing_expected_{expected_suffix.lstrip('.')}"
    else:
        status = "optional_not_converted"
    return {
        "target_name": target_name,
        "required": required,
        "expected_suffix": expected_suffix,
        "section_input": str(section_input),
        "section_output_dir": str(output_dir),
        "output_dir_exists": output_dir.is_dir(),
        "counts": _mapgis_file_counts(mapgis_files),
        "mpj_files": [str(path) for path in mpj_files],
        "expected_files": [str(path) for path in expected_files],
        "ok": ok,
        "status": status,
    }


def verify_batch(batch_dir: Path) -> dict[str, Any]:
    records = _load_section_manifest(batch_dir)
    summaries = [_summarize_section_output(record) for record in records]
    required_records = [summary for summary in summaries if summary["required"]]
    required_missing = [summary["target_name"] for summary in required_records if not summary["ok"]]
    report = {
        "action": "verify_batch",
        "ok": not required_missing and bool(required_records),
        "batch_dir": str(batch_dir),
        "section_batch_manifest": str(_section_manifest_path(batch_dir)),
        "required_total": len(required_records),
        "required_ok": sum(1 for summary in required_records if summary["ok"]),
        "required_missing": required_missing,
        "optional_targets": [summary["target_name"] for summary in summaries if not summary["required"]],
        "records": summaries,
    }
    _write_json(batch_dir / "section_batch_verify_report.json", report)
    return report


def _finalize_convert_report(report: dict[str, Any], path: Path) -> None:
    report["failure_category"] = classify_bridge_failure(
        str(report.get("status", "")), report.get("automation_stage")
    )
    _write_json(path, report)


def _first_prepared_dxf(prepared_records: Sequence[dict[str, Any]]) -> Path | None:
    for record in prepared_records:
        section_input = Path(record.get("section_input", ""))
        if section_input.suffix.lower() == ".dxf" and section_input.is_file():
            return section_input
    return None


def section_batch_convert(
    *,
    batch_dir: Path,
    section_exe: Path | None = None,
    bootstrap_file: Path | None = DEFAULT_SECTION_BOOTSTRAP_FILE,
    dry_run: bool = False,
    wait_timeout_seconds: int = 300,
) -> dict[str, Any]:
    prepared = _inspect_prepared_batch(batch_dir)
    resolved_section_exe, section_exe_candidates = _resolve_section_exe(section_exe)
    bootstrap_exists = bootstrap_file.is_file() if bootstrap_file is not None else None
    report: dict[str, Any] = {
        "action": "section_batch_convert",
        "ok": False,
        "status": "not_started",
        "conversion_started": False,
        "section_exe": str(resolved_section_exe),
        "section_exe_exists": resolved_section_exe.is_file(),
        "section_exe_candidates": section_exe_candidates,
        "bootstrap_file": str(bootstrap_file) if bootstrap_file is not None else None,
        "bootstrap_file_exists": bootstrap_exists,
        "batch_dir": str(batch_dir),
        "menu_path": list(SECTION_DXF_MENU_PATH),
        "precondition": SECTION_BATCH_CONVERT_PRECONDITION,
        "prepared": prepared,
    }
    if not prepared["ok"]:
        report["status"] = "invalid_batch"
        _finalize_convert_report(report, batch_dir / "section_batch_convert_report.json")
        return report
    if dry_run:
        report["status"] = "dry_run"
        report["note"] = "Dry run validates inputs only; SECTION was not launched and no conversion is claimed."
        _finalize_convert_report(report, batch_dir / "section_batch_convert_report.json")
        return report
    if bootstrap_file is not None and not bootstrap_file.is_file():
        report["status"] = "missing_bootstrap_file"
        report["manual_recovery"] = (
            "Set --bootstrap-file to any existing MapGIS .WL/.WT/.WP/.MPJ file, or open one manually "
            "in SECTION before using 1辅助工具 -> 打开外部数据 -> 批量转换dxf."
        )
        _finalize_convert_report(report, batch_dir / "section_batch_convert_report.json")
        return report
    if not resolved_section_exe.is_file():
        report["status"] = "missing_section_exe"
        report["manual_recovery"] = (
            "Set --section-exe or MAPGIS67_SECTION_EXE to the local section.exe path, then retry. "
            "Run env_probe first on colleague machines because MapGIS/SECTION install paths vary."
        )
        _finalize_convert_report(report, batch_dir / "section_batch_convert_report.json")
        return report

    snapshot_before = _directory_snapshot(batch_dir)
    try:
        automation = _run_section_win32_batch_convert(
            section_exe=resolved_section_exe,
            bootstrap_file=bootstrap_file,
            batch_dir=batch_dir,
            menu_path=SECTION_DXF_MENU_PATH,
        )
        report["conversion_started"] = True
        report["automation"] = automation
    except SectionAutomationError as exc:  # pragma: no cover - depends on SECTION GUI state.
        report["status"] = "automation_failed"
        report["automation_stage"] = exc.stage
        report["automation_diagnostics"] = exc.diagnostics
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["manual_recovery"] = _manual_recovery_for_stage(exc.stage)
        _finalize_convert_report(report, batch_dir / "section_batch_convert_report.json")
        return report
    except Exception as exc:  # pragma: no cover - depends on SECTION GUI state.
        report["status"] = "automation_failed"
        report["automation_stage"] = "unknown"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["manual_recovery"] = (
            "Open or load any MapGIS file in SECTION first, confirm the "
            "1辅助工具 -> 打开外部数据 -> 批量转换dxf menu is visible, then run the prepared batch "
            "folder manually or retry automation after an attach-to-existing-window/bootstrap-file backend is added."
        )
        _finalize_convert_report(report, batch_dir / "section_batch_convert_report.json")
        return report

    deadline = time.monotonic() + wait_timeout_seconds
    verify_report = verify_batch(batch_dir)
    while not verify_report["ok"] and time.monotonic() < deadline:
        time.sleep(2)
        verify_report = verify_batch(batch_dir)

    report["verify"] = verify_report
    report["ok"] = bool(verify_report["ok"])
    report["status"] = "verified" if report["ok"] else "output_verification_failed"
    report["post_trigger_directory_changes"] = _diff_directory_snapshots(
        snapshot_before, _directory_snapshot(batch_dir)
    )
    if not report["ok"]:
        report["post_wait_window_state"] = _post_wait_window_state(
            automation.get("pid"), automation.get("hwnd")
        )
    _finalize_convert_report(report, batch_dir / "section_batch_convert_report.json")
    return report


def w60_batch_convert(
    *,
    batch_dir: Path,
    w60_conv_exe: Path | None = None,
    dry_run: bool = False,
    wait_timeout_seconds: int = 300,
) -> dict[str, Any]:
    prepared = _inspect_prepared_batch(batch_dir)
    resolved_w60_exe, w60_exe_candidates = _resolve_w60_conv_exe(w60_conv_exe)
    selected_dxf = _first_prepared_dxf(prepared.get("records", []))
    report: dict[str, Any] = {
        "action": "w60_batch_convert",
        "ok": False,
        "status": "not_started",
        "conversion_started": False,
        "w60_conv_exe": str(resolved_w60_exe),
        "w60_conv_exe_exists": resolved_w60_exe.is_file(),
        "w60_conv_exe_candidates": w60_exe_candidates,
        "batch_dir": str(batch_dir),
        "selected_dxf": str(selected_dxf) if selected_dxf is not None else None,
        "menu_path": list(W60_DXF_MENU_PATH),
        "prepared": prepared,
    }
    if not prepared["ok"]:
        report["status"] = "invalid_batch"
        _finalize_convert_report(report, batch_dir / "w60_batch_convert_report.json")
        return report
    if selected_dxf is None:
        report["status"] = "missing_batch_dxf"
        report["manual_recovery"] = "Prepare the SECTION/W60 batch first; at least one non-empty .DXF input is required."
        _finalize_convert_report(report, batch_dir / "w60_batch_convert_report.json")
        return report
    if dry_run:
        report["status"] = "dry_run"
        report["note"] = "Dry run validates inputs only; W60_Conv was not launched and no conversion is claimed."
        _finalize_convert_report(report, batch_dir / "w60_batch_convert_report.json")
        return report
    if not resolved_w60_exe.is_file():
        report["status"] = "missing_w60_conv_exe"
        report["manual_recovery"] = (
            "Set --w60-conv-exe or MAPGIS67_W60_CONV_EXE to the local W60_Conv.exe path, then retry. "
            "Run env_probe first on colleague machines because MapGIS/SECTION install paths vary."
        )
        _finalize_convert_report(report, batch_dir / "w60_batch_convert_report.json")
        return report

    snapshot_before = _directory_snapshot(batch_dir)
    try:
        automation = _run_w60_win32_batch_convert(
            w60_conv_exe=resolved_w60_exe,
            selected_dxf=selected_dxf,
        )
        report["conversion_started"] = True
        report["automation"] = automation
    except SectionAutomationError as exc:  # pragma: no cover - depends on W60 GUI state.
        report["status"] = "automation_failed"
        report["automation_stage"] = exc.stage
        report["automation_diagnostics"] = exc.diagnostics
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["manual_recovery"] = _manual_recovery_for_stage(exc.stage)
        _finalize_convert_report(report, batch_dir / "w60_batch_convert_report.json")
        return report
    except Exception as exc:  # pragma: no cover - depends on W60 GUI state.
        report["status"] = "automation_failed"
        report["automation_stage"] = "unknown"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["manual_recovery"] = (
            "Open W60_Conv.exe manually, use 输入 -> 成批转换DXF, select any DXF in the prepared batch, "
            "then run verify_batch/collect_ready. Do not claim conversion until verify_batch passes."
        )
        _finalize_convert_report(report, batch_dir / "w60_batch_convert_report.json")
        return report

    deadline = time.monotonic() + wait_timeout_seconds
    verify_report = verify_batch(batch_dir)
    while not verify_report["ok"] and time.monotonic() < deadline:
        time.sleep(2)
        verify_report = verify_batch(batch_dir)

    report["verify"] = verify_report
    report["ok"] = bool(verify_report["ok"])
    report["status"] = "verified" if report["ok"] else "output_verification_failed"
    report["post_trigger_directory_changes"] = _diff_directory_snapshots(
        snapshot_before, _directory_snapshot(batch_dir)
    )
    if report["ok"]:
        _close_process_windows(automation["pid"])
    else:
        report["post_wait_window_state"] = _post_wait_window_state(
            automation.get("pid"), automation.get("hwnd")
        )
    _finalize_convert_report(report, batch_dir / "w60_batch_convert_report.json")
    return report


def _run_w60_shape_to_wp_automation(
    *,
    w60_conv_exe: Path,
    shp_path: Path,
    target_wp: Path,
) -> dict[str, Any]:
    # DEVNULL: GUI apps never use stdio; inherited handles would otherwise
    # point at the caller's stdout (the engine host's JSONL protocol pipe).
    process = subprocess.Popen(
        [str(w60_conv_exe)],
        cwd=str(w60_conv_exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    startup_warning = _dismiss_w60_startup_warning(process.pid, timeout_seconds=8)
    hwnd = _wait_for_w60_main_window(process.pid, timeout_seconds=30)
    time.sleep(1)
    _activate_section_window(hwnd)

    def _menu_step(
        menu_path: Sequence[str],
        command_name: str,
        fallback_command_id: int,
    ) -> tuple[int, dict[str, Any]]:
        try:
            item_id = _find_menu_item_id(hwnd, menu_path)
        except Exception:
            # Same pattern as the proven DXF flow: W60 menu captions carry
            # accelerator prefixes; the fixed resource command id still works.
            item_id = fallback_command_id
        return item_id, _trigger_menu_command(hwnd, item_id, command_name=command_name)

    def _dialog_step(path_text: str, stage: str, failure_message: str) -> int:
        try:
            dialog_hwnd = _wait_for_process_dialog(
                process.pid, hwnd, timeout_seconds=20, failure_message=failure_message
            )
            _set_dialog_text_with_clipboard_and_enter(dialog_hwnd, path_text)
        except Exception as exc:
            raise SectionAutomationError(
                stage=stage,
                message=str(exc),
                diagnostics=_collect_w60_window_diagnostics(
                    process.pid, hwnd, stage=stage, extra={"dialog_path": path_text}
                ),
            ) from exc
        return dialog_hwnd

    load_id, load_trigger = _menu_step(
        W60_LOAD_SHAPE_MENU_PATH, "w60_load_shape", W60_LOAD_SHAPE_COMMAND_ID
    )
    _dialog_step(
        str(shp_path),
        "w60_load_shape_dialog_failed",
        "W60 load-SHAPE file dialog did not appear",
    )
    # Give W60 time to import the shapefile before the save menu is used.
    time.sleep(W60_SHAPE_LOAD_SETTLE_SECONDS)
    _activate_section_window(hwnd)
    save_id, save_trigger = _menu_step(
        W60_SAVE_WP_MENU_PATH, "w60_save_wp", W60_SAVE_WP_COMMAND_ID
    )
    try:
        first_dialog = _wait_for_process_dialog(
            process.pid,
            hwnd,
            timeout_seconds=20,
            failure_message="W60 save-WP dialog did not appear",
        )
        if _find_dialog_edit_with_retry(first_dialog, timeout_seconds=3) is None:
            # 换名存区 first shows 选择另存文件 — a pick-which-file listbox
            # (确定/取消, no filename field). Confirm the preselected entry;
            # the real save-as filename dialog opens after it.
            if not _click_dialog_button(first_dialog, ("确定", "OK")):
                raise RuntimeError("W60 选择另存文件 dialog has no 确定 button")
            save_dialog = _wait_for_next_process_dialog(
                process.pid,
                hwnd,
                exclude_hwnd=first_dialog,
                timeout_seconds=20,
                failure_message="W60 save-as filename dialog did not appear after 确定",
            )
        else:
            save_dialog = first_dialog
        _set_dialog_text_with_clipboard_and_enter(save_dialog, str(target_wp))
    except Exception as exc:
        raise SectionAutomationError(
            stage="w60_save_wp_dialog_failed",
            message=str(exc),
            diagnostics=_collect_w60_window_diagnostics(
                process.pid,
                hwnd,
                stage="w60_save_wp_dialog_failed",
                extra={"dialog_path": str(target_wp)},
            ),
        ) from exc
    return {
        "pid": process.pid,
        "hwnd": hwnd,
        "window": _window_text(hwnd),
        "startup_warning": startup_warning,
        "load_menu_item_id": load_id,
        "load_menu_trigger": load_trigger,
        "save_menu_item_id": save_id,
        "save_menu_trigger": save_trigger,
    }


def w60_shape_to_wp(
    *,
    shp_path: Path,
    target_wp: Path,
    w60_conv_exe: Path | None = None,
    dry_run: bool = False,
    wait_timeout_seconds: int = 120,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Convert one Shapefile exchange package to a MapGIS .WP via W60_Conv.

    Automates 输入->装入SHAPE文件 then 文件->换名存区 and verifies success
    deterministically: the target .WP must appear on disk, non-empty, with a
    stable size. A failed conversion is reported, never claimed. The WP stays
    review-only (checked=no candidates; manual MapGIS acceptance required).
    """
    shp_path = Path(shp_path)
    target_wp = Path(target_wp)
    resolved_exe, exe_candidates = _resolve_w60_conv_exe(w60_conv_exe)
    report: dict[str, Any] = {
        "action": "w60_shape_to_wp",
        "ok": False,
        "status": "not_started",
        "conversion_started": False,
        "w60_conv_exe": str(resolved_exe),
        "w60_conv_exe_exists": resolved_exe.is_file(),
        "w60_conv_exe_candidates": exe_candidates,
        "shp_path": str(shp_path),
        "target_wp": str(target_wp),
        "load_menu_path": list(W60_LOAD_SHAPE_MENU_PATH),
        "save_menu_path": list(W60_SAVE_WP_MENU_PATH),
        "review_only": True,
        "writes_checked_yes": False,
    }

    def _finish() -> dict[str, Any]:
        if report_path is not None:
            _finalize_convert_report(report, report_path)
        return report

    if not shp_path.is_file() or shp_path.stat().st_size == 0:
        report["status"] = "missing_shp"
        report["manual_recovery"] = (
            "Run the area exchange package first (--include-areas); a non-empty .shp is required."
        )
        return _finish()
    if dry_run:
        report["status"] = "dry_run"
        report["note"] = "Dry run validates inputs only; W60_Conv was not launched and no conversion is claimed."
        return _finish()
    if not resolved_exe.is_file():
        report["status"] = "missing_w60_conv_exe"
        report["manual_recovery"] = (
            "Set --w60-conv-exe or MAPGIS67_W60_CONV_EXE to the local W60_Conv.exe path, then retry."
        )
        return _finish()

    # A pre-existing target would make the save dialog pop an overwrite
    # confirmation the automation does not handle; start clean instead.
    if target_wp.exists():
        target_wp.unlink()
    target_wp.parent.mkdir(parents=True, exist_ok=True)

    try:
        automation = _run_w60_shape_to_wp_automation(
            w60_conv_exe=resolved_exe, shp_path=shp_path, target_wp=target_wp
        )
        report["conversion_started"] = True
        report["automation"] = automation
    except SectionAutomationError as exc:  # pragma: no cover - depends on W60 GUI state.
        report["status"] = "automation_failed"
        report["automation_stage"] = exc.stage
        report["automation_diagnostics"] = exc.diagnostics
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["manual_recovery"] = (
            "Open W60_Conv.exe manually: 输入 -> 装入SHAPE文件 (select the exchange .shp), "
            "then 文件 -> 换名存区 (save as the target .WP). Do not claim conversion until the .WP opens in MapGIS."
        )
        return _finish()
    except Exception as exc:  # pragma: no cover - depends on W60 GUI state.
        report["status"] = "automation_failed"
        report["automation_stage"] = "unknown"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["manual_recovery"] = (
            "Open W60_Conv.exe manually: 输入 -> 装入SHAPE文件, then 文件 -> 换名存区 to the target .WP."
        )
        return _finish()

    # Deterministic success check: the .WP exists, non-empty, size stable.
    deadline = time.monotonic() + wait_timeout_seconds
    last_size = -1
    stable = False
    while time.monotonic() < deadline:
        if target_wp.is_file():
            size = target_wp.stat().st_size
            if size > 0 and size == last_size:
                stable = True
                break
            last_size = size
        time.sleep(2)

    report["ok"] = stable
    report["status"] = "verified" if stable else "output_verification_failed"
    report["target_wp_size"] = target_wp.stat().st_size if target_wp.is_file() else 0
    if stable:
        _close_process_windows(automation["pid"])
    else:
        report["post_wait_window_state"] = _post_wait_window_state(
            automation.get("pid"), automation.get("hwnd")
        )
        report["manual_recovery"] = (
            "W60 did not write the target .WP in time. Check the W60 window for a pending dialog, "
            "or run 输入 -> 装入SHAPE文件 / 文件 -> 换名存区 manually."
        )
    return _finish()


def collect_ready(
    *,
    conversion_list: Path,
    section_batch_dir: Path,
    ready_dir: Path,
    layer_output_to_target: dict[str, str] | None = None,
) -> dict[str, Any]:
    raw_report = collect_section_batch_mapgis_files(
        conversion_list=conversion_list,
        section_batch_manifest=_section_manifest_path(section_batch_dir),
        output_dir=ready_dir,
        layer_output_to_target=layer_output_to_target or {},
    )
    entries = parse_conversion_list(conversion_list)
    required_targets = [
        entry.target_file
        for entry in entries
        if entry.kind == "dxf" and _is_required_line_text_target(entry.target_file)
    ]
    copied_targets = {
        record["target_file"]
        for record in raw_report["records"]
        if record["status"] == "copied" and Path(record["destination"]).is_file()
    }
    missing_required = [target for target in required_targets if target not in copied_targets]
    skipped_optional = [
        record["target_file"]
        for record in raw_report["records"]
        if not _is_required_line_text_target(record["target_file"]) and record["status"] in {"skipped", "missing"}
    ]
    report = {
        **raw_report,
        "action": "collect_ready",
        "ok": not missing_required and bool(required_targets),
        "required_targets": required_targets,
        "missing_required": missing_required,
        "skipped_optional": skipped_optional,
        "raw_missing": raw_report["missing"],
    }
    _write_json(ready_dir / "MAPGIS67_BRIDGE_COLLECT_REPORT.json", report)
    return report


def run_dxf_to_wl_wt_pipeline(
    *,
    source_dir: Path,
    batch_dir: Path,
    conversion_list: Path,
    ready_dir: Path,
    reuse_batch: bool = False,
    skip_section_convert: bool = False,
    conversion_backend: str = "auto",
    section_exe: Path | None = None,
    w60_conv_exe: Path | None = None,
    bootstrap_file: Path | None = DEFAULT_SECTION_BOOTSTRAP_FILE,
    layer_output_to_target: dict[str, str] | None = None,
    wait_timeout_seconds: int = 300,
) -> dict[str, Any]:
    if reuse_batch:
        prepare_report = _inspect_prepared_batch(batch_dir)
        prepare_report["reused"] = True
    else:
        prepare_report = prepare_batch(source_dir=source_dir, batch_dir=batch_dir)

    report: dict[str, Any] = {
        "action": "run_dxf_to_wl_wt_pipeline",
        "ok": False,
        "source_dir": str(source_dir),
        "batch_dir": str(batch_dir),
        "conversion_list": str(conversion_list),
        "ready_dir": str(ready_dir),
        "conversion_backend": conversion_backend,
        "prepare": prepare_report,
    }
    if not prepare_report["ok"]:
        report["status"] = "prepare_failed"
        _write_json(batch_dir / "mapgis67_bridge_pipeline_report.json", report)
        return report

    if skip_section_convert:
        report["convert"] = {
            "action": "section_batch_convert",
            "ok": None,
            "status": "skipped_existing_outputs",
            "conversion_started": False,
            "bootstrap_file": str(bootstrap_file) if bootstrap_file is not None else None,
        }
    else:
        convert_report = _convert_batch_with_backend(
            batch_dir=batch_dir,
            conversion_backend=conversion_backend,
            section_exe=section_exe,
            w60_conv_exe=w60_conv_exe,
            bootstrap_file=bootstrap_file,
            wait_timeout_seconds=wait_timeout_seconds,
        )
        report["convert"] = convert_report
        if not convert_report["ok"]:
            report["status"] = "convert_failed"
            _write_json(batch_dir / "mapgis67_bridge_pipeline_report.json", report)
            return report

    verify_report = verify_batch(batch_dir)
    report["verify"] = verify_report
    if not verify_report["ok"]:
        report["status"] = "verify_failed"
        _write_json(batch_dir / "mapgis67_bridge_pipeline_report.json", report)
        return report

    collect_report = collect_ready(
        conversion_list=conversion_list,
        section_batch_dir=batch_dir,
        ready_dir=ready_dir,
        layer_output_to_target=layer_output_to_target,
    )
    report["collect"] = collect_report
    report["ok"] = bool(collect_report["ok"])
    report["status"] = "ok" if report["ok"] else "collect_failed"
    _write_json(batch_dir / "mapgis67_bridge_pipeline_report.json", report)
    if ready_dir.exists():
        _write_json(ready_dir / "MAPGIS67_BRIDGE_PIPELINE_REPORT.json", report)
    return report


def _convert_batch_with_backend(
    *,
    batch_dir: Path,
    conversion_backend: str,
    section_exe: Path | None,
    w60_conv_exe: Path | None,
    bootstrap_file: Path | None,
    wait_timeout_seconds: int,
) -> dict[str, Any]:
    if conversion_backend == "section":
        return section_batch_convert(
            batch_dir=batch_dir,
            section_exe=section_exe,
            bootstrap_file=bootstrap_file,
            wait_timeout_seconds=wait_timeout_seconds,
        )
    if conversion_backend == "w60":
        return w60_batch_convert(
            batch_dir=batch_dir,
            w60_conv_exe=w60_conv_exe,
            wait_timeout_seconds=wait_timeout_seconds,
        )
    if conversion_backend != "auto":
        return {
            "action": "batch_convert",
            "ok": False,
            "status": "invalid_conversion_backend",
            "conversion_backend": conversion_backend,
            "allowed_backends": ["auto", "section", "w60"],
        }

    attempts: list[dict[str, Any]] = []
    section_report = section_batch_convert(
        batch_dir=batch_dir,
        section_exe=section_exe,
        bootstrap_file=bootstrap_file,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    attempts.append({"backend": "section", "report": section_report})
    if section_report["ok"]:
        return {
            "action": "batch_convert",
            "ok": True,
            "status": "verified",
            "conversion_backend": "auto",
            "selected_backend": "section",
            "attempts": attempts,
        }

    w60_report = w60_batch_convert(
        batch_dir=batch_dir,
        w60_conv_exe=w60_conv_exe,
        wait_timeout_seconds=wait_timeout_seconds,
    )
    attempts.append({"backend": "w60", "report": w60_report})
    return {
        "action": "batch_convert",
        "ok": bool(w60_report["ok"]),
        "status": "verified" if w60_report["ok"] else "output_verification_failed",
        "conversion_backend": "auto",
        "selected_backend": "w60" if w60_report["ok"] else None,
        "attempts": attempts,
    }


def _run_section_win32_batch_convert(
    *,
    section_exe: Path,
    bootstrap_file: Path | None,
    batch_dir: Path,
    menu_path: Sequence[str],
) -> dict[str, Any]:
    import win32con
    import win32gui

    process = subprocess.Popen(
        _section_launch_args(section_exe, bootstrap_file),
        cwd=str(section_exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    hwnd = _wait_for_process_window(process.pid, timeout_seconds=30)
    time.sleep(SECTION_STARTUP_SETTLE_SECONDS)
    _activate_section_window(hwnd)
    bootstrap = {
        "file": str(bootstrap_file),
        "route": "section_exe_argument",
    } if bootstrap_file is not None else None
    _activate_section_window(hwnd)
    try:
        item_id = _find_menu_item_id(hwnd, menu_path)
    except Exception as exc:
        raise SectionAutomationError(
            stage="batch_dxf_menu_not_found",
            message=str(exc),
            diagnostics=_collect_section_window_diagnostics(
                process.pid,
                hwnd,
                stage="batch_dxf_menu_not_found",
                menu_path=menu_path,
            ),
        ) from exc
    trigger = _trigger_menu_command(hwnd, item_id, command_name="batch_dxf")
    try:
        dialog_hwnd = _wait_for_process_dialog(
            process.pid,
            hwnd,
            timeout_seconds=20,
            failure_message="SECTION batch DXF directory dialog did not appear",
        )
    except Exception as exc:
        raise SectionAutomationError(
            stage="batch_dxf_dialog_failed",
            message=str(exc),
            diagnostics=_collect_section_window_diagnostics(
                process.pid,
                hwnd,
                stage="batch_dxf_dialog_failed",
                menu_path=menu_path,
                extra={"menu_trigger": trigger},
            ),
        ) from exc
    try:
        _set_dialog_text_and_confirm(dialog_hwnd, str(batch_dir))
    except Exception as exc:
        raise SectionAutomationError(
            stage="batch_dxf_dialog_confirm_failed",
            message=str(exc),
            diagnostics=_collect_section_window_diagnostics(
                process.pid,
                hwnd,
                stage="batch_dxf_dialog_confirm_failed",
                menu_path=menu_path,
            ),
        ) from exc
    return {
        "pid": process.pid,
        "hwnd": hwnd,
        "window": _window_text(hwnd),
        "bootstrap": bootstrap,
        "dialog": _window_text(dialog_hwnd),
        "menu_item_id": item_id,
        "menu_trigger": trigger,
    }


def _section_launch_args(section_exe: Path, bootstrap_file: Path | None) -> list[str]:
    args = [str(section_exe)]
    if bootstrap_file is not None:
        args.append(str(bootstrap_file))
    return args


def _run_w60_win32_batch_convert(
    *,
    w60_conv_exe: Path,
    selected_dxf: Path,
) -> dict[str, Any]:
    process = subprocess.Popen(
        [str(w60_conv_exe)],
        cwd=str(w60_conv_exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    startup_warning = _dismiss_w60_startup_warning(process.pid, timeout_seconds=8)
    hwnd = _wait_for_w60_main_window(process.pid, timeout_seconds=30)
    time.sleep(1)
    _activate_section_window(hwnd)
    try:
        item_id = _find_menu_item_id(hwnd, W60_DXF_MENU_PATH)
        menu_route = "menu_path"
    except Exception:
        item_id = W60_DXF_MENU_COMMAND_ID
        menu_route = "command_id_fallback"
    trigger = _trigger_menu_command(hwnd, item_id, command_name="w60_batch_dxf")
    try:
        dialog_hwnd = _wait_for_process_dialog(
            process.pid,
            hwnd,
            timeout_seconds=20,
            failure_message="W60 batch DXF file dialog did not appear",
        )
    except Exception as exc:
        raise SectionAutomationError(
            stage="w60_file_dialog_failed",
            message=str(exc),
            diagnostics=_collect_w60_window_diagnostics(
                process.pid,
                hwnd,
                stage="w60_file_dialog_failed",
                extra={"menu_trigger": trigger, "menu_route": menu_route},
            ),
        ) from exc
    try:
        _set_dialog_text_with_clipboard_and_enter(dialog_hwnd, str(selected_dxf))
    except Exception as exc:
        raise SectionAutomationError(
            stage="w60_file_dialog_confirm_failed",
            message=str(exc),
            diagnostics=_collect_w60_window_diagnostics(
                process.pid,
                hwnd,
                stage="w60_file_dialog_confirm_failed",
                extra={"menu_trigger": trigger, "menu_route": menu_route},
            ),
        ) from exc
    return {
        "pid": process.pid,
        "hwnd": hwnd,
        "window": _window_text(hwnd),
        "startup_warning": startup_warning,
        "dialog": _window_text(dialog_hwnd) if _safe_is_window(dialog_hwnd) else "closed",
        "selected_dxf": str(selected_dxf),
        "menu_item_id": item_id,
        "menu_route": menu_route,
        "menu_trigger": trigger,
    }


def _open_section_bootstrap_file(*, pid: int, hwnd: int, bootstrap_file: Path) -> dict[str, Any]:
    import win32con
    import win32gui

    _activate_section_window(hwnd)
    try:
        open_item_id = _find_first_menu_item_id(hwnd, SECTION_OPEN_FILE_MENU_PATHS)
    except Exception as exc:
        raise SectionAutomationError(
            stage="open_bootstrap_menu_not_found",
            message=str(exc),
            diagnostics=_collect_section_window_diagnostics(
                pid,
                hwnd,
                stage="open_bootstrap_menu_not_found",
                menu_path=SECTION_OPEN_FILE_MENU_PATHS[0],
            ),
        ) from exc
    trigger = _trigger_menu_command(hwnd, open_item_id, command_name="open_bootstrap")
    try:
        dialog_hwnd = _wait_for_process_dialog(
            pid,
            hwnd,
            timeout_seconds=20,
            failure_message="SECTION bootstrap open-file dialog did not appear",
        )
    except Exception as exc:
        raise SectionAutomationError(
            stage="open_bootstrap_dialog_failed",
            message=str(exc),
            diagnostics=_collect_section_window_diagnostics(
                pid,
                hwnd,
                stage="open_bootstrap_dialog_failed",
                menu_path=SECTION_OPEN_FILE_MENU_PATHS[0],
                extra={"menu_trigger": trigger},
            ),
        ) from exc
    try:
        _set_dialog_text_and_confirm(dialog_hwnd, str(bootstrap_file))
    except Exception as exc:
        raise SectionAutomationError(
            stage="open_bootstrap_dialog_confirm_failed",
            message=str(exc),
            diagnostics=_collect_section_window_diagnostics(
                pid,
                hwnd,
                stage="open_bootstrap_dialog_confirm_failed",
                menu_path=SECTION_OPEN_FILE_MENU_PATHS[0],
            ),
        ) from exc
    time.sleep(1)
    return {
        "file": str(bootstrap_file),
        "dialog": _window_text(dialog_hwnd),
        "open_menu_item_id": open_item_id,
        "menu_trigger": trigger,
    }


def _window_text(hwnd: int) -> str:
    import win32gui

    return win32gui.GetWindowText(hwnd)


def _manual_recovery_for_stage(stage: str) -> str:
    if stage.startswith("w60_"):
        return (
            f"{stage}: W60_Conv.exe automation did not finish selecting the prepared DXF batch. "
            "Close stale W60_Conv windows, retry the bridge, or manually use 输入 -> 成批转换DXF and select any DXF "
            "inside the prepared batch folder. Acceptance still requires verify_batch/collect_ready."
        )
    if stage.startswith("open_bootstrap_"):
        return (
            f"{stage}: SECTION could not open the bootstrap MapGIS file automatically. "
            "Confirm the bootstrap file exists and SECTION can open it, then retry the MCP/CLI bridge. "
            "If SECTION is already open, close stale SECTION windows before retrying so the bridge controls a clean process."
        )
    if stage.startswith("batch_dxf_"):
        return (
            f"{stage}: SECTION reached the batch-conversion phase but did not expose or accept the DXF batch dialog. "
            "Confirm 1辅助工具 -> 打开外部数据 -> 批量转换dxf is visible after the bootstrap file loads, then retry. "
            "If this remains unstable, implement the W60_Conv.exe backend as the next bridge route and keep verify/collect as the acceptance gate."
        )
    return (
        f"{stage}: SECTION automation failed before verified WL/WT outputs were produced. "
        "Keep the prepared DXF batch for diagnosis and do not count conversion as accepted until verify_batch passes."
    )


def _activate_section_window(hwnd: int) -> dict[str, Any]:
    import win32api
    import win32con
    import win32gui
    import win32process

    result: dict[str, Any] = {"hwnd": hwnd, "ok": True, "errors": []}
    foreground_hwnd = win32gui.GetForegroundWindow()
    current_thread = win32api.GetCurrentThreadId()
    target_thread, _target_pid = win32process.GetWindowThreadProcessId(hwnd)
    foreground_thread = 0
    if foreground_hwnd:
        foreground_thread, _foreground_pid = win32process.GetWindowThreadProcessId(foreground_hwnd)
    attached_threads: list[int] = []

    def attach(thread_id: int) -> None:
        if thread_id and thread_id != current_thread:
            try:
                win32process.AttachThreadInput(current_thread, thread_id, True)
                attached_threads.append(thread_id)
            except Exception as exc:  # pragma: no cover - depends on desktop focus policy.
                result["ok"] = False
                result["errors"].append({"action": "attach_thread_input", "error": f"{type(exc).__name__}: {exc}"})

    attach(target_thread)
    attach(foreground_thread)
    try:
        for action, call in (
            ("show", lambda: win32gui.ShowWindow(hwnd, win32con.SW_SHOW)),
            ("restore", lambda: win32gui.ShowWindow(hwnd, win32con.SW_RESTORE) if win32gui.IsIconic(hwnd) else None),
            ("bring_to_top", lambda: win32gui.BringWindowToTop(hwnd)),
            ("set_foreground", lambda: win32gui.SetForegroundWindow(hwnd)),
            ("set_focus", lambda: win32gui.SetFocus(hwnd)),
        ):
            try:
                call()
            except Exception as exc:  # pragma: no cover - depends on desktop focus policy.
                result["ok"] = False
                result["errors"].append({"action": action, "error": f"{type(exc).__name__}: {exc}"})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if win32gui.GetForegroundWindow() == hwnd:
                break
            time.sleep(0.1)
    finally:
        for thread_id in attached_threads:
            try:
                win32process.AttachThreadInput(current_thread, thread_id, False)
            except Exception as exc:  # pragma: no cover - depends on desktop focus policy.
                result["ok"] = False
                result["errors"].append({"action": "detach_thread_input", "error": f"{type(exc).__name__}: {exc}"})
    result["foreground_hwnd"] = win32gui.GetForegroundWindow()
    result["foreground_title"] = win32gui.GetWindowText(result["foreground_hwnd"]) if result["foreground_hwnd"] else ""
    return result


def _trigger_menu_command(hwnd: int, item_id: int, *, command_name: str) -> dict[str, Any]:
    import win32con
    import win32gui

    result: dict[str, Any] = {
        "command_name": command_name,
        "hwnd": hwnd,
        "item_id": item_id,
        "method": "send_message_timeout",
        "ok": None,
    }
    try:
        result["send_result"] = win32gui.SendMessageTimeout(
            hwnd,
            win32con.WM_COMMAND,
            item_id,
            0,
            win32con.SMTO_ABORTIFHUNG,
            2000,
        )
        result["ok"] = True
    except Exception as exc:  # pragma: no cover - depends on SECTION GUI state.
        timeout_code = 1460
        error_code = getattr(exc, "winerror", None)
        if error_code is None and getattr(exc, "args", None):
            error_code = exc.args[0]
        if error_code == timeout_code:
            result["ok"] = None
            result["timed_out_waiting_for_command_return"] = True
            result["note"] = "Timeout can be expected when the command opens a modal SECTION dialog."
        else:
            result["ok"] = False
            result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def _collect_section_window_diagnostics(
    pid: int,
    main_hwnd: int,
    *,
    stage: str,
    menu_path: Sequence[str] | Sequence[Sequence[str]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        import win32gui
        import win32process
    except Exception as exc:  # pragma: no cover - depends on optional pywin32.
        return {"stage": stage, "diagnostic_error": f"{type(exc).__name__}: {exc}"}

    windows: list[dict[str, Any]] = []

    def collect(hwnd: int, _extra: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid != pid:
            return
        windows.append(
            {
                "hwnd": hwnd,
                "title": win32gui.GetWindowText(hwnd),
                "class_name": _window_class_name(hwnd),
                "is_main_window": hwnd == main_hwnd,
            }
        )

    win32gui.EnumWindows(collect, None)
    foreground_hwnd = win32gui.GetForegroundWindow()
    diagnostics: dict[str, Any] = {
        "stage": stage,
        "pid": pid,
        "main_hwnd": main_hwnd,
        "window_title": win32gui.GetWindowText(main_hwnd),
        "window_class_name": _window_class_name(main_hwnd),
        "foreground_hwnd": foreground_hwnd,
        "foreground_title": win32gui.GetWindowText(foreground_hwnd) if foreground_hwnd else "",
        "visible_windows": windows,
        "visible_dialogs": [window for window in windows if window["class_name"] == "#32770"],
        "menu_path": _jsonable_menu_path(menu_path),
        "menu": _menu_snapshot(main_hwnd),
    }
    if extra:
        diagnostics.update(extra)
    return diagnostics


def _collect_w60_window_diagnostics(
    pid: int,
    main_hwnd: int | None,
    *,
    stage: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        import win32gui
    except Exception as exc:  # pragma: no cover - depends on optional pywin32.
        return {"stage": stage, "diagnostic_error": f"{type(exc).__name__}: {exc}"}

    foreground_hwnd = win32gui.GetForegroundWindow()
    diagnostics: dict[str, Any] = {
        "stage": stage,
        "pid": pid,
        "main_hwnd": main_hwnd,
        "window_title": win32gui.GetWindowText(main_hwnd) if main_hwnd else "",
        "foreground_hwnd": foreground_hwnd,
        "foreground_title": win32gui.GetWindowText(foreground_hwnd) if foreground_hwnd else "",
        "visible_windows": _process_visible_window_records(pid),
        "menu_path": list(W60_DXF_MENU_PATH),
    }
    if main_hwnd:
        diagnostics["menu"] = _menu_snapshot(main_hwnd)
    if extra:
        diagnostics.update(extra)
    return diagnostics


def _process_visible_window_records(pid: int) -> list[dict[str, Any]]:
    import win32gui
    import win32process

    records: list[dict[str, Any]] = []

    def collect(hwnd: int, _extra: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid != pid:
            return
        records.append(
            {
                "hwnd": hwnd,
                "title": win32gui.GetWindowText(hwnd),
                "class_name": _window_class_name(hwnd),
            }
        )

    win32gui.EnumWindows(collect, None)
    return records


def _window_class_name(hwnd: int) -> str:
    import win32gui

    try:
        return win32gui.GetClassName(hwnd)
    except Exception:  # pragma: no cover - defensive for stale window handles.
        return ""


def _jsonable_menu_path(menu_path: Sequence[str] | Sequence[Sequence[str]] | None) -> list[Any]:
    if menu_path is None:
        return []
    return [list(item) if isinstance(item, tuple) else item for item in menu_path]


def _menu_snapshot(hwnd: int) -> list[dict[str, Any]]:
    try:
        import win32con
        import win32gui

        menu = win32gui.GetMenu(hwnd)
        if not menu:
            return []
        return _menu_snapshot_items(menu, win32con)
    except Exception as exc:  # pragma: no cover - depends on SECTION GUI state.
        return [{"error": f"{type(exc).__name__}: {exc}"}]


def _menu_snapshot_items(menu: int, win32con: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    import win32gui

    if depth > 2:
        return []
    items: list[dict[str, Any]] = []
    for index in range(win32gui.GetMenuItemCount(menu)):
        submenu = win32gui.GetSubMenu(menu, index)
        item: dict[str, Any] = {
            "index": index,
            "caption": _menu_item_text(menu, index, win32con),
            "item_id": win32gui.GetMenuItemID(menu, index),
        }
        if submenu:
            item["children"] = _menu_snapshot_items(submenu, win32con, depth=depth + 1)
        items.append(item)
    return items


def _wait_for_process_window(pid: int, *, timeout_seconds: int) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        hwnd = _find_process_window(pid)
        if hwnd:
            return hwnd
        time.sleep(0.5)
    raise RuntimeError(f"SECTION main window did not appear for pid {pid}")


def _wait_for_w60_main_window(pid: int, *, timeout_seconds: int) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for window in _process_visible_window_records(pid):
            title = window["title"]
            class_name = window["class_name"]
            if class_name == "#32770":
                continue
            if "W60_Conv" in title or title:
                return int(window["hwnd"])
        time.sleep(0.5)
    raise RuntimeError(f"W60_Conv main window did not appear for pid {pid}")


def _dismiss_w60_startup_warning(pid: int, *, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        for window in _process_visible_window_records(pid):
            if window["class_name"] != "#32770" or window["title"] != "W60_Conv":
                continue
            dialog_hwnd = int(window["hwnd"])
            button_hwnd = _find_button(dialog_hwnd, ("确定", "OK"))
            if button_hwnd:
                import win32con
                import win32gui

                button_id = win32gui.GetDlgCtrlID(button_hwnd)
                win32gui.SendMessage(dialog_hwnd, win32con.WM_COMMAND, button_id, button_hwnd)
                time.sleep(0.5)
                return {"dismissed": True, "dialog": window, "button_id": button_id}
        if any(window["class_name"] != "#32770" for window in _process_visible_window_records(pid)):
            return {"dismissed": False, "reason": "main_window_visible"}
        time.sleep(0.25)
    return {"dismissed": False, "reason": "warning_not_found"}


def _find_process_window(pid: int) -> int | None:
    import win32gui
    import win32process

    matches: list[int] = []

    def collect(hwnd: int, _extra: object) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid == pid and win32gui.GetWindowText(hwnd):
            matches.append(hwnd)

    win32gui.EnumWindows(collect, None)
    return matches[0] if matches else None


def _wait_for_process_dialog(
    pid: int,
    main_hwnd: int,
    *,
    timeout_seconds: int,
    failure_message: str = "SECTION dialog did not appear",
) -> int:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        dialog = _find_process_dialog(pid, main_hwnd)
        if dialog:
            return dialog
        time.sleep(0.5)
    raise RuntimeError(failure_message)


def _wait_for_next_process_dialog(
    pid: int,
    main_hwnd: int,
    *,
    exclude_hwnd: int,
    timeout_seconds: int,
    failure_message: str,
) -> int:
    """Wait for a dialog other than ``exclude_hwnd`` (a follow-up dialog)."""
    import win32gui
    import win32process

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        matches: list[int] = []

        def collect(hwnd: int, _extra: object) -> None:
            if hwnd in (main_hwnd, exclude_hwnd) or not win32gui.IsWindowVisible(hwnd):
                return
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
            if window_pid == pid:
                matches.append(hwnd)

        win32gui.EnumWindows(collect, None)
        if matches:
            return matches[0]
        time.sleep(0.5)
    raise RuntimeError(failure_message)


def _click_dialog_button(dialog_hwnd: int, captions: Sequence[str]) -> bool:
    """Press a dialog button by caption; True if a button was found."""
    import win32con
    import win32gui

    button_hwnd = _find_button(dialog_hwnd, captions)
    if not button_hwnd:
        return False
    button_id = win32gui.GetDlgCtrlID(button_hwnd)
    win32gui.SendMessage(dialog_hwnd, win32con.WM_COMMAND, button_id, button_hwnd)
    time.sleep(0.5)
    if _safe_is_window(dialog_hwnd) and win32gui.IsWindowVisible(dialog_hwnd):
        win32gui.SendMessage(button_hwnd, win32con.BM_CLICK, 0, 0)
        time.sleep(0.5)
    return True


def _find_process_dialog(pid: int, main_hwnd: int) -> int | None:
    import win32gui
    import win32process

    matches: list[int] = []

    def collect(hwnd: int, _extra: object) -> None:
        if hwnd == main_hwnd or not win32gui.IsWindowVisible(hwnd):
            return
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid == pid:
            matches.append(hwnd)

    win32gui.EnumWindows(collect, None)
    return matches[0] if matches else None


def _normalize_caption(caption: str) -> str:
    return caption.replace("&", "").replace(" ", "").replace("\t", "").strip().lower()


def _caption_matches(actual: str, expected: str) -> bool:
    actual_norm = _normalize_caption(actual)
    expected_norm = _normalize_caption(expected)
    if not actual_norm or not expected_norm:
        return actual_norm == expected_norm
    if actual_norm.startswith(expected_norm) or expected_norm.startswith(actual_norm):
        return True
    # W60 top-level menus glue the accelerator letter onto the caption
    # ("&I输入" -> "i输入"), which defeats the prefix checks above; a
    # containment check recovers those without touching exact submenu names.
    return len(expected_norm) >= 2 and expected_norm in actual_norm


def _find_menu_item_id(hwnd: int, captions: Sequence[str]) -> int:
    import win32con
    import win32gui

    menu = win32gui.GetMenu(hwnd)
    if not menu:
        raise RuntimeError("SECTION window has no Win32 menu handle")
    item_id = _find_menu_item_id_in_menu(menu, captions, win32con)
    if item_id is None:
        raise RuntimeError("SECTION menu path not found: " + " -> ".join(captions))
    return item_id


def _find_first_menu_item_id(hwnd: int, paths: Sequence[Sequence[str]]) -> int:
    errors: list[str] = []
    for path in paths:
        try:
            return _find_menu_item_id(hwnd, path)
        except RuntimeError as exc:
            errors.append(str(exc))
    raise RuntimeError("SECTION menu path not found in variants: " + " | ".join(errors))


def _find_menu_item_id_in_menu(menu: int, captions: Sequence[str], win32con: Any) -> int | None:
    import win32gui

    if not captions:
        return None
    expected = captions[0]
    for index in range(win32gui.GetMenuItemCount(menu)):
        caption = _menu_item_text(menu, index, win32con)
        if not _caption_matches(caption, expected):
            continue
        submenu = win32gui.GetSubMenu(menu, index)
        if len(captions) == 1:
            item_id = win32gui.GetMenuItemID(menu, index)
            return item_id if item_id != -1 else None
        if submenu:
            found = _find_menu_item_id_in_menu(submenu, captions[1:], win32con)
            if found is not None:
                return found
    return None


def _menu_item_text(menu: int, index: int, win32con: Any) -> str:
    import win32gui

    get_menu_string = getattr(win32gui, "GetMenuString", None)
    if get_menu_string is not None:
        return str(get_menu_string(menu, index, win32con.MF_BYPOSITION))

    import win32gui_struct

    buffer, _extras = win32gui_struct.EmptyMENUITEMINFO(win32con.MIIM_STRING, text_buf_size=512)
    win32gui.GetMenuItemInfo(menu, index, True, buffer)
    return str(win32gui_struct.UnpackMENUITEMINFO(buffer).text or "")


def _set_dialog_text_and_confirm(dialog_hwnd: int, value: str) -> None:
    import win32con
    import win32gui

    edit_hwnd = _find_child_by_class(dialog_hwnd, "Edit")
    if not edit_hwnd:
        raise RuntimeError("Could not find an editable directory field in SECTION dialog")
    win32gui.SetWindowText(edit_hwnd, value)
    if win32gui.GetWindowText(edit_hwnd) != value:
        win32gui.SendMessage(edit_hwnd, win32con.WM_SETTEXT, 0, value)
    button_hwnd = _find_button(dialog_hwnd, ("确定", "OK", "打开"))
    if not button_hwnd:
        raise RuntimeError("Could not find a confirm button in SECTION dialog")
    button_id = win32gui.GetDlgCtrlID(button_hwnd)
    win32gui.SendMessage(dialog_hwnd, win32con.WM_COMMAND, button_id, button_hwnd)
    time.sleep(0.5)
    if win32gui.IsWindow(dialog_hwnd):
        win32gui.SendMessage(button_hwnd, win32con.BM_CLICK, 0, 0)


def _find_dialog_edit_with_retry(dialog_hwnd: int, *, timeout_seconds: float = 6.0) -> int | None:
    """Find the filename Edit control, retrying while the dialog builds itself.

    File dialogs (e.g. W60 的 选择另存文件) can be returned by the dialog wait
    before their child controls exist; some also nest the Edit inside a
    ComboBox. Poll for a while and check both shapes before giving up.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        edit_hwnd = _find_child_by_class(dialog_hwnd, "Edit")
        if edit_hwnd:
            return edit_hwnd
        combo_hwnd = _find_child_by_class(dialog_hwnd, "ComboBox")
        if combo_hwnd:
            edit_hwnd = _find_child_by_class(combo_hwnd, "Edit")
            if edit_hwnd:
                return edit_hwnd
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def _set_dialog_text_with_clipboard_and_enter(dialog_hwnd: int, value: str) -> None:
    import win32api
    import win32con
    import win32gui

    edit_hwnd = _find_dialog_edit_with_retry(dialog_hwnd)
    if not edit_hwnd:
        raise RuntimeError("Could not find an editable file field in W60 dialog")
    _activate_section_window(dialog_hwnd)
    try:
        win32gui.SetFocus(edit_hwnd)
    except Exception:
        win32gui.SendMessage(edit_hwnd, win32con.WM_SETFOCUS, 0, 0)
    _set_clipboard_text(value)
    _send_key_combo(ord("A"), modifiers=[win32con.VK_CONTROL])
    _send_key_combo(ord("V"), modifiers=[win32con.VK_CONTROL])
    time.sleep(0.2)
    if win32gui.GetWindowText(edit_hwnd) != value:
        win32gui.SendMessage(edit_hwnd, win32con.WM_SETTEXT, 0, value)
    win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
    win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.8)
    if _safe_is_window(dialog_hwnd) and win32gui.IsWindowVisible(dialog_hwnd):
        button_hwnd = _find_button(dialog_hwnd, ("打开", "确定", "OK"))
        if button_hwnd:
            win32gui.SendMessage(button_hwnd, win32con.BM_CLICK, 0, 0)
            time.sleep(0.8)
    if _safe_is_window(dialog_hwnd) and win32gui.IsWindowVisible(dialog_hwnd):
        raise RuntimeError("W60 file dialog stayed open after confirming selected DXF")


def _set_clipboard_text(value: str) -> None:
    import win32con
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, value)
    finally:
        win32clipboard.CloseClipboard()


def _send_key_combo(key_code: int, *, modifiers: Sequence[int] = ()) -> None:
    import win32api
    import win32con

    for modifier in modifiers:
        win32api.keybd_event(modifier, 0, 0, 0)
    win32api.keybd_event(key_code, 0, 0, 0)
    win32api.keybd_event(key_code, 0, win32con.KEYEVENTF_KEYUP, 0)
    for modifier in reversed(modifiers):
        win32api.keybd_event(modifier, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.1)


def _safe_is_window(hwnd: int) -> bool:
    try:
        import win32gui

        return bool(win32gui.IsWindow(hwnd))
    except Exception:  # pragma: no cover - defensive for optional pywin32/stale handles.
        return False


def _close_process_windows(pid: int) -> None:
    try:
        import win32con
        import win32gui
    except Exception:  # pragma: no cover - optional pywin32.
        return

    for window in _process_visible_window_records(pid):
        try:
            win32gui.PostMessage(int(window["hwnd"]), win32con.WM_CLOSE, 0, 0)
        except Exception:
            continue


def _find_child_by_class(parent_hwnd: int, class_name_part: str) -> int | None:
    import win32gui

    matches: list[int] = []

    def collect(hwnd: int, _extra: object) -> None:
        if class_name_part.lower() in win32gui.GetClassName(hwnd).lower():
            matches.append(hwnd)

    win32gui.EnumChildWindows(parent_hwnd, collect, None)
    return matches[0] if matches else None


def _find_button(parent_hwnd: int, captions: Sequence[str]) -> int | None:
    import win32gui

    matches: list[int] = []

    def collect(hwnd: int, _extra: object) -> None:
        if "button" not in win32gui.GetClassName(hwnd).lower():
            return
        text = win32gui.GetWindowText(hwnd)
        if any(_caption_matches(text, caption) for caption in captions):
            matches.append(hwnd)

    win32gui.EnumChildWindows(parent_hwnd, collect, None)
    return matches[0] if matches else None


def _load_layer_map(path: Path | None) -> dict[str, str] | None:
    if path is None:
        return None
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise RuntimeError(f"Layer map must be a JSON object: {path}")
    return {str(key): str(value) for key, value in payload.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal MapGIS67 SECTION bridge for DXF to WL/WT batches.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare_batch")
    prepare_parser.add_argument("--source-dir", type=Path, required=True)
    prepare_parser.add_argument("--batch-dir", type=Path, required=True)

    convert_parser = subparsers.add_parser("section_batch_convert")
    convert_parser.add_argument("--batch-dir", type=Path, required=True)
    convert_parser.add_argument("--section-exe", type=Path)
    convert_parser.add_argument("--bootstrap-file", type=Path, default=DEFAULT_SECTION_BOOTSTRAP_FILE)
    convert_parser.add_argument("--dry-run", action="store_true")
    convert_parser.add_argument("--timeout", type=int, default=300)

    w60_parser = subparsers.add_parser("w60_batch_convert")
    w60_parser.add_argument("--batch-dir", type=Path, required=True)
    w60_parser.add_argument("--w60-conv-exe", type=Path)
    w60_parser.add_argument("--dry-run", action="store_true")
    w60_parser.add_argument("--timeout", type=int, default=300)

    verify_parser = subparsers.add_parser("verify_batch")
    verify_parser.add_argument("--batch-dir", type=Path, required=True)

    collect_parser = subparsers.add_parser("collect_ready")
    collect_parser.add_argument("--conversion-list", type=Path, required=True)
    collect_parser.add_argument("--section-batch-dir", type=Path, required=True)
    collect_parser.add_argument("--ready-dir", type=Path, required=True)
    collect_parser.add_argument("--layer-map-json", type=Path)

    pipeline_parser = subparsers.add_parser("run_dxf_to_wl_wt_pipeline")
    pipeline_parser.add_argument("--source-dir", type=Path, required=True)
    pipeline_parser.add_argument("--batch-dir", type=Path, required=True)
    pipeline_parser.add_argument("--conversion-list", type=Path, required=True)
    pipeline_parser.add_argument("--ready-dir", type=Path, required=True)
    pipeline_parser.add_argument("--reuse-batch", action="store_true")
    pipeline_parser.add_argument("--skip-section-convert", action="store_true")
    pipeline_parser.add_argument("--conversion-backend", choices=["auto", "section", "w60"], default="auto")
    pipeline_parser.add_argument("--section-exe", type=Path)
    pipeline_parser.add_argument("--w60-conv-exe", type=Path)
    pipeline_parser.add_argument("--bootstrap-file", type=Path, default=DEFAULT_SECTION_BOOTSTRAP_FILE)
    pipeline_parser.add_argument("--layer-map-json", type=Path)
    pipeline_parser.add_argument("--timeout", type=int, default=300)

    args = parser.parse_args()
    if args.command == "prepare_batch":
        report = prepare_batch(source_dir=args.source_dir, batch_dir=args.batch_dir)
    elif args.command == "section_batch_convert":
        report = section_batch_convert(
            batch_dir=args.batch_dir,
            section_exe=args.section_exe,
            bootstrap_file=args.bootstrap_file,
            dry_run=args.dry_run,
            wait_timeout_seconds=args.timeout,
        )
    elif args.command == "w60_batch_convert":
        report = w60_batch_convert(
            batch_dir=args.batch_dir,
            w60_conv_exe=args.w60_conv_exe,
            dry_run=args.dry_run,
            wait_timeout_seconds=args.timeout,
        )
    elif args.command == "verify_batch":
        report = verify_batch(args.batch_dir)
    elif args.command == "collect_ready":
        report = collect_ready(
            conversion_list=args.conversion_list,
            section_batch_dir=args.section_batch_dir,
            ready_dir=args.ready_dir,
            layer_output_to_target=_load_layer_map(args.layer_map_json),
        )
    elif args.command == "run_dxf_to_wl_wt_pipeline":
        report = run_dxf_to_wl_wt_pipeline(
            source_dir=args.source_dir,
            batch_dir=args.batch_dir,
            conversion_list=args.conversion_list,
            ready_dir=args.ready_dir,
            reuse_batch=args.reuse_batch,
            skip_section_convert=args.skip_section_convert,
            conversion_backend=args.conversion_backend,
            section_exe=args.section_exe,
            w60_conv_exe=args.w60_conv_exe,
            bootstrap_file=args.bootstrap_file,
            layer_output_to_target=_load_layer_map(args.layer_map_json),
            wait_timeout_seconds=args.timeout,
        )
    else:  # pragma: no cover - argparse prevents this.
        raise RuntimeError(args.command)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
