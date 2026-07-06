"""Sequential batch operations layer for the ~300-map production task.

Runs the existing single-map production program over a queue of source
rasters, strictly one map at a time (MapGIS/SECTION cannot be parallelized),
with per-map failure isolation and resumability:

- a map whose output folder already holds a ``PROGRAM_RUN_REPORT.json`` is
  skipped (``skipped_completed``) — rerunning the same batch command resumes
  where it stopped;
- a map whose output folder exists without a run report is a crashed/partial
  run; it is NOT silently reset (fresh-run rule) — it is reported as
  ``incomplete_needs_attention`` unless ``--retry-incomplete`` explicitly
  authorizes a reset;
- one failing map never stops the batch; the error lands in its status row.

Status lives in ``BATCH_OPS/BATCH_STATUS.csv`` (+ ``BATCH_RUN_REPORT.json``),
rewritten after every map so an operator can watch progress from Excel.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from geoscan.production_accuracy_workflow import (
    short_output_root_for_map_id,
)
from geoscan.production_program import (
    VALID_CONVERSION_MODES,
    VALID_ENHANCED_PREVIEW_MODES,
    VALID_LEVEL_INPUT_MODES,
    VALID_LINE_ENGINES,
    VALID_LINE_EXPORT_SOURCES,
    VALID_LINE_CONNECT_MODES,
    VALID_LINE_REPAIR_MODES,
    DonglePrecheckError,
    ProgramConfig,
    _dongle_precheck_message,
    derive_map_id_from_filename,
    run_production_program,
)
from geoscan.env_probe import dongle_process_running

BATCH_DIR_NAME = "BATCH_OPS"
STATUS_CSV_NAME = "BATCH_STATUS.csv"
REPORT_NAME = "BATCH_RUN_REPORT.json"
RASTER_SUFFIXES = {".tif", ".tiff"}

STATUS_FIELDS = [
    "map_id",
    "status",
    "source_raster",
    "output_root",
    "line_engine",
    "line_candidates",
    "repaired_candidates",
    "text_candidates",
    "area_candidates",
    "line_dxf_status",
    "text_dxf_status",
    "area_shp_status",
    "conversion_status",
    "elapsed_seconds",
    "finished_at_utc",
    "error",
]


@dataclass(frozen=True)
class BatchConfig:
    project_root: Path
    source_rasters: tuple[Path, ...]
    conversion_mode: str = "none"
    line_engine: str = "hough"
    line_connect: str = "conservative"
    line_bridge_gap_px: float | None = None
    line_close_gap_px: float | None = None
    line_repair: str = "off"
    line_export_source: str = "raw"
    ai_enhance: bool = False
    ai_provider: str = "none"
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    include_areas: bool = False
    export_dxf: bool = True
    qgis_files: bool = True
    ocr_python: Path | None = None
    retry_incomplete: bool = False
    limit: int | None = None
    wait_timeout_seconds: int = 300
    level_input: str = "off"
    enhanced_preview: str = "standard"
    skip_dongle_check: bool = False


def map_id_from_raster(path: Path) -> str:
    """t01_0007.tif -> T01_0007; free-form/numeric names are sanitized, not rejected."""
    map_id = derive_map_id_from_filename(path)
    if not map_id:
        raise ValueError(f"raster stem has no usable map id characters: {path.name}")
    return map_id


def discover_source_rasters(source_dir: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in Path(source_dir).iterdir()
            if path.is_file() and path.suffix.lower() in RASTER_SUFFIXES
        ),
        key=lambda path: path.name.lower(),
    )


def read_map_list_csv(path: Path) -> list[Path]:
    """One raster path per row (first column); '#' comment lines skipped."""
    rasters: list[Path] = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        cell = line.split(",")[0].strip().strip('"')
        if not cell or cell.startswith("#") or cell.lower() in {"source_raster", "raster"}:
            continue
        rasters.append(Path(cell))
    return rasters


def _dxf_status(report: dict[str, Any] | None) -> str:
    if not report:
        return "absent"
    return str((report.get("dxf_export") or {}).get("status") or "absent")


def _shp_status(report: dict[str, Any] | None) -> str:
    if not report:
        return "absent"
    return str((report.get("shp_export") or {}).get("status") or "absent")


def _row_from_run_report(report: dict[str, Any]) -> dict[str, Any]:
    line_generation = report.get("line_candidate_generation") or {}
    text_generation = report.get("text_candidate_generation") or {}
    area_generation = report.get("area_candidate_generation") or {}
    repair = report.get("line_repair") or {}
    return {
        "line_engine": report.get("line_engine") or line_generation.get("engine") or "",
        "line_candidates": line_generation.get("feature_count", ""),
        "repaired_candidates": repair.get("repaired_feature_count", ""),
        "text_candidates": text_generation.get("feature_count", ""),
        "area_candidates": area_generation.get("feature_count", ""),
        "line_dxf_status": _dxf_status(report.get("line")),
        "text_dxf_status": _dxf_status(report.get("text")),
        "area_shp_status": _shp_status(report.get("area")),
        "conversion_status": (report.get("conversion") or {}).get("status", ""),
    }


def _write_status_files(
    batch_dir: Path, rows: list[dict[str, Any]], batch_report: dict[str, Any]
) -> None:
    batch_dir.mkdir(parents=True, exist_ok=True)
    csv_path = batch_dir / STATUS_CSV_NAME
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=STATUS_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in STATUS_FIELDS})
    (batch_dir / REPORT_NAME).write_text(
        json.dumps(batch_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_batch(
    config: BatchConfig,
    *,
    runner: Callable[[ProgramConfig], dict[str, Any]] = run_production_program,
    progress: Callable[[dict[str, Any]], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if config.conversion_mode not in VALID_CONVERSION_MODES:
        raise ValueError(f"conversion_mode must be one of {sorted(VALID_CONVERSION_MODES)}")
    if config.line_engine not in VALID_LINE_ENGINES:
        raise ValueError(f"line_engine must be one of {sorted(VALID_LINE_ENGINES)}")
    if config.line_connect not in VALID_LINE_CONNECT_MODES:
        raise ValueError(
            f"line_connect must be one of {sorted(VALID_LINE_CONNECT_MODES)}"
        )
    if config.line_repair not in VALID_LINE_REPAIR_MODES:
        raise ValueError(f"line_repair must be one of {sorted(VALID_LINE_REPAIR_MODES)}")
    if config.line_export_source not in VALID_LINE_EXPORT_SOURCES:
        raise ValueError(
            f"line_export_source must be one of {sorted(VALID_LINE_EXPORT_SOURCES)}"
        )
    # Abort the whole batch up front if the dongle service is missing — otherwise
    # every cli map fails at its own final timeout. Fail once, loudly, at t=0.
    if (
        config.conversion_mode == "cli"
        and not config.skip_dongle_check
        and not dongle_process_running()
    ):
        raise DonglePrecheckError(_dongle_precheck_message())

    project_root = Path(config.project_root).resolve()
    batch_dir = project_root / BATCH_DIR_NAME
    rows: list[dict[str, Any]] = []
    counts = {
        "completed": 0,
        "failed": 0,
        "skipped_completed": 0,
        "incomplete_needs_attention": 0,
        "not_started": 0,
    }
    started_at = datetime.now(timezone.utc).isoformat()
    batch_report: dict[str, Any] = {
        "program": "batch_runner",
        "project_root": str(project_root),
        "started_at_utc": started_at,
        "conversion_mode": config.conversion_mode,
        "line_engine": config.line_engine,
        "line_repair": config.line_repair,
        "line_export_source": config.line_export_source,
        "include_areas": config.include_areas,
        "one_map_at_a_time": True,
        "queue_size": len(config.source_rasters),
        "counts": counts,
        "rows": rows,
    }

    def _finish_row(row: dict[str, Any], status: str | None = None) -> None:
        """Seal one row: count it, notify, and rewrite the status files.

        The per-map status rewrite is the resumability contract — every branch
        that ends a map MUST go through here.
        """
        if status is not None:
            row["status"] = status
            counts[status] += 1
        rows.append(row)
        if progress:
            progress(dict(row))
        _write_status_files(batch_dir, rows, batch_report)

    executed = 0
    for source_raster in config.source_rasters:
        source_raster = Path(source_raster)
        map_id = map_id_from_raster(source_raster)
        output_root = short_output_root_for_map_id(project_root, map_id)
        run_report_path = output_root / "PROGRAM_RUN_REPORT.json"
        row: dict[str, Any] = {
            "map_id": map_id,
            "source_raster": str(source_raster),
            "output_root": str(output_root),
            "line_engine": config.line_engine,
            "error": "",
        }

        if run_report_path.is_file():
            existing_conversion_failed = False
            try:
                existing_report = json.loads(run_report_path.read_text(encoding="utf-8"))
                row.update(_row_from_run_report(existing_report))
                existing_conversion_failed = (
                    (existing_report.get("conversion") or {}).get("ok") is False
                )
            except (json.JSONDecodeError, OSError) as exc:
                row["error"] = f"unreadable existing run report: {exc}"
            if not existing_conversion_failed:
                _finish_row(row, "skipped_completed")
                continue
            if not config.retry_incomplete:
                row["error"] = (
                    "existing run report has conversion ok=false (failed W60/CLI bridge "
                    "conversion); rerun with --retry-incomplete to reset it deliberately"
                )
                _finish_row(row, "incomplete_needs_attention")
                continue

        reset_output = False
        if output_root.exists() and any(output_root.iterdir()):
            if not config.retry_incomplete:
                row["error"] = (
                    "output folder exists without PROGRAM_RUN_REPORT.json (crashed/partial "
                    "run); rerun with --retry-incomplete to reset it deliberately"
                )
                _finish_row(row, "incomplete_needs_attention")
                continue
            reset_output = True

        stop_requested = should_stop is not None and should_stop()
        if stop_requested:
            batch_report["stopped_early"] = True
        if stop_requested or (config.limit is not None and executed >= config.limit):
            _finish_row(row, "not_started")
            continue

        executed += 1
        start = time.perf_counter()
        try:
            run_report = runner(
                ProgramConfig(
                    project_root=project_root,
                    source_raster=source_raster,
                    map_id=map_id,
                    ai_provider=config.ai_provider,
                    ai_base_url=config.ai_base_url,
                    ai_api_key=config.ai_api_key,
                    ai_model=config.ai_model,
                    include_areas=config.include_areas,
                    conversion_mode=config.conversion_mode,
                    line_engine=config.line_engine,
                    line_connect=config.line_connect,
                    line_bridge_gap_px=config.line_bridge_gap_px,
                    line_close_gap_px=config.line_close_gap_px,
                    line_repair=config.line_repair,
                    line_export_source=config.line_export_source,
                    ai_enhance=config.ai_enhance,
                    export_dxf=config.export_dxf,
                    qgis_files=config.qgis_files,
                    reset_output=reset_output,
                    wait_timeout_seconds=config.wait_timeout_seconds,
                    ocr_python=config.ocr_python,
                    level_input=config.level_input,
                    enhanced_preview=config.enhanced_preview,
                    skip_dongle_check=config.skip_dongle_check,
                )
            )
            row.update(_row_from_run_report(run_report))
            if (run_report.get("conversion") or {}).get("ok") is False:
                row["status"] = "conversion_failed"
                row["error"] = (
                    "conversion ok=false "
                    f"(status: {(run_report.get('conversion') or {}).get('status')})"
                )
                counts["failed"] += 1
            else:
                row["status"] = "completed"
                counts["completed"] += 1
        except Exception as exc:
            row["status"] = "failed"
            row["error"] = f"{type(exc).__name__}: {exc}"
            (batch_dir / "errors").mkdir(parents=True, exist_ok=True)
            (batch_dir / "errors" / f"{map_id}_traceback.txt").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
            counts["failed"] += 1
        row["elapsed_seconds"] = round(time.perf_counter() - start, 1)
        row["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        _finish_row(row)

    batch_report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    _write_status_files(batch_dir, rows, batch_report)
    return batch_report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapgis-batch-runner",
        description="Sequential multi-map driver for the production workflow.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Run the production program over a map queue.")
    run.add_argument("--project-root", type=Path, default=Path.cwd())
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-dir", type=Path, help="Folder of source TIFF rasters.")
    source.add_argument("--map-list", type=Path, help="CSV with one source raster path per row.")
    run.add_argument("--conversion-mode", choices=sorted(VALID_CONVERSION_MODES), default="none")
    run.add_argument("--line-engine", choices=sorted(VALID_LINE_ENGINES), default="hough")
    run.add_argument(
        "--line-connect",
        choices=sorted(VALID_LINE_CONNECT_MODES),
        default="conservative",
        help="Line connectivity level (see production_program --line-connect).",
    )
    run.add_argument(
        "--line-bridge-gap-px",
        type=float,
        default=None,
        help="Fine-tune: max bridging gap px (see production_program --line-bridge-gap-px).",
    )
    run.add_argument(
        "--line-close-gap-px",
        type=float,
        default=None,
        help="Fine-tune: max ring snap-close gap px (see production_program --line-close-gap-px).",
    )
    run.add_argument("--line-repair", choices=sorted(VALID_LINE_REPAIR_MODES), default="off")
    run.add_argument(
        "--line-export-source", choices=sorted(VALID_LINE_EXPORT_SOURCES), default="raw"
    )
    run.add_argument(
        "--ai-enhance",
        action="store_true",
        help="Optional additive AI enhance stage per map (requires --ai-provider and --line-repair).",
    )
    run.add_argument(
        "--include-areas",
        action="store_true",
        help="Optional per-map WP area candidates and Shapefile exchange package.",
    )
    run.add_argument("--ai-provider", default="none")
    run.add_argument("--ai-base-url", default="")
    run.add_argument("--ai-api-key", default="")
    run.add_argument("--ai-model", default="")
    run.add_argument("--ocr-python", type=Path, default=None)
    run.add_argument(
        "--level-input",
        choices=sorted(VALID_LEVEL_INPUT_MODES),
        default="off",
        help="Level (deskew + RGB TIFF) each input before vectorizing (off default | auto | force).",
    )
    run.add_argument(
        "--enhanced-preview",
        choices=sorted(VALID_ENHANCED_PREVIEW_MODES),
        default="standard",
        help="Per map, also write the enhanced human-viewing backdrop (none|light|standard|strong).",
    )
    run.add_argument(
        "--skip-dongle-check",
        action="store_true",
        help="Skip the cli dongle pre-flight (dog67.exe). Use only if the dongle service is named differently.",
    )
    run.add_argument(
        "--retry-incomplete",
        action="store_true",
        help="Reset and rerun maps whose output folder exists without a run report.",
    )
    run.add_argument("--limit", type=int, default=None, help="Run at most N new maps this session.")
    run.add_argument("--wait-timeout-seconds", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    from geoscan.app_settings import bootstrap_settings

    settings_report = bootstrap_settings()
    if settings_report.get("settings_file"):
        print(f"settings: {settings_report['settings_file']} -> {settings_report.get('applied_env')}")
    args = build_arg_parser().parse_args(argv)
    if args.command != "run":
        return 1
    if args.source_dir is not None:
        rasters = discover_source_rasters(args.source_dir)
    else:
        rasters = read_map_list_csv(args.map_list)
    if not rasters:
        print("No source rasters found.")
        return 1

    def print_progress(row: dict[str, Any]) -> None:
        print(
            f"[{row['status']}] {row['map_id']}"
            + (f" — {row['error']}" if row.get("error") else "")
        )

    try:
        report = run_batch(
            BatchConfig(
                project_root=args.project_root,
                source_rasters=tuple(rasters),
                conversion_mode=args.conversion_mode,
                line_engine=args.line_engine,
                line_connect=args.line_connect,
                line_bridge_gap_px=args.line_bridge_gap_px,
                line_close_gap_px=args.line_close_gap_px,
                line_repair=args.line_repair,
                line_export_source=args.line_export_source,
                ai_enhance=bool(args.ai_enhance),
                ai_provider=args.ai_provider,
                ai_base_url=args.ai_base_url,
                ai_api_key=args.ai_api_key,
                ai_model=args.ai_model,
                include_areas=bool(args.include_areas),
                ocr_python=args.ocr_python,
                retry_incomplete=bool(args.retry_incomplete),
                limit=args.limit,
                wait_timeout_seconds=args.wait_timeout_seconds,
                level_input=args.level_input,
                enhanced_preview=args.enhanced_preview,
                skip_dongle_check=bool(args.skip_dongle_check),
            ),
            progress=print_progress,
        )
    except DonglePrecheckError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
    return 0 if report["counts"]["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
