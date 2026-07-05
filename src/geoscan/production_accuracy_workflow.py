from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

from geoscan.dxf_encoding import make_dxf_mapgis_chinese_compatible
from geoscan.dxf_style import PIXEL_TO_ORIGINAL_TIF_MM, mapgis_dxf_label_style
from geoscan.grouped_exchange import grouped_exchange_path, safe_target_stem


DEFAULT_OGR2OGR = Path(r"D:\Qgis\bin\ogr2ogr.exe")
DEFAULT_GDAL_DATA = Path(r"D:\Qgis\apps\gdal\share\gdal")
OGR2OGR_ENV_VAR = "MAPGIS_OGR2OGR"
GDAL_DATA_ENV_VAR = "MAPGIS_GDAL_DATA"
TEXT_PLACEHOLDER_ROUTE = "section_w60_text_dxf"
LINE_EXCHANGE_ROUTE = "section_w60_line_dxf"
AREA_EXCHANGE_ROUTE = "w60_area_shp_optional"


def bundled_gdal_dir() -> Path | None:
    """``gdal/`` folder shipped next to the frozen exe.

    Lets colleague machines run DXF export without a QGIS install; the bundle
    is created by ``packaging/build_gdal_bundle`` docs and copied into
    ``dist/GeoScan/gdal`` by ``build_gui_exe.cmd``.
    """
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).resolve().parent / "gdal"
        if (candidate / "ogr2ogr.exe").is_file():
            return candidate
    return None


def resolve_ogr2ogr(path: Path | None = None) -> Path:
    """Explicit argument -> MAPGIS_OGR2OGR env var -> bundled gdal/ -> known QGIS default."""
    if path is not None:
        return Path(path)
    env_value = os.environ.get(OGR2OGR_ENV_VAR, "").strip()
    if env_value:
        return Path(env_value)
    bundled = bundled_gdal_dir()
    if bundled is not None:
        return bundled / "ogr2ogr.exe"
    return DEFAULT_OGR2OGR


def resolve_gdal_data(path: Path | None = None) -> Path:
    """Explicit argument -> MAPGIS_GDAL_DATA env var -> bundled gdal/ -> known QGIS default."""
    if path is not None:
        return Path(path)
    candidates: list[Path] = []
    env_value = os.environ.get(GDAL_DATA_ENV_VAR, "").strip()
    if env_value:
        candidates.append(Path(env_value))
    bundled = bundled_gdal_dir()
    if bundled is not None:
        candidates.append(bundled / "gdal-data")
    candidates.append(DEFAULT_GDAL_DATA)

    seen: list[Path] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.append(candidate)
        if (candidate / "header.dxf").is_file():
            return candidate
    return seen[0] if seen else DEFAULT_GDAL_DATA


def _has_non_ascii(value: str) -> bool:
    return any(ord(char) > 127 for char in value)


def _windows_short_path(path: Path) -> Path | None:
    """8.3 short form of an existing path, or None when unavailable."""
    if os.name != "nt":
        return None
    import ctypes

    source = str(path)
    needed = ctypes.windll.kernel32.GetShortPathNameW(source, None, 0)
    if needed == 0:
        return None
    buffer = ctypes.create_unicode_buffer(needed)
    if ctypes.windll.kernel32.GetShortPathNameW(source, buffer, needed) == 0:
        return None
    return Path(buffer.value)


def _ascii_temp_root() -> Path | None:
    """A writable directory whose full path is pure ASCII, or None."""
    for candidate in (tempfile.gettempdir(), os.environ.get("ProgramData", ""), r"C:\Temp"):
        candidate = (candidate or "").strip()
        if not candidate or _has_non_ascii(candidate):
            continue
        root = Path(candidate) / "mapgis_vectorize_ascii"
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / "write_probe.txt"
            probe.write_text("ok", encoding="ascii")
            probe.unlink()
        except OSError:
            continue
        return root
    return None


