"""把落在文字候选框内的线候选从主导出中拆分出去。

Large map-furniture text (titles, legend labels) gets vectorized by the line
engines as glyph outlines — the same content the OCR/text workflow already
captures as a WT candidate, so the line copies are pure interference. This
stage SPLITS the export: line candidates whose vertices sit (almost) entirely
inside a text candidate's bbox are excluded from the main export file (the
converted .WL then comes out clean — SECTION/W60 flattens DXF layers, so a
same-file layer split would still show up in MapGIS) and written to a sidecar
GeoJSON for review instead.

Nothing is lost and no geometry changes: the removed strokes live on in the
sidecar (tagged ``T04_TEXT_INTERFERENCE``), the source layers stay
byte-identical, and a real map line passing THROUGH a text box keeps vertices
outside the box, so it never crosses the ``min_inside_fraction`` gate.
Everything stays ``checked=no``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TEXT_INTERFERENCE_LAYER = "T04_TEXT_INTERFERENCE"

Box = tuple[float, float, float, float]  # min_x, min_y, max_x, max_y (map coords)


def _text_boxes(
    text_features: list[dict[str, Any]], *, image_height: float, margin_px: float
) -> list[Box]:
    """Map-coordinate bboxes from text candidates' pixel bbox properties."""
    boxes: list[Box] = []
    for item in text_features:
        props = item.get("properties") or {}
        try:
            left = float(props["bbox_left_px"])
            top = float(props["bbox_top_px"])
            right = float(props["bbox_right_px"])
            bottom = float(props["bbox_bottom_px"])
        except (KeyError, TypeError, ValueError):
            continue
        if right <= left or bottom <= top:
            continue
        boxes.append(
            (
                left - margin_px,
                image_height - bottom - margin_px,
                right + margin_px,
                image_height - top + margin_px,
            )
        )
    return boxes


def _inside_fraction(coordinates: list[Any], boxes: list[Box]) -> float:
    if not coordinates or not boxes:
        return 0.0
    inside = 0
    for point in coordinates:
        x, y = float(point[0]), float(point[1])
        if any(bx1 <= x <= bx2 and by1 <= y <= by2 for bx1, by1, bx2, by2 in boxes):
            inside += 1
    return inside / len(coordinates)


def split_text_interference_lines(
    line_features: list[dict[str, Any]],
    text_features: list[dict[str, Any]],
    *,
    image_height: float,
    margin_px: float = 3.0,
    min_inside_fraction: float = 0.9,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Split (kept, removed) line features; geometry untouched, nothing lost."""
    boxes = _text_boxes(text_features, image_height=image_height, margin_px=margin_px)
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for item in line_features:
        geometry = item.get("geometry") or {}
        coordinates = geometry.get("coordinates") or []
        if (
            geometry.get("type") == "LineString"
            and _inside_fraction(coordinates, boxes) >= min_inside_fraction
        ):
            # Only flagged features get rewritten properties, so only they
            # need a deep clone; kept features pass through by reference.
            clone = json.loads(json.dumps(item, ensure_ascii=False))
            props = clone.setdefault("properties", {})
            props["original_cad_layer"] = props.get("cad_layer", "")
            props["cad_layer"] = TEXT_INTERFERENCE_LAYER
            props["object_class"] = "text_interference"
            props["note"] = (
                "疑似文字笔画干扰线（整体落在文字候选框内，文字层已有同内容注记）；"
                "已从主导出移除，仅供复核。"
            )
            removed.append(clone)
        else:
            kept.append(item)
    report = {
        "text_boxes": len(boxes),
        "line_features": len(line_features),
        "flagged_count": len(removed),
        "kept_count": len(kept),
        "interference_layer": TEXT_INTERFERENCE_LAYER,
        "min_inside_fraction": min_inside_fraction,
        "margin_px": margin_px,
    }
    return kept, removed, report


def write_text_flagged_line_export(
    line_geojson: Path,
    text_geojson: Path,
    output_path: Path,
    *,
    image_height: float,
    sidecar_path: Path | None = None,
) -> dict[str, Any]:
    """Write a clean main-export copy + a sidecar of removed interference lines.

    The source line layer is never modified; callers switch the DXF export to
    ``output_path`` only when at least one feature was removed.
    """
    line_payload = json.loads(Path(line_geojson).read_text(encoding="utf-8"))
    text_payload = json.loads(Path(text_geojson).read_text(encoding="utf-8"))
    kept, removed, report = split_text_interference_lines(
        list(line_payload.get("features") or []),
        list(text_payload.get("features") or []),
        image_height=image_height,
    )
    report["source_geojson"] = str(line_geojson)
    report["output_geojson"] = str(output_path)
    if removed:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {"type": "FeatureCollection", "features": kept},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if sidecar_path is not None:
            sidecar_path.write_text(
                json.dumps(
                    {"type": "FeatureCollection", "features": removed},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            report["sidecar_geojson"] = str(sidecar_path)
        report["written"] = True
    else:
        report["written"] = False
    return report
