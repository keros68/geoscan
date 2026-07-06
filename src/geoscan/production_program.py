from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image

from geoscan.ai_vision_review import (
    AiVisionConfig,
    analyze_map_image_with_ai,
)
from geoscan.area_candidate_workflow import generate_review_area_candidates
from geoscan.candidates import utc_now as _utc_now, write_json as _write_json
from geoscan.mapgis67_bridge import (
    prepare_batch,
    run_dxf_to_wl_wt_pipeline,
)
from geoscan.env_probe import (
    DONGLE_PROCESS_NAME,
    dongle_process_running,
)
from geoscan.grouped_exchange import safe_target_stem
from geoscan.line_candidate_workflow import generate_review_line_candidates
from geoscan.line_connectivity import (
    VALID_LINE_CONNECT_MODES,
    resolve_connectivity_profile,
)
from geoscan.line_repair_stage import RepairStageConfig, generate_repaired_line_candidates
from geoscan.text_interference import write_text_flagged_line_export
from geoscan.raster_enhance import ENHANCE_PRESETS, enhance_image_file
from geoscan.raster_level import (
    LevelParams,
    level_to_rgb_tiff,
    needs_leveling,
)
from geoscan.production_accuracy_workflow import (
    short_output_root_for_map_id,
    write_area_exchange_package,
    write_line_exchange_package,
    write_text_placeholder_exchange_package,
)
from geoscan.text_candidate_workflow import generate_review_text_candidates


PROGRAM_NAME = "mapgis_accuracy_workflow"


class RunCancelledError(RuntimeError):
    """Cooperative stop requested by the caller; raised only at stage boundaries."""


class DonglePrecheckError(RuntimeError):
    """cli conversion requested but the MapGIS dongle service is not running.

    Raised before any vectorization work so the run fails fast instead of wasting
    the whole pipeline and only failing at the final SECTION/W60 timeout.
    """


def _dongle_precheck_message() -> str:
    return (
        f"MapGIS 密码狗服务 {DONGLE_PROCESS_NAME} 未在运行——cli 转换会在最后一步失败。\n"
        f"请插好加密狗并启动 {DONGLE_PROCESS_NAME} 后重试；"
        "或把转换模式改为 none / prepare（只出候选和 DXF，不需要密码狗）。\n"
        f"MapGIS dongle service {DONGLE_PROCESS_NAME} is not running; cli conversion "
        "would fail at the last step. Plug in the dongle and start "
        f"{DONGLE_PROCESS_NAME}, then retry — or use conversion-mode none/prepare."
    )
VALID_CONVERSION_MODES = {"none", "prepare", "cli"}
VALID_LINE_REPAIR_MODES = {"off", "conservative"}
VALID_LINE_EXPORT_SOURCES = {"raw", "repaired", "ai_enhanced"}
VALID_LINE_ENGINES = {"hough", "trace"}
VALID_LEVEL_INPUT_MODES = {"auto", "force", "off"}
VALID_ENHANCED_PREVIEW_MODES = {"none"} | set(ENHANCE_PRESETS)
PIXEL_UNIT_DPI = 25.4


def conversion_outcome(conversion: Any) -> str:
    """Single source of truth: what did the conversion stage mean for this run?

    Returns "converted" | "prepared" | "skipped" | "failed". New reports carry
    an explicit ``outcome`` key (stamped by ``run_production_program``); old
    PROGRAM_RUN_REPORT.json files on disk predate it, so the legacy
    status/ok/mode derivation below must stay (the console history view still
    parses them). ``no_text_package`` is the historical name of
    ``no_exchange_package``.
    """
    if not isinstance(conversion, dict) or not conversion:
        return "failed"
    outcome = conversion.get("outcome")
    if isinstance(outcome, str) and outcome:
        return outcome
    status = str(conversion.get("status") or "")
    mode = str(conversion.get("mode") or "")
    if status == "converted" and conversion.get("ok") is True:
        return "converted"
    if status == "prepared":
        return "prepared"
    if status in {"not_requested", "no_exchange_package", "no_text_package"} or mode == "none":
        return "skipped"
    return "failed"


@dataclass(frozen=True)
class ProgramConfig:
    project_root: Path
    source_raster: Path
    map_id: str
    output_root: Path | None = None
    line_candidates: Path | None = None
    text_candidates: Path | None = None
    target_line_file: str | None = None
    target_text_file: str | None = None
    target_area_file: str | None = None
    ai_provider: str = "none"
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    conversion_mode: str = "prepare"
    line_engine: str = "hough"
    # Connectivity level: how aggressively broken strokes are reconnected
    # (engine gap thresholds + deterministic ink-evidence bridging).
    # "conservative" reproduces the historical behavior exactly.
    line_connect: str = "conservative"
    # Optional numeric fine-tuning on top of the level (None = level default,
    # 0 = off): max endpoint-bridging gap / max ring snap-close gap, in px.
    line_bridge_gap_px: float | None = None
    line_close_gap_px: float | None = None
    line_repair: str = "off"
    line_export_source: str = "raw"
    ai_enhance: bool = False
    export_dxf: bool = True
    # QGIS alignment files in the deliverable folder: .tfw world files for the
    # rasters + mm-unit GeoJSON copies. Cheap to produce; off only when the
    # user explicitly deselects the QGIS output category.
    qgis_files: bool = True
    auto_generate_line_candidates: bool = True
    auto_generate_text_candidates: bool = True
    include_areas: bool = False
    reset_output: bool = False
    wait_timeout_seconds: int = 300
    ocr_python: Path | None = None
    # Level (deskew + RGB TIFF) the input before vectorizing. "auto" levels
    # non-TIFF inputs (jpg/png/bmp) and passes TIFFs through; "force" always
    # levels; "off" never levels. Vectorization then reads the leveled raster.
    level_input: str = "off"
    # Extra human-viewing backdrop (sharpen/contrast) written from the pixel-unit
    # raster, so its geometry is identical and vectors overlay 1:1. "none" or an
    # ENHANCE_PRESETS name. Vectorization/OCR never read it.
    enhanced_preview: str = "standard"
    # Escape hatch for the cli dongle pre-flight: if the dongle service happens to
    # be named differently on a machine, this lets the run proceed anyway.
    skip_dongle_check: bool = False


def sanitize_map_id(text: str) -> str:
    """Fold arbitrary text into a folder-safe map id.

    Keeps alphanumerics (ASCII digits/letters and CJK, matching
    ``short_output_root_for_map_id``) and single underscores; every other run of
    characters (spaces, dots, hyphens, punctuation) collapses to one underscore.
    Returns "" only when nothing usable remains.
    """
    chars: list[str] = []
    for char in str(text):
        if char.isalnum() or char == "_":
            chars.append(char)
        elif chars and chars[-1] != "_":
            chars.append("_")
    return "".join(chars).strip("_").upper()