def ascii_safe_env_dir(path: Path, *, purpose: str) -> Path:
    """ASCII-only equivalent of a directory passed to ogr2ogr via env vars.

    ogr2ogr reads GDAL_DATA/PROJ_LIB with ANSI getenv() but decodes file
    paths as UTF-8, so an install folder with Chinese characters makes
    header.dxf unreadable even though Python sees the file (colleague
    failure, 2026-07-04). Order: ASCII paths pass through; try the 8.3
    short path (often disabled on data volumes); copy the directory to an
    ASCII temp folder; otherwise fail loudly with a fix instruction.
    """
    if not _has_non_ascii(str(path)):
        return path
    short = _windows_short_path(path)
    if short is not None and not _has_non_ascii(str(short)):
        return short
    temp_root = _ascii_temp_root()
    if temp_root is not None:
        target = temp_root / purpose.lower()
        shutil.copytree(path, target, dirs_exist_ok=True)
        return target
    raise RuntimeError(
        f"{purpose} 路径包含中文（非 ASCII）字符，ogr2ogr 读不到该目录，DXF 导出无法进行。\n"
        f"当前路径: {path}\n"
        "请把程序整个文件夹移动到全英文路径（例如 E:\\GeoScan）后重新运行。\n"
        f"({purpose} contains non-ASCII characters and no ASCII-safe fallback is "
        "available; move the app folder to an ASCII-only path.)"
    )


def non_ascii_install_path_problem() -> str | None:
    """Startup check: message when DXF export cannot auto-handle a non-ASCII GDAL_DATA path."""
    gdal_data = resolve_gdal_data()
    if not _has_non_ascii(str(gdal_data)):
        return None
    short = _windows_short_path(gdal_data)
    if short is not None and not _has_non_ascii(str(short)):
        return None
    if _ascii_temp_root() is not None:
        return None
    return (
        "程序所在路径包含中文（非 ASCII）字符，且本机没有可用的英文临时目录，"
        "导出 DXF 时会失败。\n"
        f"GDAL_DATA: {gdal_data}\n"
        "请把程序整个文件夹移动到全英文路径（例如 E:\\GeoScan）后重新打开。"
    )


def short_output_root_for_map_id(project_root: Path, map_id: str) -> Path:
    compact = "".join(char for char in str(map_id).upper() if char.isalnum() or char == "_").strip("_")
    if not compact:
        raise ValueError("map_id is required")
    return Path(project_root) / f"{compact}_P"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _feature_point(feature: dict[str, Any]) -> tuple[float, float]:
    geometry = feature.get("geometry") or {}
    coordinates = geometry.get("coordinates") or [0.0, 0.0]
    if geometry.get("type") == "Point":
        return float(coordinates[0]), float(coordinates[1])
    if geometry.get("type") == "LineString" and coordinates:
        return float(coordinates[0][0]), float(coordinates[0][1])
    if geometry.get("type") == "Polygon" and coordinates and coordinates[0]:
        return float(coordinates[0][0][0]), float(coordinates[0][0][1])
    return float(coordinates[0]), float(coordinates[1])


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _text_value(props: dict[str, Any], index: int) -> tuple[str, bool]:
    for key in ("confirmed_text", "suggested_text", "output_text", "review_text", "ocr_text", "Text", "text"):
        text = _clean_text(props.get(key))
        if text:
            return text, False
    return f"TXT_{index:04d}", True


def _font_mm(category: str, text: str) -> float:
    category = category or "uncertain_text"
    if category == "title_text" or "剖面图" in text or "地质图" in text:
        return 5.2
    if category == "scale_text":
        return 2.2
    if category in {"legend_text", "title_block_text"}:
        return 2.0
    if category == "sample_table_text":
        return 1.6
    if category == "frame_coordinate_text":
        return 1.7
    return 1.8


def _placeholder_feature(
    feature: dict[str, Any],
    *,
    index: int,
    map_id: str,
    target_file: str,
) -> tuple[dict[str, Any], bool]:
    props = dict(feature.get("properties") or {})
    text, is_placeholder = _text_value(props, index)
    category = _clean_text(props.get("category") or props.get("text_role") or "uncertain_text")
    x, y = _feature_point(feature)
    font_mm = _font_mm(category, text)
    candidate_id = _clean_text(props.get("candidate_id") or props.get("feature_id")) or f"{map_id}_TXT_{index:04d}"
    layer = _clean_text(props.get("suggested_layer")) or f"{map_id}_TEXT_PLACEHOLDER"
    output_props = {
        **props,
        "candidate_id": candidate_id,
        "Layer": layer,
        "Target": "WT",
        "TargetFile": target_file,
        "Feature": "text_placeholder",
        "Text": text,
        "Checked": "no",
        "checked": "no",
        "text_placeholder": "yes" if is_placeholder else "no",
        "text_route": TEXT_PLACEHOLDER_ROUTE,
        "native_wt_ready_for_acceptance": "no",
        "Note": "Text placeholder for manual correction; generated through SECTION/W60 DXF route.",
        # This route keeps geometry in PIXEL units (1 px = 1 ground unit), so
        # the label size must be font_mm converted to px (coordinate_scale=1).
        # Passing PIXEL_TO_ORIGINAL_TIF_MM here cancelled the conversion and
        # rendered every annotation ~12x too small.
        "OGR_STYLE": mapgis_dxf_label_style(
            text,
            "SimSun",
            font_mm,
            coordinate_scale=1.0,
        ),
    }
    return (
        {
            "type": "Feature",
            "properties": output_props,
            "geometry": {"type": "Point", "coordinates": [round(x, 6), round(y, 6)]},
        },
        is_placeholder,
    )


def _write_conversion_list(path: Path, *, target_file: str, dxf_path: Path, output_root: Path, count: int) -> None:
    try:
        relative = dxf_path.relative_to(output_root)
    except ValueError:
        relative = dxf_path
    relative_text = str(relative).replace("/", "\\")
    path.write_text(f"{target_file}\tdxf\t{relative_text}\t{count}\n", encoding="utf-8")


def _relative_exchange_text(path: Path, *, output_root: Path) -> str:
    try:
        relative = path.relative_to(output_root)
    except ValueError:
        relative = path
    return str(relative).replace("/", "\\")


def _line_coordinates(feature: dict[str, Any]) -> list[list[float]] | None:
    geometry = feature.get("geometry") or {}
    if geometry.get("type") != "LineString":
        return None
    coordinates = geometry.get("coordinates") or []
    if len(coordinates) < 2:
        return None
    output: list[list[float]] = []
    for point in coordinates:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return None
        output.append([round(float(point[0]), 6), round(float(point[1]), 6)])
    return output


def _line_feature(
    feature: dict[str, Any],
    *,
    index: int,
    map_id: str,
    target_file: str,
) -> dict[str, Any] | None:
    coordinates = _line_coordinates(feature)
    if coordinates is None:
        return None
    props = dict(feature.get("properties") or {})
    candidate_id = _clean_text(props.get("candidate_id") or props.get("feature_id")) or f"{map_id}_LINE_{index:04d}"
    layer = _clean_text(props.get("cad_layer") or props.get("Layer")) or f"{map_id}_AUTO_LINE"
    output_props = {
        **props,
        "candidate_id": candidate_id,
        "Layer": layer,
        "Target": "WL",
        "TargetFile": target_file,
        "Feature": _clean_text(props.get("feature") or props.get("Feature") or "line_candidate"),
        "Checked": "no",
        "checked": "no",
        "line_route": LINE_EXCHANGE_ROUTE,
        "native_wl_ready_for_acceptance": "no",
        "Note": _clean_text(props.get("note") or props.get("Note") or "Line candidate for manual review."),
    }
    return {
        "type": "Feature",
        "properties": output_props,
        "geometry": {"type": "LineString", "coordinates": coordinates},
    }