def derive_map_id_from_filename(path: Path | str) -> str:
    """Best-effort map id from an input raster filename.

    Prefers the standard ``t<digits>_<digits>`` convention (``t01_0007`` ->
    ``T01_0007``, also matched when embedded, e.g. ``scan_t01_0007_final``);
    otherwise sanitizes the whole stem so numeric or free-form names still yield
    a usable id (``12345`` -> ``12345``, ``嫩北矿区 3`` -> ``嫩北矿区_3``).
    Returns "" only when the stem has no usable character at all.
    """
    stem = Path(path).stem
    match = re.search(r"(t\d+_\d+)", stem, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return sanitize_map_id(stem)


def _default_target_file(map_id: str, kind: str, extension: str, fallback: str) -> str:
    numbers = re.findall(r"\d+", map_id)
    if not numbers:
        return fallback
    suffix = numbers[-1][-2:].zfill(2)
    return f"T{suffix}{kind}.{extension}"


def default_text_target_file(map_id: str) -> str:
    return _default_target_file(map_id, "TXT", "WT", "TEXTTXT.WT")


def default_line_target_file(map_id: str) -> str:
    return _default_target_file(map_id, "LINE", "WL", "LINE.WL")


def default_area_target_file(map_id: str) -> str:
    return _default_target_file(map_id, "AREA", "WP", "AREA.WP")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_geojson_features(path: Path) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return len(payload.get("features", []))


def _resolve_output_root(project_root: Path, map_id: str, output_root: Path | None) -> Path:
    expected = short_output_root_for_map_id(project_root, map_id).resolve()
    if output_root is None:
        return expected
    actual = Path(output_root).resolve()
    if actual.name.upper() != expected.name.upper():
        raise ValueError(f"Use the short output root folder name for this map: {expected.name}")
    return actual


def redact_api_key(api_key: str) -> str:
    value = str(api_key or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _backup_load_ready_before_reset(output_root: Path) -> dict[str, Any] | None:
    """Copy hand-editable MAPGIS_LOAD_READY* contents aside before a reset deletes them.

    Rasters (*.tif) are excluded: they are large and regenerated deterministically.
    """
    load_dirs = sorted(
        child
        for child in output_root.iterdir()
        if child.is_dir() and child.name.upper().startswith("MAPGIS_LOAD_READY")
    )
    if not load_dirs:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = output_root.parent / f"{output_root.name}_LAST_READY_BACKUP" / stamp
    copied: list[str] = []
    for load_dir in load_dirs:
        for src in sorted(load_dir.rglob("*")):
            if not src.is_file() or src.suffix.lower() in {".tif", ".tiff"}:
                continue
            rel = src.relative_to(output_root)
            dst = backup_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(str(rel))
    if not copied:
        return None
    return {
        "backup_root": str(backup_root),
        "file_count": len(copied),
        "files": copied,
        "note": "Automatic pre-reset backup of MAPGIS_LOAD_READY contents (rasters excluded).",
    }


def _ensure_fresh_output_root(output_root: Path, *, reset_output: bool) -> dict[str, Any] | None:
    reset_backup: dict[str, Any] | None = None
    if output_root.exists():
        if not reset_output:
            existing = [path for path in output_root.iterdir()]
            if existing:
                raise FileExistsError(
                    f"Output root already contains files: {output_root}. "
                    "Pass --reset-output only when starting a deliberate fresh run."
                )
        else:
            reset_backup = _backup_load_ready_before_reset(output_root)
            shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    return reset_backup


def _copy_input_freeze(
    *, source_raster: Path, output_root: Path, map_id: str, level_input: str = "off"
) -> dict[str, Any]:
    if not source_raster.is_file():
        raise FileNotFoundError(source_raster)

    freeze_dir = output_root / "00_INPUT_FREEZE"
    freeze_dir.mkdir(parents=True, exist_ok=True)
    frozen_raster = freeze_dir / f"{map_id.lower()}_source_frozen{source_raster.suffix.lower()}"
    shutil.copy2(source_raster, frozen_raster)

    # The original is always frozen byte-for-byte for provenance. When leveling
    # is requested, downstream stages read a leveled RGB TIFF derived from the
    # frozen original; otherwise they read the frozen original directly.
    leveling: dict[str, Any] | None = None
    if needs_leveling(frozen_raster, level_input):
        leveled_raster = freeze_dir / f"{map_id.lower()}_source_leveled.tif"
        leveling = level_to_rgb_tiff(frozen_raster, leveled_raster, LevelParams())
        leveling["mode"] = level_input
        working_raster = leveled_raster
    else:
        working_raster = frozen_raster

    with Image.open(working_raster) as image:
        width, height = image.size
        dpi = image.info.get("dpi")
        mode = image.mode
    dpi_value = [float(value) for value in dpi] if dpi else None

    report = {
        "input_freeze_created": True,
        "source_raster": str(source_raster),
        "frozen_raster": str(frozen_raster),
        "working_raster": str(working_raster),
        "leveling": leveling,
        "source_sha256": _sha256(source_raster),
        "frozen_sha256": _sha256(frozen_raster),
        "width": width,
        "height": height,
        "mode": mode,
        "dpi": dpi_value,
        "created_at_utc": _utc_now(),
    }
    _write_json(freeze_dir / "INPUT_MANIFEST.json", report)
    return report


def _write_pixel_unit_raster(*, frozen_raster: Path, output_root: Path, map_id: str) -> dict[str, Any]:
    freeze_dir = output_root / "00_INPUT_FREEZE"
    output_path = freeze_dir / f"{map_id.lower()}_mapgis_pixel_units.tif"
    with Image.open(frozen_raster) as image:
        width, height = image.size
        dpi = image.info.get("dpi") or (300.0, 300.0)
        dpi_x = float(dpi[0])
        dpi_y = float(dpi[1] if len(dpi) > 1 else dpi[0])
        image.save(output_path, format="TIFF", dpi=(PIXEL_UNIT_DPI, PIXEL_UNIT_DPI), compression="raw")

    report = {
        "source_raster": str(frozen_raster),
        "pixel_unit_raster": str(output_path),
        "source_dpi": [round(dpi_x, 6), round(dpi_y, 6)],
        "target_dpi": [PIXEL_UNIT_DPI, PIXEL_UNIT_DPI],
        "source_size_px": [width, height],
        "source_mm_extent": [
            0.0,
            0.0,
            round(width * 25.4 / dpi_x, 6),
            round(height * 25.4 / dpi_y, 6),
        ],
        "pixel_unit_extent": [0.0, 0.0, float(width), float(height)],
        "scale_factor_from_source_dpi": [
            round(dpi_x / PIXEL_UNIT_DPI, 6),
            round(dpi_y / PIXEL_UNIT_DPI, 6),
        ],
        "export_units": "mm",
        "px_to_mm_scale": [round(25.4 / dpi_x, 8), round(25.4 / dpi_y, 8)],
        "mapgis_import_note": (
            "Internal raster for console/AI pixel-space overlay checks only. "
            "The exported DXF/WL/WT are in millimetres at the source dpi; in MapGIS "
            "overlay them on the source-dpi raster shipped in MAPGIS_LOAD_READY, "
            "not on this pixel-unit file."
        ),
    }
    _write_json(freeze_dir / "RASTER_ALIGNMENT_REPORT.json", report)
    return report


def _link_or_copy_file(src: Path, dst: Path) -> dict[str, Any]:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    try:
        os.link(src, dst)
        method = "hardlink"
    except OSError:
        shutil.copy2(src, dst)
        method = "copy"
    return {
        "source": str(src),
        "destination": str(dst),
        "bytes": dst.stat().st_size,
        "method": method,
    }


def _staging_ready_dir(output_root: Path) -> Path:
    """Internal collection of verified .WL/.WT from the SECTION/W60 conversion.

    Nested under ``08_SECTION_W60`` (not a top-level folder) so it is not a
    confusing sibling of the single user-facing deliverable ``MAPGIS_LOAD_READY``.
    """
    return output_root / "08_SECTION_W60" / "MAPGIS_READY"


def _write_world_file(
    raster_dst: Path, *, px_to_mm: tuple[float, float], height_px: float
) -> Path:
    """ESRI world file so QGIS georeferences the raster copy in sheet-mm.

    Same coordinate system as the exported DXF/GeoJSON: x right, y up, origin
    at the bottom-left of the raster, 1 unit = 1 mm on the map sheet.
    """
    scale_x, scale_y = px_to_mm
    world_path = raster_dst.with_suffix(".tfw")
    world_path.write_text(
        "\n".join(
            [
                f"{scale_x:.10f}",
                "0.0",
                "0.0",
                f"{-scale_y:.10f}",
                f"{0.5 * scale_x:.10f}",
                f"{(float(height_px) - 0.5) * scale_y:.10f}",
                "",
            ]
        ),
        encoding="ascii",
    )
    return world_path


def _write_mapgis_load_ready(
    *,
    output_root: Path,
    map_id: str,
    raster_alignment: dict[str, Any],
    conversion_report: dict[str, Any],
    line_report: dict[str, Any] | None = None,
    text_report: dict[str, Any] | None = None,
    area_report: dict[str, Any] | None = None,
    qgis_files: bool = True,
) -> dict[str, Any]:
    load_dir = output_root / "MAPGIS_LOAD_READY"
    ready_dir = _staging_ready_dir(output_root)
    if load_dir.exists():
        for child in load_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    # Deliverable backdrop = the source-dpi raster the vectors were traced
    # from. MapGIS displays it at physical sheet size (mm), matching the
    # mm-unit WL/WT/DXF 1:1. (Older packages shipped the 25.4-dpi pixel-unit
    # copy instead; that raster stays internal in 00_INPUT_FREEZE now.)
    raster_src = Path(
        str(raster_alignment.get("source_raster") or raster_alignment["pixel_unit_raster"])
    )
    raster_record = _link_or_copy_file(raster_src, load_dir / raster_src.name)
    enhanced_record: dict[str, Any] | None = None
    enhanced_preview = raster_alignment.get("enhanced_preview") or {}
    enhanced_src = Path(str(enhanced_preview.get("target", "")))
    if enhanced_preview and enhanced_src.is_file():
        enhanced_record = _link_or_copy_file(enhanced_src, load_dir / enhanced_src.name)

    # QGIS alignment package: world files for the raster copies + the exported
    # GeoJSON (same mm coordinates as the DXF), so the folder opens in QGIS
    # with rasters and vectors aligned without any manual georeferencing.
    px_to_mm_pair = raster_alignment.get("px_to_mm_scale") or []
    size_px = raster_alignment.get("source_size_px") or []
    world_records: list[str] = []
    qgis_records: list[dict[str, Any]] = []
    if qgis_files and len(px_to_mm_pair) == 2 and len(size_px) == 2:
        px_to_mm = (float(px_to_mm_pair[0]), float(px_to_mm_pair[1]))
        height_px = float(size_px[1])
        for record in (raster_record, enhanced_record):
            if record is None:
                continue
            world_records.append(
                str(
                    _write_world_file(
                        Path(record["destination"]), px_to_mm=px_to_mm, height_px=height_px
                    )
                )
            )
        for label, exchange in (("line", line_report), ("text", text_report), ("area", area_report)):
            if not exchange or exchange.get("output_units") != "mm":
                continue
            geojson_src = Path(str(exchange.get("source_geojson") or ""))
            if geojson_src.is_file():
                record = _link_or_copy_file(geojson_src, load_dir / geojson_src.name)
                record["kind"] = f"{label}_geojson"
                qgis_records.append(record)

    mapgis_records: list[dict[str, Any]] = []
    skipped_empty_files: list[str] = []
    if ready_dir.is_dir():
        for src in sorted(ready_dir.iterdir(), key=lambda path: path.name.lower()):
            if src.suffix.upper() not in {".WL", ".WT", ".WP"} or not src.is_file():
                continue
            if src.stat().st_size == 0:
                skipped_empty_files.append(str(src))
                continue
            mapgis_records.append(_link_or_copy_file(src, load_dir / src.name))

    # Also gather the exchange DXFs here so the deliverable folder holds the DXF
    # + WL/WT + raster together, instead of the DXF living off in 06_/07_.
    dxf_records: list[dict[str, Any]] = []
    for label, exchange in (("line", line_report), ("text", text_report)):
        dxf_path_str = ((exchange or {}).get("dxf_export") or {}).get("path")
        if not dxf_path_str:
            continue
        dxf_src = Path(str(dxf_path_str))
        if dxf_src.is_file():
            record = _link_or_copy_file(dxf_src, load_dir / dxf_src.name)
            record["kind"] = f"{label}_dxf"
            dxf_records.append(record)

    conversion_mode = str(conversion_report.get("mode", ""))
    conversion_ok = conversion_report.get("ok")
    conversion_status = conversion_report.get("status")
    if conversion_mode == "cli":
        complete = bool(conversion_ok) and bool(mapgis_records) and not skipped_empty_files
    else:
        # Conversion was not requested to run; this package is candidates/raster only.
        complete = None

    warning_lines: list[str] = []
    if complete is False:
        marker = load_dir / "INCOMPLETE_DO_NOT_USE.txt"
        marker.write_text(
            "\n".join(
                [
                    "This MAPGIS_LOAD_READY package is INCOMPLETE.",
                    f"Conversion status: {conversion_status}",
                    "The W60/CLI bridge conversion did not produce verified .WL/.WT outputs"
                    " (or produced empty files).",
                    "Do not use this folder for MapGIS editing or acceptance.",
                    "Rerun the conversion (conversion-mode cli) and confirm PROGRAM_RUN_REPORT.json"
                    " reports conversion ok=true.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        warning_lines = [
            "",
            "**WARNING: INCOMPLETE PACKAGE — conversion failed; do not use for MapGIS editing.**",
            f"Conversion status: `{conversion_status}`",
        ]

    readme = load_dir / "README_MAPGIS_LOAD.md"
    readme.write_text(
        "\n".join(
            [
                "# MapGIS Load-Ready Files",
                *warning_lines,
                "",
                f"Map id: `{map_id}`",
                "",
                "Vector units: millimetres on the map sheet (same physical size as the "
                "source scan). Load this source-dpi raster for overlay with the WL/WT files:",
                "",
                f"- `{raster_record['destination']}`",
                *(
                    [
                        "",
                        "Or load this enhanced backdrop instead — same pixel geometry, so the "
                        "vectors overlay 1:1; sharpened/higher-contrast for easier manual editing "
                        "(human viewing only; vectorization did not use it):",
                        "",
                        f"- `{enhanced_record['destination']}`",
                    ]
                    if enhanced_record
                    else []
                ),
                "",
                "MapGIS files in this folder:",
                "",
                *[f"- `{record['destination']}`" for record in mapgis_records],
                "",
                *(
                    [
                        "DXF exchange files (same folder, for reference / re-conversion):",
                        "",
                        *[f"- `{record['destination']}`" for record in dxf_records],
                        "",
                    ]
                    if dxf_records
                    else []
                ),
                *(
                    [
                        "QGIS: open the `.tif` directly (the `.tfw` world file georeferences "
                        "it in sheet-mm) and add the `.geojson` / `.dxf` on top — they share "
                        "the same coordinates, so everything aligns. Ignore the missing-CRS "
                        "warning (this is a plane coordinate system, not an earth CRS).",
                        "",
                        *[f"- `{record['destination']}`" for record in qgis_records],
                        "",
                    ]
                    if world_records
                    else []
                ),
            ]
        ),
        encoding="utf-8",
    )
    report = {
        "load_folder": str(load_dir),
        "raster": raster_record,
        "enhanced_backdrop": enhanced_record,
        "mapgis_files": mapgis_records,
        "dxf_files": dxf_records,
        "world_files": world_records,
        "qgis_files": qgis_records,
        "skipped_empty_files": skipped_empty_files,
        "conversion_mode": conversion_mode,
        "conversion_ok": conversion_ok,
        "conversion_status": conversion_status,
        "complete": complete,
        "readme": str(readme),
        "note": (
            "Deliverable folder: source-dpi raster + mm-unit WL/WT/DXF/GeoJSON. "
            "MapGIS overlays the raster at sheet size; QGIS uses the .tfw world files."
        ),
    }
    _write_json(load_dir / "MAPGIS_LOAD_READY_REPORT.json", report)
    return report


def _exchange_feature_count(report: dict[str, Any]) -> int:
    for key in ("output_line_count", "output_text_count", "output_area_count", "output_feature_count"):
        value = report.get(key)
        if value is not None:
            return int(value)
    return 0


def _exchange_export_record(report: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    shp_export = report.get("shp_export")
    if isinstance(shp_export, dict):
        return "shp", shp_export
    return "dxf", report.get("dxf_export") or {}


def _exchange_status(report: dict[str, Any]) -> dict[str, Any]:
    kind, export = _exchange_export_record(report)
    return {
        "target_file": report.get("target_file"),
        "kind": kind,
        "status": export.get("status"),
        "path": export.get("path"),
        "features": _exchange_feature_count(report),
        "optional": bool(report.get("optional")),
    }


def _copy_exchange_file(
    *,
    source_path: Path,
    exchange_dir: Path,
    target_file: str,
    kind: str,
) -> Path:
    if kind == "dxf":
        dest = exchange_dir / f"{safe_target_stem(target_file)}.dxf"
        if source_path.resolve() != dest.resolve():
            shutil.copy2(source_path, dest)
        return dest

    dest_dir = exchange_dir / safe_target_stem(target_file)
    if source_path.parent.resolve() != dest_dir.resolve():
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(source_path.parent, dest_dir)
    return dest_dir / source_path.name


def _build_combined_exchange_package(
    *,
    output_root: Path,
    exchange_reports: list[dict[str, Any]],
) -> dict[str, Any]:
    exchange_dir = output_root / "08_SECTION_W60" / "grouped_exchange"
    batch_dir = output_root / "08_SECTION_W60" / "section_batch"
    conversion_list = output_root / "08_SECTION_W60" / "CONVERSION_LIST.txt"
    ready_dir = _staging_ready_dir(output_root)
    exchange_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, dict[str, Any]] = {}
    conversion_lines: list[str] = []
    for report in exchange_reports:
        target_file = str(report["target_file"])
        kind, export = _exchange_export_record(report)
        source_path = Path(str(export["path"]))
        dest = _copy_exchange_file(
            source_path=source_path,
            exchange_dir=exchange_dir,
            target_file=target_file,
            kind=kind,
        )
        count = _exchange_feature_count(report)
        manifest[target_file] = {
            "kind": kind,
            "source": str(report.get("source_geojson")),
            "path": str(dest),
            "features": count,
            "optional": bool(report.get("optional")),
        }
        relative_path = str(dest.relative_to(output_root / "08_SECTION_W60")).replace("/", "\\")
        conversion_lines.append(
            f"{target_file}\t{kind}\t{relative_path}\t{count}"
        )

    _write_json(exchange_dir / "manifest.json", manifest)
    conversion_list.write_text("\n".join(conversion_lines) + ("\n" if conversion_lines else ""), encoding="utf-8")
    report = {
        "source_dir": str(exchange_dir),
        "batch_dir": str(batch_dir),
        "conversion_list": str(conversion_list),
        "ready_dir": str(ready_dir),
        "target_count": len(manifest),
        "targets": sorted(manifest),
        "optional_targets": sorted(target for target, record in manifest.items() if record.get("optional")),
    }
    _write_json(output_root / "08_SECTION_W60" / "COMBINED_EXCHANGE_REPORT.json", report)
    return report


def _run_conversion_stage(
    *,
    mode: str,
    exchange_reports: list[dict[str, Any] | None],
    output_root: Path,
    wait_timeout_seconds: int,
) -> dict[str, Any]:
    if mode not in VALID_CONVERSION_MODES:
        raise ValueError(f"conversion_mode must be one of {sorted(VALID_CONVERSION_MODES)}")
    packages = [report for report in exchange_reports if report is not None]
    if not packages:
        return {"mode": mode, "status": "no_exchange_package", "ok": None}

    exchange_statuses = [_exchange_status(report) for report in packages]
    if mode == "none":
        return {"mode": mode, "status": "not_requested", "ok": None, "exchange_statuses": exchange_statuses}

    written_reports = [
        report for report in packages if _exchange_export_record(report)[1].get("status") == "written"
    ]
    written_dxf_reports = [
        report
        for report in written_reports
        if _exchange_export_record(report)[0] == "dxf"
    ]
    if not written_dxf_reports:
        return {
            "mode": mode,
            "status": "dxf_not_exported",
            "ok": False,
            "exchange_statuses": exchange_statuses,
            "note": "At least one written DXF is required before preparing or running SECTION conversion; optional SHP area packages cannot drive SECTION by themselves.",
        }

    combined = _build_combined_exchange_package(output_root=output_root, exchange_reports=written_reports)
    grouped_exchange_dir = Path(combined["source_dir"])
    section_batch_dir = Path(combined["batch_dir"])
    conversion_list = Path(combined["conversion_list"])
    ready_dir = Path(combined["ready_dir"])

    if mode == "prepare":
        prepare_report = prepare_batch(source_dir=grouped_exchange_dir, batch_dir=section_batch_dir)
        return {
            "mode": mode,
            "status": "prepared" if prepare_report["ok"] else "prepare_failed",
            "ok": bool(prepare_report["ok"]),
            "combined_exchange": combined,
            "exchange_statuses": exchange_statuses,
            "prepare": prepare_report,
            "note": "Prepared only. Conversion must be completed by MCP/CLI bridge, not Computer Use.",
        }

    pipeline_report = run_dxf_to_wl_wt_pipeline(
        source_dir=grouped_exchange_dir,
        batch_dir=section_batch_dir,
        conversion_list=conversion_list,
        ready_dir=ready_dir,
        reuse_batch=False,
        skip_section_convert=False,
        conversion_backend="w60",
        wait_timeout_seconds=wait_timeout_seconds,
    )

    # Optional WP area conversion: after the required WL/WT pipeline, drive
    # W60_Conv 装入SHAPE文件 -> 换名存区 for the area Shapefile. The verified
    # .WP lands in the same ready dir, so MAPGIS_LOAD_READY picks it up
    # automatically. Areas stay optional: a WP failure never flips the overall
    # conversion result.
    area_wp_report: dict[str, Any] | None = None
    area_packages = [
        report
        for report in written_reports
        if _exchange_export_record(report)[0] == "shp" and report.get("target_file")
    ]
    if pipeline_report["ok"] and area_packages:
        from geoscan.mapgis67_bridge import w60_shape_to_wp

        area_package = area_packages[0]
        shp_path = Path(str((area_package.get("shp_export") or {}).get("path", "")))
        target_wp = ready_dir / str(area_package["target_file"])
        try:
            area_wp_report = w60_shape_to_wp(
                shp_path=shp_path,
                target_wp=target_wp,
                wait_timeout_seconds=min(wait_timeout_seconds, 180),
                report_path=output_root / "07_AREA_SECTION_W60" / "W60_SHAPE_TO_WP_REPORT.json",
            )
        except Exception as exc:
            area_wp_report = {
                "action": "w60_shape_to_wp",
                "ok": False,
                "status": "automation_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }

    result = {
        "mode": mode,
        "status": "converted" if pipeline_report["ok"] else "conversion_incomplete",
        "ok": bool(pipeline_report["ok"]),
        "combined_exchange": combined,
        "exchange_statuses": exchange_statuses,
        "pipeline": pipeline_report,
        "note": "No Computer Use fallback is accepted; failed bridge conversion remains incomplete.",
    }
    if area_wp_report is not None:
        result["area_wp"] = area_wp_report
    return result


def _run_ai_visual_review_stage(
    *,
    config: ProgramConfig,
    frozen_raster: Path,
    output_root: Path,
) -> dict[str, Any] | None:
    if config.ai_provider == "none":
        return None
    ai_config = AiVisionConfig(
        provider=config.ai_provider,
        base_url=config.ai_base_url,
        api_key=config.ai_api_key,
        model=config.ai_model,
        timeout_seconds=config.wait_timeout_seconds,
    )
    try:
        return analyze_map_image_with_ai(
            ai_config,
            image_path=frozen_raster,
            output_root=output_root,
            map_id=config.map_id,
        )
    except Exception as exc:
        failure_report = {
            "ok": False,
            "provider": ai_config.provider,
            "base_url": ai_config.base_url,
            "model": ai_config.model,
            "map_id": config.map_id,
            "review_only": True,
            "api_key_configured": bool(ai_config.api_key.strip()),
            "api_key_redacted": redact_api_key(ai_config.api_key),
            "writes_coordinates": False,
            "writes_checked_yes": False,
            "error": f"{type(exc).__name__}: {exc}",
            "note": "AI visual review is optional; the fresh run report is still written for debugging.",
        }
        _write_json(output_root / "AI_VISUAL_REVIEW" / "AI_VISUAL_REVIEW_REPORT.json", failure_report)
        return failure_report


def _write_program_readme(path: Path, report: dict[str, Any]) -> None:
    conversion_mode = (report.get("conversion") or {}).get("mode", "cli")
    raster_alignment = report.get("raster_alignment") or {}
    mapgis_load_ready = report.get("mapgis_load_ready") or {}
    text = f"""# MapGIS Accuracy Workflow Program Run

This folder was created by the reusable production program.

Command pattern:

```powershell
python -m geoscan.production_program run --project-root "{report["project_root"]}" --source-raster "<source.tif>" --map-id "{report["map_id"]}" --conversion-mode {conversion_mode} --ai-provider none
```

Rules for this run:

- Fresh input freeze is required.
- Old candidate GeoJSON/CSV outputs are not reused.
- Old MAPGIS_READY outputs are not reused.
- AI is optional review only; required AI steps: 0.
- Computer Use is not allowed as a production dependency.
- If conversion fails, the status stays incomplete.
- Preferred MapGIS load folder: `{mapgis_load_ready.get("load_folder", "")}`
- Exported vectors are in sheet-mm (px * 25.4/dpi); overlay them on the source-dpi raster: `{raster_alignment.get("source_raster", "")}`

Main reports:

- `00_INPUT_FREEZE/INPUT_MANIFEST.json`
- `00_INPUT_FREEZE/RASTER_ALIGNMENT_REPORT.json`
- `MAPGIS_LOAD_READY/MAPGIS_LOAD_READY_REPORT.json`
- `PROGRAM_RUN_REPORT.json`
"""
    if report.get("text_candidate_generation"):
        text += "- `05_TEXT_WORKFLOW/TEXT_CANDIDATE_GENERATION_REPORT.json`\n"
    if report.get("line_candidate_generation"):
        text += "- `04_LINE_WORKFLOW/LINE_CANDIDATE_GENERATION_REPORT.json`\n"
    if report.get("area_candidate_generation"):
        text += "- `05_AREA_WORKFLOW/AREA_CANDIDATE_GENERATION_REPORT.json`\n"
    if report.get("line"):
        text += "- `06_LINE_SECTION_W60/LINE_EXCHANGE_PACKAGE_REPORT.json`\n"
    if report.get("text"):
        text += "- `07_TEXT_SECTION_W60/TEXT_PLACEHOLDER_PACKAGE_REPORT.json`\n"
    if report.get("area"):
        text += "- `07_AREA_SECTION_W60/AREA_EXCHANGE_PACKAGE_REPORT.json`\n"
    if (report.get("conversion") or {}).get("combined_exchange"):
        text += "- `08_SECTION_W60/COMBINED_EXCHANGE_REPORT.json`\n"
    if (report.get("ai") or {}).get("visual_review"):
        text += "- `AI_VISUAL_REVIEW/ai_visual_analysis.json`\n"
    path.write_text(text, encoding="utf-8")


def run_production_program(
    config: ProgramConfig,
    should_stop: Any | None = None,
) -> dict[str, Any]:
    """Run the full pipeline; ``should_stop`` (a nullary callable) is polled at
    stage boundaries and raises :class:`RunCancelledError` when true — a running
    stage (including SECTION/W60 automation) is never interrupted mid-flight."""

    def _stop_check(next_stage: str) -> None:
        if should_stop is not None and should_stop():
            raise RunCancelledError(f"run cancelled before stage: {next_stage}")

    if config.line_engine not in VALID_LINE_ENGINES:
        raise ValueError(f"line_engine must be one of {sorted(VALID_LINE_ENGINES)}")
    connectivity_profile = resolve_connectivity_profile(config.line_connect)
    for override_name, override_value in (
        ("line_bridge_gap_px", config.line_bridge_gap_px),
        ("line_close_gap_px", config.line_close_gap_px),
    ):
        if override_value is not None and float(override_value) < 0:
            raise ValueError(f"{override_name} must be >= 0 (0 turns the pass off)")
    if config.level_input not in VALID_LEVEL_INPUT_MODES:
        raise ValueError(f"level_input must be one of {sorted(VALID_LEVEL_INPUT_MODES)}")
    if config.enhanced_preview not in VALID_ENHANCED_PREVIEW_MODES:
        raise ValueError(
            f"enhanced_preview must be one of {sorted(VALID_ENHANCED_PREVIEW_MODES)}"
        )
    if config.line_repair not in VALID_LINE_REPAIR_MODES:
        raise ValueError(f"line_repair must be one of {sorted(VALID_LINE_REPAIR_MODES)}")
    if config.line_export_source not in VALID_LINE_EXPORT_SOURCES:
        raise ValueError(f"line_export_source must be one of {sorted(VALID_LINE_EXPORT_SOURCES)}")
    if config.line_export_source == "repaired" and config.line_repair == "off":
        raise ValueError(
            "line_export_source=repaired requires line_repair=conservative in the same run; "
            "an old repaired layer must not be reused (fresh-run rule)."
        )
    if config.ai_enhance and config.ai_provider.strip().lower() in {"", "none"}:
        raise ValueError("ai_enhance requires an AI provider (--ai-provider is none).")
    if config.ai_enhance and config.line_repair == "off":
        raise ValueError(
            "ai_enhance runs on the repaired layer; enable line_repair=conservative."
        )
    if config.line_export_source == "ai_enhanced" and not config.ai_enhance:
        raise ValueError(
            "line_export_source=ai_enhanced requires --ai-enhance in the same run; "
            "an old enhanced layer must not be reused (fresh-run rule)."
        )
    if config.line_repair != "off" and config.line_candidates is not None:
        raise ValueError(
            "line_repair requires auto-generated raw candidates in 04_LINE_WORKFLOW; "
            "it cannot run on a user-supplied --line-candidates path."
        )
    # Fail fast BEFORE any vectorization work: a cli run needs the MapGIS dongle
    # service, and without it the conversion only fails at the final ~300s
    # verification timeout after the whole pipeline has already run.
    if (
        config.conversion_mode == "cli"
        and not config.skip_dongle_check
        and not dongle_process_running()
    ):
        raise DonglePrecheckError(_dongle_precheck_message())

    project_root = Path(config.project_root).resolve()
    source_raster = Path(config.source_raster).resolve()
    output_root = _resolve_output_root(project_root, config.map_id, config.output_root)
    reset_backup = _ensure_fresh_output_root(output_root, reset_output=config.reset_output)

    _stop_check("input_freeze")
    input_report = _copy_input_freeze(
        source_raster=source_raster,
        output_root=output_root,
        map_id=config.map_id,
        level_input=config.level_input,
    )
    working_raster = Path(input_report["working_raster"])
    raster_alignment_report = _write_pixel_unit_raster(
        frozen_raster=working_raster,
        output_root=output_root,
        map_id=config.map_id,
    )
    # All exported vectors (DXF/GeoJSON -> WL/WT/WP) are scaled px -> sheet-mm
    # so the map loads at the source scan's physical size in MapGIS/QGIS.
    source_dpi = raster_alignment_report["source_dpi"]
    px_to_mm = (25.4 / float(source_dpi[0]), 25.4 / float(source_dpi[1]))
    if config.enhanced_preview != "none":
        # Derive from the working raster so the pixel geometry matches the
        # vectors exactly, and keep the source dpi so MapGIS/QGIS display it
        # at sheet size like the mm-unit vectors (human-viewing backdrop;
        # vectorization never reads it).
        enhanced_target = working_raster.with_name(
            f"{config.map_id.lower()}_enhanced.tif"
        )
        raster_alignment_report["enhanced_preview"] = enhance_image_file(
            working_raster,
            enhanced_target,
            preset=config.enhanced_preview,
        )

    _stop_check("line_candidates")
    line_candidate_generation: dict[str, Any] | None = None
    line_candidates_path: Path | None = None
    if config.line_candidates is not None:
        line_candidates_path = Path(config.line_candidates).resolve()
        line_candidate_generation = {
            "mode": "provided",
            "ok": True,
            "output_geojson": str(line_candidates_path),
            "feature_count": _count_geojson_features(line_candidates_path),
            "writes_checked_yes": False,
            "note": "User supplied line candidate GeoJSON; no automatic line extraction was run.",
        }
    elif config.auto_generate_line_candidates:
        line_candidate_generation = generate_review_line_candidates(
            source_raster=working_raster,
            output_root=output_root,
            map_id=config.map_id,
            engine=config.line_engine,
            connectivity=config.line_connect,
            bridge_gap_px=config.line_bridge_gap_px,
            close_gap_px=config.line_close_gap_px,
        )
        line_candidates_path = Path(str(line_candidate_generation["output_geojson"]))

    _stop_check("line_repair")
    line_repair_report: dict[str, Any] | None = None
    if config.line_repair == "conservative" and line_candidates_path is not None:
        line_repair_report = generate_repaired_line_candidates(
            output_root=output_root,
            map_id=config.map_id,
            config=RepairStageConfig(
                small_gap_tolerance=connectivity_profile.repair_small_gap_tolerance
            ),
            image_width=float(input_report["width"]),
            image_height=float(input_report["height"]),
            reset=True,
        )

    line_ai_review_report: dict[str, Any] | None = None
    if line_repair_report is not None and config.ai_provider.strip().lower() not in {"", "none"}:
        from geoscan.line_ai_review import run_repaired_line_ai_review

        try:
            line_ai_review_report = run_repaired_line_ai_review(
                AiVisionConfig(
                    provider=config.ai_provider,
                    base_url=config.ai_base_url,
                    api_key=config.ai_api_key,
                    model=config.ai_model,
                ),
                output_root=output_root,
                map_id=config.map_id,
            )
        except Exception as exc:
            line_ai_review_report = {
                "ok": False,
                "stage": "after_repaired_before_export",
                "review_only": True,
                "writes_coordinates": False,
                "writes_checked_yes": False,
                "error": f"{type(exc).__name__}: {exc}",
                "note": "AI line review failed; the deterministic pipeline continued without it.",
            }

    # Text candidates are generated before line export so the optional AI
    # enhance stage can see lines and texts in one pass; the two stages have no
    # data dependency on each other.
    _stop_check("text_candidates")
    text_candidate_generation: dict[str, Any] | None = None
    text_candidates_path: Path | None = None
    if config.text_candidates is not None:
        text_candidates_path = Path(config.text_candidates).resolve()
        text_candidate_generation = {
            "mode": "provided",
            "ok": True,
            "output_geojson": str(text_candidates_path),
            "feature_count": _count_geojson_features(text_candidates_path),
            "fallback_used": False,
            "writes_checked_yes": False,
            "note": "User supplied text candidate GeoJSON; no automatic OCR/text-region generation was run.",
        }
    elif config.auto_generate_text_candidates:
        text_candidate_generation = generate_review_text_candidates(
            source_raster=working_raster,
            output_root=output_root,
            map_id=config.map_id,
            ocr_python=config.ocr_python,
        )
        text_candidates_path = Path(str(text_candidate_generation["output_geojson"]))

    _stop_check("area_candidates")
    area_candidate_generation: dict[str, Any] | None = None
    area_candidates_path: Path | None = None
    if config.include_areas:
        area_candidate_generation = generate_review_area_candidates(
            source_raster=working_raster,
            output_root=output_root,
            map_id=config.map_id,
        )
        area_candidates_path = Path(str(area_candidate_generation["output_geojson"]))

    ai_enhance_report: dict[str, Any] | None = None
    if config.ai_enhance:
        from geoscan.ai_enhance import AiEnhanceThresholds, run_ai_enhance_stage

        try:
            ai_enhance_report = run_ai_enhance_stage(
                AiVisionConfig(
                    provider=config.ai_provider,
                    base_url=config.ai_base_url,
                    api_key=config.ai_api_key,
                    model=config.ai_model,
                ),
                output_root=output_root,
                map_id=config.map_id,
                frozen_raster=working_raster,
                thresholds=AiEnhanceThresholds(
                    max_gap_px=connectivity_profile.ai_max_gap_px,
                    min_dark_coverage=connectivity_profile.ai_min_dark_coverage,
                ),
            )
        except Exception as exc:
            ai_enhance_report = {
                "ok": False,
                "stage": "ai_enhance_after_repair_before_export",
                "nomination_only": True,
                "ai_wrote_coordinates": False,
                "checked_yes_written": False,
                "error": f"{type(exc).__name__}: {exc}",
                "note": "AI enhance failed; the deterministic pipeline continued without it.",
            }

    _stop_check("line_export")
    line_export_path = line_candidates_path
    if config.line_export_source == "repaired":
        if line_repair_report is None:
            raise ValueError(
                "line_export_source=repaired but no repaired layer was generated in this run "
                "(no line candidates were available to repair)."
            )
        line_export_path = Path(str(line_repair_report["repaired_geojson"]))
    elif config.line_export_source == "ai_enhanced":
        if ai_enhance_report is None or not ai_enhance_report.get("ok"):
            raise ValueError(
                "line_export_source=ai_enhanced but the AI enhance stage did not complete in "
                "this run; rerun with a working AI provider or export repaired instead."
            )
        line_export_path = Path(str(ai_enhance_report["enhanced_geojson"]))

    # Split out line candidates living inside text candidate bboxes (glyph
    # outlines of big titles/labels): the main export -> DXF -> WL stays
    # clean, the removed strokes land in a review sidecar. Source layers stay
    # byte-identical; skipped entirely when nothing gets flagged.
    text_interference_report: dict[str, Any] | None = None
    line_export_count = _count_geojson_features(line_export_path) if line_export_path is not None else 0
    if line_export_count > 0 and text_candidates_path is not None:
        line_workflow_dir = output_root / "04_LINE_WORKFLOW"
        flagged_path = (
            line_workflow_dir
            / f"{config.map_id.lower()}_export_line_candidates_text_flagged.geojson"
        )
        text_interference_report = write_text_flagged_line_export(
            line_export_path,
            text_candidates_path,
            flagged_path,
            image_height=float(input_report["height"]),
            sidecar_path=(
                line_workflow_dir
                / f"{config.map_id.lower()}_text_interference_lines.geojson"
            ),
        )
        if text_interference_report.get("written"):
            line_export_path = flagged_path
            line_export_count = int(text_interference_report["kept_count"])

    line_report: dict[str, Any] | None = None
    if line_export_path is not None and line_export_count > 0:
        target_file = config.target_line_file or default_line_target_file(config.map_id)
        line_report = write_line_exchange_package(
            source_geojson=line_export_path,
            output_root=output_root,
            map_id=config.map_id,
            target_file=target_file,
            export_dxf=config.export_dxf,
            px_to_mm=px_to_mm,
        )
        line_report["line_export_source"] = config.line_export_source

    text_report: dict[str, Any] | None = None
    if text_candidates_path is not None and _count_geojson_features(text_candidates_path) > 0:
        target_file = config.target_text_file or default_text_target_file(config.map_id)
        text_report = write_text_placeholder_exchange_package(
            source_geojson=text_candidates_path,
            output_root=output_root,
            map_id=config.map_id,
            target_file=target_file,
            export_dxf=config.export_dxf,
            px_to_mm=px_to_mm,
        )

    area_report: dict[str, Any] | None = None
    if area_candidates_path is not None and _count_geojson_features(area_candidates_path) > 0:
        target_file = config.target_area_file or default_area_target_file(config.map_id)
        area_report = write_area_exchange_package(
            source_geojson=area_candidates_path,
            output_root=output_root,
            map_id=config.map_id,
            target_file=target_file,
            export_shp=config.export_dxf,
            px_to_mm=px_to_mm,
        )

    _stop_check("conversion")
    conversion_report = _run_conversion_stage(
        mode=config.conversion_mode,
        exchange_reports=[line_report, text_report, area_report],
        output_root=output_root,
        wait_timeout_seconds=config.wait_timeout_seconds,
    )
    conversion_report["outcome"] = conversion_outcome(conversion_report)
    ai_visual_report = _run_ai_visual_review_stage(
        config=config,
        frozen_raster=working_raster,
        output_root=output_root,
    )

    report: dict[str, Any] = {
        "program": PROGRAM_NAME,
        "map_id": config.map_id,
        "project_root": str(project_root),
        "output_root": str(output_root),
        "created_at_utc": _utc_now(),
        "fresh_run_acceptance": {
            "input_freeze_created": bool(input_report["input_freeze_created"]),
            "old_candidate_inputs_used": False,
            "old_ocr_csv_used": False,
            "old_mapgis_ready_used": False,
            "reuse_batch": False,
        },
        "reset_backup": reset_backup,
        "ai": {
            "provider": config.ai_provider,
            "base_url": config.ai_base_url,
            "model": config.ai_model,
            "api_key_configured": bool(config.ai_api_key.strip()),
            "api_key_redacted": redact_api_key(config.ai_api_key),
            "required_steps": 0,
            "role": "optional_review_only",
            "writes_coordinates": False,
            "writes_checked_yes": False,
            "visual_review": ai_visual_report,
        },
        "computer_use": {
            "allowed": False,
            "production_dependency": False,
        },
        "input": input_report,
        "raster_alignment": raster_alignment_report,
        "line_engine": config.line_engine,
        "line_connect": config.line_connect,
        "line_bridge_gap_px": config.line_bridge_gap_px,
        "line_close_gap_px": config.line_close_gap_px,
        "text_interference": (
            text_interference_report
            if text_interference_report is not None
            else {"enabled": False}
        ),
        "line_candidate_generation": line_candidate_generation,
        "line_repair": line_repair_report if line_repair_report is not None else {"mode": "off"},
        "line_ai_review": line_ai_review_report,
        "ai_enhance": ai_enhance_report if ai_enhance_report is not None else {"enabled": False},
        "line_export_source": config.line_export_source,
        "line": line_report,
        "text_candidate_generation": text_candidate_generation,
        "text": text_report,
        "area_candidate_generation": area_candidate_generation,
        "area": area_report,
        "conversion": conversion_report,
    }
    report["mapgis_load_ready"] = _write_mapgis_load_ready(
        output_root=output_root,
        map_id=config.map_id,
        raster_alignment=raster_alignment_report,
        conversion_report=conversion_report,
        line_report=line_report,
        text_report=text_report,
        area_report=area_report,
        qgis_files=config.qgis_files,
    )
    _write_json(output_root / "PROGRAM_RUN_REPORT.json", report)
    _write_program_readme(output_root / "WORKFLOW_PROGRAM_README.md", report)
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapgis-accuracy-workflow",
        description="Reusable MapGIS accuracy workflow package builder.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Create a fresh reusable workflow package.")
    run.add_argument("--project-root", type=Path, default=Path.cwd())
    run.add_argument("--source-raster", type=Path, required=True)
    run.add_argument("--map-id", required=True)
    run.add_argument("--output-root", type=Path)
    run.add_argument("--line-candidates", type=Path)
    run.add_argument("--text-candidates", type=Path)
    run.add_argument("--target-line-file")
    run.add_argument("--target-text-file")
    run.add_argument("--target-area-file")
    run.add_argument("--ai-provider", default="none")
    run.add_argument("--ai-base-url", default="")
    run.add_argument("--ai-api-key", default="")
    run.add_argument("--ai-model", default="")
    run.add_argument("--conversion-mode", choices=sorted(VALID_CONVERSION_MODES), default="prepare")
    run.add_argument(
        "--line-engine",
        choices=sorted(VALID_LINE_ENGINES),
        default="hough",
        help="Line candidate engine: hough (production default, straight only) or trace (centerline tracing, straight + curve + loop).",
    )
    run.add_argument(
        "--line-connect",
        choices=sorted(VALID_LINE_CONNECT_MODES),
        default="conservative",
        help=(
            "Line connectivity level. conservative (default): historical behavior, no "
            "extra reconnection. standard/aggressive: engines jump larger breaks and a "
            "deterministic bridging pass reconnects endpoints where the raster shows ink "
            "along the whole bridge (never invents lines; all candidates stay checked=no)."
        ),
    )
    run.add_argument(
        "--line-bridge-gap-px",
        type=float,
        default=None,
        help=(
            "Fine-tune: max endpoint-bridging gap in px, overriding the level "
            "(standard=60, aggressive=100). 0 disables bridging; omit to use the level."
        ),
    )
    run.add_argument(
        "--line-close-gap-px",
        type=float,
        default=None,
        help=(
            "Fine-tune: max ring snap-close gap in px for nearly-closed polylines "
            "(legend boxes, closed outlines), overriding the level (standard=12, "
            "aggressive=20). 0 disables; omit to use the level."
        ),
    )
    run.add_argument(
        "--level-input",
        choices=sorted(VALID_LEVEL_INPUT_MODES),
        default="off",
        help=(
            "Level (deskew + RGB TIFF) the input before vectorizing: off (default, "
            "never level — for already-processed images), auto (levels non-TIFF "
            "jpg/png/bmp, passes TIFF through), force (always level). Original is "
            "always frozen unchanged."
        ),
    )
    run.add_argument(
        "--enhanced-preview",
        choices=sorted(VALID_ENHANCED_PREVIEW_MODES),
        default="standard",
        help=(
            "Extra human-viewing backdrop written from the working raster "
            "(*_enhanced.tif; same geometry and dpi, vectors overlay 1:1): "
            "none | light | standard (default) | strong. Vectorization never reads it."
        ),
    )
    run.add_argument("--no-export-dxf", action="store_true")
    run.add_argument(
        "--no-qgis-files",
        action="store_true",
        help="Skip the QGIS alignment files (.tfw world files + mm-unit GeoJSON copies) in MAPGIS_LOAD_READY.",
    )
    run.add_argument("--no-auto-line-candidates", action="store_true")
    run.add_argument("--no-auto-text-candidates", action="store_true")
    run.add_argument(
        "--include-areas",
        action="store_true",
        help=(
            "Optional: generate review-only WP area candidates from colored fills and export a "
            "Shapefile exchange package. Results stay checked=no and require manual MapGIS review."
        ),
    )
    run.add_argument(
        "--line-repair",
        choices=sorted(VALID_LINE_REPAIR_MODES),
        default="off",
        help="Generate a repaired line candidate layer (new file; raw is never overwritten).",
    )
    run.add_argument(
        "--line-export-source",
        choices=sorted(VALID_LINE_EXPORT_SOURCES),
        default="raw",
        help=(
            "Which line layer feeds DXF export: raw (default), repaired (requires --line-repair), "
            "or ai_enhanced (requires --ai-enhance in the same run)."
        ),
    )
    run.add_argument(
        "--ai-enhance",
        action="store_true",
        help=(
            "Optional additive AI stage: the model nominates gap bridges / OCR text fixes from a "
            "closed vocabulary; code validates against the raster and writes a NEW enhanced layer. "
            "Requires --ai-provider and --line-repair; raw/repaired outputs are never modified."
        ),
    )
    run.add_argument(
        "--ocr-python",
        type=Path,
        default=None,
        help="External Python interpreter with rapidocr (default: MAPGIS_OCR_PYTHON env var or known conda paths).",
    )
    run.add_argument("--reset-output", action="store_true")
    run.add_argument("--wait-timeout-seconds", type=int, default=300)
    run.add_argument(
        "--skip-dongle-check",
        action="store_true",
        help=(
            "Skip the cli dongle pre-flight (checks the MapGIS dongle service "
            f"{DONGLE_PROCESS_NAME} is running). Use only if the dongle service is "
            "named differently on this machine."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        try:
            report = run_production_program(
                ProgramConfig(
                    project_root=args.project_root,
                    source_raster=args.source_raster,
                    map_id=args.map_id,
                    output_root=args.output_root,
                    line_candidates=args.line_candidates,
                    text_candidates=args.text_candidates,
                    target_line_file=args.target_line_file,
                    target_text_file=args.target_text_file,
                    target_area_file=args.target_area_file,
                    ai_provider=args.ai_provider,
                    ai_base_url=args.ai_base_url,
                    ai_api_key=args.ai_api_key,
                    ai_model=args.ai_model,
                    conversion_mode=args.conversion_mode,
                    line_engine=args.line_engine,
                    line_connect=args.line_connect,
                    line_bridge_gap_px=args.line_bridge_gap_px,
                    line_close_gap_px=args.line_close_gap_px,
                    line_repair=args.line_repair,
                    line_export_source=args.line_export_source,
                    ai_enhance=bool(args.ai_enhance),
                    ocr_python=args.ocr_python,
                    export_dxf=not args.no_export_dxf,
                    qgis_files=not args.no_qgis_files,
                    auto_generate_line_candidates=not args.no_auto_line_candidates,
                    auto_generate_text_candidates=not args.no_auto_text_candidates,
                    include_areas=bool(args.include_areas),
                    reset_output=args.reset_output,
                    wait_timeout_seconds=args.wait_timeout_seconds,
                    level_input=args.level_input,
                    enhanced_preview=args.enhanced_preview,
                    skip_dongle_check=bool(args.skip_dongle_check),
                )
            )
        except DonglePrecheckError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(2)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        conversion = report.get("conversion") or {}
        if conversion.get("ok") is False:
            print(
                "CONVERSION FAILED: status="
                f"{conversion.get('status')}. The W60/CLI bridge did not produce verified "
                ".WL/.WT outputs; this run must not be accepted as converted.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return
    parser.error(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    main()