def _area_feature(
    feature: dict[str, Any],
    *,
    index: int,
    map_id: str,
    target_file: str,
) -> dict[str, Any] | None:
    geometry = feature.get("geometry") or {}
    if geometry.get("type") not in {"Polygon", "MultiPolygon"}:
        return None
    props = dict(feature.get("properties") or {})
    candidate_id = _clean_text(props.get("candidate_id") or props.get("feature_id")) or f"{map_id}_AREA_{index:04d}"
    layer = _clean_text(props.get("cad_layer") or props.get("Layer")) or f"{map_id}_AUTO_AREA"
    output_props = {
        "CID": candidate_id,
        "LAYER": layer,
        "TARGET": "WP",
        "TFILE": target_file,
        "FEATURE": _clean_text(props.get("feature") or props.get("Feature") or "area_candidate"),
        "CHECKED": "no",
        "REVIEW": "pending",
        "CONF": round(float(props.get("confidence") or 0.0), 4),
        "AREA_PX": round(float(props.get("area_px") or 0.0), 2),
        "ROUTE": AREA_EXCHANGE_ROUTE,
        "NOTE": "Optional area candidate; manual MapGIS review required.",
    }
    return {
        "type": "Feature",
        "properties": output_props,
        "geometry": geometry,
    }


def _export_dxf(
    *,
    source_geojson: Path,
    dxf_path: Path,
    ogr2ogr_path: Path,
    gdal_data: Path,
) -> dict[str, Any]:
    if not ogr2ogr_path.exists():
        raise FileNotFoundError(f"Missing ogr2ogr: {ogr2ogr_path}")
    if not (gdal_data / "header.dxf").is_file():
        raise FileNotFoundError(
            "Missing GDAL DXF template header.dxf. "
            f"Expected: {gdal_data / 'header.dxf'}. "
            f"Set {GDAL_DATA_ENV_VAR} to a GDAL data directory, or keep the packaged gdal/gdal-data folder."
        )
    dxf_path.parent.mkdir(parents=True, exist_ok=True)
    if dxf_path.exists():
        dxf_path.unlink()
    env = os.environ.copy()
    effective_gdal_data = ascii_safe_env_dir(gdal_data, purpose="GDAL_DATA")
    env["GDAL_DATA"] = str(effective_gdal_data)
    bundled_proj = ogr2ogr_path.parent / "proj"
    if bundled_proj.is_dir() and not env.get("PROJ_LIB", "").strip():
        try:
            env["PROJ_LIB"] = str(ascii_safe_env_dir(bundled_proj, purpose="PROJ_LIB"))
        except RuntimeError:
            # PROJ is not needed for the pixel-unit DXF route; keep the
            # original dir rather than failing the whole export.
            env["PROJ_LIB"] = str(bundled_proj)
    completed = subprocess.run(
        [str(ogr2ogr_path), "-f", "DXF", str(dxf_path), str(source_geojson), "-nln", "entities", "-overwrite"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "DXF export failed\nSTDOUT:\n"
            + completed.stdout
            + "\nSTDERR:\n"
            + completed.stderr
        )
    make_dxf_mapgis_chinese_compatible(dxf_path)
    return {
        "status": "written",
        "path": str(dxf_path),
        "bytes": dxf_path.stat().st_size,
        "gdal_data_used": str(effective_gdal_data),
    }


def _remove_shapefile_prj(shp_path: Path) -> bool:
    prj_path = shp_path.with_suffix(".prj")
    if prj_path.exists():
        prj_path.unlink()
        return True
    return False


def _export_shapefile(
    *,
    source_geojson: Path,
    shp_path: Path,
    ogr2ogr_path: Path,
    gdal_data: Path,
) -> dict[str, Any]:
    if not ogr2ogr_path.exists():
        raise FileNotFoundError(f"Missing ogr2ogr: {ogr2ogr_path}")
    if shp_path.parent.exists():
        shutil.rmtree(shp_path.parent)
    shp_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    if gdal_data.exists():
        env["GDAL_DATA"] = str(ascii_safe_env_dir(gdal_data, purpose="GDAL_DATA"))
    completed = subprocess.run(
        [
            str(ogr2ogr_path),
            "-f",
            "ESRI Shapefile",
            str(shp_path),
            str(source_geojson),
            "-nln",
            shp_path.stem,
            "-overwrite",
        ],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "Shapefile export failed\nSTDOUT:\n"
            + completed.stdout
            + "\nSTDERR:\n"
            + completed.stderr
        )
    prj_removed = _remove_shapefile_prj(shp_path)
    return {
        "status": "written",
        "path": str(shp_path),
        "bytes": shp_path.stat().st_size,
        "prj_removed": prj_removed,
    }


def _read_dxf_text(path: Path) -> str:
    for encoding in ("utf-8", "gbk", "cp1252", "latin1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _first_dxf_value(entity: list[tuple[str, str]], code: str, default: str = "") -> str:
    for item_code, item_value in entity:
        if item_code == code:
            return item_value
    return default


def _all_dxf_values(entity: list[tuple[str, str]], *codes: str) -> list[str]:
    wanted = set(codes)
    return [item_value for item_code, item_value in entity if item_code in wanted]


def _legacy_text_entity_from_mtext(entity: list[tuple[str, str]]) -> list[tuple[str, str]]:
    handle = _first_dxf_value(entity, "5")
    layer = _first_dxf_value(entity, "8", "TEXT")
    color = _first_dxf_value(entity, "62")
    x = _first_dxf_value(entity, "10", "0")
    y = _first_dxf_value(entity, "20", "0")
    z = _first_dxf_value(entity, "30", "0")
    height = _first_dxf_value(entity, "40", "2")
    rotation = _first_dxf_value(entity, "50", "0")
    text = "".join(_all_dxf_values(entity, "1", "3"))

    output: list[tuple[str, str]] = [("0", "TEXT")]
    if handle:
        output.extend([("5", handle)])
    output.extend(
        [
            ("100", "AcDbEntity"),
            ("8", layer),
        ]
    )
    if color:
        output.append(("62", color))
    output.extend(
        [
            ("100", "AcDbText"),
            ("10", x),
            ("20", y),
            ("30", z),
            ("40", height),
            ("1", text),
            ("50", rotation),
            ("100", "AcDbText"),
        ]
    )
    return output


def rewrite_dxf_mtext_entities_to_text(path: Path) -> dict[str, Any]:
    """Convert GDAL MTEXT labels into legacy TEXT entities for MapGIS/SECTION."""
    path = Path(path)
    raw_lines = _read_dxf_text(path).splitlines()
    pairs: list[tuple[str, str]] = []
    index = 0
    while index < len(raw_lines):
        code = raw_lines[index]
        value = raw_lines[index + 1] if index + 1 < len(raw_lines) else ""
        pairs.append((code, value))
        index += 2

    output: list[tuple[str, str]] = []
    converted = 0
    pair_index = 0
    while pair_index < len(pairs):
        code, value = pairs[pair_index]
        if code == "0" and value == "MTEXT":
            pair_index += 1
            entity: list[tuple[str, str]] = []
            while pair_index < len(pairs) and pairs[pair_index][0] != "0":
                entity.append(pairs[pair_index])
                pair_index += 1
            output.extend(_legacy_text_entity_from_mtext(entity))
            converted += 1
            continue
        output.append((code, value))
        pair_index += 1

    flattened: list[str] = []
    for code, value in output:
        flattened.extend([code, value])
    path.write_text("\n".join(flattened) + "\n", encoding="gbk", errors="replace", newline="")
    return {"status": "rewritten", "path": str(path), "converted_mtext_count": converted}


def _write_handoff(
    path: Path,
    *,
    output_root: Path,
    grouped_exchange_dir: Path,
    section_batch_dir: Path,
    conversion_list: Path,
    ready_dir: Path,
    target_file: str,
) -> None:
    text = f"""# Accuracy Workflow Handoff

Purpose: stable text placeholder route for MapGIS visual QA.

Key rule: text uses DXF -> SECTION/W60 -> WT. Native WT is not accepted here.

Output root:
{output_root}

Text target:
{target_file}

Run these steps after the DXF package is ready:

```powershell
python -m geoscan.mapgis67_bridge prepare_batch --source-dir "{grouped_exchange_dir}" --batch-dir "{section_batch_dir}"
python -m geoscan.mapgis67_bridge section_batch_convert --batch-dir "{section_batch_dir}"
python -m geoscan.mapgis67_bridge verify_batch --batch-dir "{section_batch_dir}"
python -m geoscan.mapgis67_bridge collect_ready --conversion-list "{conversion_list}" --section-batch-dir "{section_batch_dir}" --ready-dir "{ready_dir}"
```

Manual acceptance checklist:

- Load the converted WT together with the WL files.
- Zoom in and out: text must not disappear.
- Export JPG: text must remain visible.
- Text content may be wrong, but positions should match the source raster enough for manual correction.
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_text_placeholder_exchange_package(
    *,
    source_geojson: Path,
    output_root: Path,
    map_id: str,
    target_file: str,
    export_dxf: bool = True,
    ogr2ogr_path: Path | None = None,
    gdal_data: Path | None = None,
) -> dict[str, Any]:
    ogr2ogr_path = resolve_ogr2ogr(ogr2ogr_path)
    gdal_data = resolve_gdal_data(gdal_data)
    output_root = Path(output_root)
    source_geojson = Path(source_geojson)
    text_dir = output_root / "07_TEXT_SECTION_W60"
    grouped_sources = text_dir / "grouped_sources"
    grouped_exchange = text_dir / "grouped_exchange"
    section_batch = text_dir / "section_batch"
    ready_dir = output_root / "MAPGIS_READY"
    target_stem = safe_target_stem(target_file)
    output_source = grouped_sources / f"{target_stem}.geojson"
    output_dxf = grouped_exchange_path(grouped_exchange, target_file)
    conversion_list = text_dir / "CONVERSION_LIST.txt"
    handoff = text_dir / "HANDOFF_NEXT_WINDOW.md"

    payload = _read_json(source_geojson)
    output_features: list[dict[str, Any]] = []
    placeholder_count = 0
    for index, feature in enumerate(payload.get("features", []), start=1):
        output_feature, is_placeholder = _placeholder_feature(
            feature,
            index=index,
            map_id=map_id,
            target_file=target_file,
        )
        output_features.append(output_feature)
        if is_placeholder:
            placeholder_count += 1

    _write_json(output_source, {"type": "FeatureCollection", "name": target_stem, "features": output_features})
    source_manifest = {
        target_file: {
            "kind": "dxf",
            "source": str(output_source),
            "path": str(output_dxf),
            "features": len(output_features),
        }
    }
    _write_json(grouped_sources / "manifest.json", source_manifest)
    _write_json(grouped_exchange / "manifest.json", source_manifest)
    _write_conversion_list(
        conversion_list,
        target_file=target_file,
        dxf_path=output_dxf,
        output_root=text_dir,
        count=len(output_features),
    )
    _write_handoff(
        handoff,
        output_root=output_root,
        grouped_exchange_dir=grouped_exchange,
        section_batch_dir=section_batch,
        conversion_list=conversion_list,
        ready_dir=ready_dir,
        target_file=target_file,
    )

    if export_dxf:
        dxf_report = _export_dxf(
            source_geojson=output_source,
            dxf_path=output_dxf,
            ogr2ogr_path=ogr2ogr_path,
            gdal_data=gdal_data,
        )
        dxf_report["legacy_text_entities"] = rewrite_dxf_mtext_entities_to_text(output_dxf)
    else:
        dxf_report = {"status": "skipped", "path": str(output_dxf), "reason": "export_dxf_false"}

    report = {
        "route": TEXT_PLACEHOLDER_ROUTE,
        "native_wt_ready_for_acceptance": False,
        "source_geojson_input": str(source_geojson),
        "source_geojson": str(output_source),
        "target_file": target_file,
        "output_root": str(output_root),
        "grouped_sources_manifest": str(grouped_sources / "manifest.json"),
        "grouped_exchange_manifest": str(grouped_exchange / "manifest.json"),
        "grouped_exchange_dir": str(grouped_exchange),
        "section_batch_dir": str(section_batch),
        "conversion_list": str(conversion_list),
        "handoff": str(handoff),
        "source_text_count": len(payload.get("features", [])),
        "output_text_count": len(output_features),
        "placeholder_text_count": placeholder_count,
        "dxf_export": dxf_report,
    }
    _write_json(text_dir / "TEXT_PLACEHOLDER_PACKAGE_REPORT.json", report)
    return report


def write_line_exchange_package(
    *,
    source_geojson: Path,
    output_root: Path,
    map_id: str,
    target_file: str,
    export_dxf: bool = True,
    ogr2ogr_path: Path | None = None,
    gdal_data: Path | None = None,
) -> dict[str, Any]:
    ogr2ogr_path = resolve_ogr2ogr(ogr2ogr_path)
    gdal_data = resolve_gdal_data(gdal_data)
    output_root = Path(output_root)
    source_geojson = Path(source_geojson)
    line_dir = output_root / "06_LINE_SECTION_W60"
    grouped_sources = line_dir / "grouped_sources"
    grouped_exchange = line_dir / "grouped_exchange"
    section_batch = line_dir / "section_batch"
    ready_dir = output_root / "MAPGIS_READY"
    target_stem = safe_target_stem(target_file)
    output_source = grouped_sources / f"{target_stem}.geojson"
    output_dxf = grouped_exchange_path(grouped_exchange, target_file)
    conversion_list = line_dir / "CONVERSION_LIST.txt"

    payload = _read_json(source_geojson)
    output_features: list[dict[str, Any]] = []
    for index, feature in enumerate(payload.get("features", []), start=1):
        output_feature = _line_feature(
            feature,
            index=index,
            map_id=map_id,
            target_file=target_file,
        )
        if output_feature is not None:
            output_features.append(output_feature)

    _write_json(output_source, {"type": "FeatureCollection", "name": target_stem, "features": output_features})
    source_manifest = {
        target_file: {
            "kind": "dxf",
            "source": str(output_source),
            "path": str(output_dxf),
            "features": len(output_features),
        }
    }
    _write_json(grouped_sources / "manifest.json", source_manifest)
    _write_json(grouped_exchange / "manifest.json", source_manifest)
    _write_conversion_list(
        conversion_list,
        target_file=target_file,
        dxf_path=output_dxf,
        output_root=line_dir,
        count=len(output_features),
    )

    if export_dxf and output_features:
        dxf_report = _export_dxf(
            source_geojson=output_source,
            dxf_path=output_dxf,
            ogr2ogr_path=ogr2ogr_path,
            gdal_data=gdal_data,
        )
    elif export_dxf:
        dxf_report = {"status": "empty", "path": str(output_dxf), "reason": "no_line_features"}
    else:
        dxf_report = {"status": "skipped", "path": str(output_dxf), "reason": "export_dxf_false"}

    report = {
        "route": LINE_EXCHANGE_ROUTE,
        "native_wl_ready_for_acceptance": False,
        "source_geojson_input": str(source_geojson),
        "source_geojson": str(output_source),
        "target_file": target_file,
        "output_root": str(output_root),
        "grouped_sources_manifest": str(grouped_sources / "manifest.json"),
        "grouped_exchange_manifest": str(grouped_exchange / "manifest.json"),
        "grouped_exchange_dir": str(grouped_exchange),
        "section_batch_dir": str(section_batch),
        "conversion_list": str(conversion_list),
        "ready_dir": str(ready_dir),
        "source_line_count": len(payload.get("features", [])),
        "output_line_count": len(output_features),
        "dxf_export": dxf_report,
    }
    _write_json(line_dir / "LINE_EXCHANGE_PACKAGE_REPORT.json", report)
    return report


def write_area_exchange_package(
    *,
    source_geojson: Path,
    output_root: Path,
    map_id: str,
    target_file: str,
    export_shp: bool = True,
    ogr2ogr_path: Path | None = None,
    gdal_data: Path | None = None,
) -> dict[str, Any]:
    ogr2ogr_path = resolve_ogr2ogr(ogr2ogr_path)
    gdal_data = resolve_gdal_data(gdal_data)
    output_root = Path(output_root)
    source_geojson = Path(source_geojson)
    area_dir = output_root / "07_AREA_SECTION_W60"
    grouped_sources = area_dir / "grouped_sources"
    grouped_exchange = area_dir / "grouped_exchange"
    section_batch = area_dir / "section_batch"
    ready_dir = output_root / "MAPGIS_READY"
    target_stem = safe_target_stem(target_file)
    output_source = grouped_sources / f"{target_stem}.geojson"
    output_shp = grouped_exchange_path(grouped_exchange, target_file)
    conversion_list = area_dir / "CONVERSION_LIST.txt"

    payload = _read_json(source_geojson)
    output_features: list[dict[str, Any]] = []
    for index, feature in enumerate(payload.get("features", []), start=1):
        output_feature = _area_feature(
            feature,
            index=index,
            map_id=map_id,
            target_file=target_file,
        )
        if output_feature is not None:
            output_features.append(output_feature)

    _write_json(output_source, {"type": "FeatureCollection", "name": target_stem, "features": output_features})
    source_manifest = {
        target_file: {
            "kind": "shp",
            "source": str(output_source),
            "path": str(output_shp),
            "features": len(output_features),
            "optional": True,
        }
    }
    _write_json(grouped_sources / "manifest.json", source_manifest)
    _write_json(grouped_exchange / "manifest.json", source_manifest)
    conversion_list.write_text(
        f"{target_file}\tshp\t{_relative_exchange_text(output_shp, output_root=area_dir)}\t{len(output_features)}\n",
        encoding="utf-8",
    )

    if export_shp and output_features:
        shp_report = _export_shapefile(
            source_geojson=output_source,
            shp_path=output_shp,
            ogr2ogr_path=ogr2ogr_path,
            gdal_data=gdal_data,
        )
        shp_report["prj_removed"] = bool(shp_report.get("prj_removed")) or _remove_shapefile_prj(output_shp)
    elif export_shp:
        shp_report = {"status": "empty", "path": str(output_shp), "reason": "no_area_features"}
    else:
        shp_report = {"status": "skipped", "path": str(output_shp), "reason": "export_shp_false"}

    report = {
        "route": AREA_EXCHANGE_ROUTE,
        "native_wp_ready_for_acceptance": False,
        "optional": True,
        "source_geojson_input": str(source_geojson),
        "source_geojson": str(output_source),
        "target_file": target_file,
        "output_root": str(output_root),
        "grouped_sources_manifest": str(grouped_sources / "manifest.json"),
        "grouped_exchange_manifest": str(grouped_exchange / "manifest.json"),
        "grouped_exchange_dir": str(grouped_exchange),
        "section_batch_dir": str(section_batch),
        "conversion_list": str(conversion_list),
        "ready_dir": str(ready_dir),
        "source_area_count": len(payload.get("features", [])),
        "output_area_count": len(output_features),
        "shp_export": shp_report,
        "note": "Optional WP area exchange package; not accepted until MapGIS/W60 visual review passes.",
    }
    _write_json(area_dir / "AREA_EXCHANGE_PACKAGE_REPORT.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a stable MapGIS text placeholder DXF package.")
    parser.add_argument("build_text_package", nargs="?")
    parser.add_argument("--source-geojson", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--map-id", required=True)
    parser.add_argument("--target-file", required=True)
    parser.add_argument("--no-export-dxf", action="store_true")
    parser.add_argument("--ogr2ogr", type=Path, default=None)
    parser.add_argument("--gdal-data", type=Path, default=None)
    args = parser.parse_args()
    report = write_text_placeholder_exchange_package(
        source_geojson=args.source_geojson,
        output_root=args.output_root,
        map_id=args.map_id,
        target_file=args.target_file,
        export_dxf=not args.no_export_dxf,
        ogr2ogr_path=args.ogr2ogr,
        gdal_data=args.gdal_data,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
