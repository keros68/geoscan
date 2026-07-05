from __future__ import annotations

from pathlib import Path

from geoscan.dxf_style import (
    PIXEL_TO_ORIGINAL_TIF_MM,
    mapgis_dxf_label_style,
)


def _cell(row: dict[str, object], name: str) -> str:
    value = row.get(name)
    if value is None:
        return ""
    return str(value)


def _clean_cell(row: dict[str, object], name: str) -> str:
    return _cell(row, name).strip()


def _bool_cell(row: dict[str, object], name: str) -> bool | None:
    text = _clean_cell(row, name).lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def _target_file_for_category(category: str) -> str:
    if category == "title_text":
        return "T04_TITLE.WT"
    if category == "legend_text":
        return "T04_LEGEND_TXT.WT"
    if category == "title_block_text":
        return "T04_TITLE_BLOCK_TEXT.WT"
    return "T04_TEXT_REVIEW.WT"


def _layer_for_category(category: str) -> str:
    if category == "title_text":
        return "T04_TITLE_TEXT_REVIEW"
    if category == "legend_text":
        return "T04_LEGEND_TEXT_REVIEW"
    if category == "title_block_text":
        return "T04_TITLE_BLOCK_TEXT_REVIEW"
    return "T04_TEXT_REVIEW"


def _font_for_row(category: str, text: str) -> tuple[str, float]:
    if category == "title_text" and "比例尺" not in text:
        return "SimHei", 14.2
    if category == "title_text":
        return "SimSun", 3.5
    if category == "legend_text":
        return "SimSun", 5.5 if len(text) <= 2 else 3.8
    if category == "title_block_text":
        return "SimSun", 2.4 if len(text) >= 12 else 2.7
    return "SimSun", 3.0


def _feature_name(row: dict[str, str], index: int) -> str:
    crop_stem = Path(row.get("crop_path", "")).stem
    if crop_stem:
        return f"confirmed_text_{crop_stem}"
    return f"confirmed_text_{index:04d}"


def reviewed_text_candidate_rows(
    rows: list[dict[str, object]],
    *,
    target_file_override: str | None = None,
    layer_override: str | None = None,
) -> list[dict[str, str]]:
    reviewed: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        output_text = _clean_cell(row, "output_text")
        suggested_text = _clean_cell(row, "ai_text_suggestion")
        import_status = _clean_cell(row, "import_status")
        reject_as_noise = _bool_cell(row, "ai_reject_as_noise")

        if output_text:
            text = output_text
            candidate_source = "output_text"
            review_status = _clean_cell(row, "review_status") or "manual_confirmed"
        elif suggested_text and import_status == "ai_suggested_review_required" and reject_as_noise is not True:
            text = suggested_text
            candidate_source = "ai_text_suggestion"
            review_status = "manual_template_suggested_review_required"
        else:
            continue

        category = _clean_cell(row, "category") or _clean_cell(row, "ai_text_role") or "unknown"
        intended_target_file = _clean_cell(row, "suggested_target_file") or _target_file_for_category(category)
        intended_layer = _clean_cell(row, "suggested_layer") or _layer_for_category(category)
        target_file = target_file_override or intended_target_file
        layer = layer_override or intended_layer
        font_name, font_mm = _font_for_row(category, text)
        x_px = float(_clean_cell(row, "x_px"))
        y_px = float(_clean_cell(row, "y_px"))
        crop_path = _clean_cell(row, "crop_path")

        reviewed.append(
            {
                "source_index": str(index),
                "category": category,
                "crop_path": crop_path,
                "ocr_text": _clean_cell(row, "ocr_text"),
                "suggested_text": suggested_text or _clean_cell(row, "suggested_text"),
                "confirmed_text": text,
                "excel_text": text,
                "review_note": _clean_cell(row, "ai_review_note") or _clean_cell(row, "review_note"),
                "checked": "no",
                "review_status": review_status,
                "candidate_source": candidate_source,
                "source_import_status": import_status,
                "intended_target_file": intended_target_file,
                "intended_layer": intended_layer,
                "x_px": f"{x_px:g}",
                "y_px": f"{y_px:g}",
                "x_mm": f"{x_px * PIXEL_TO_ORIGINAL_TIF_MM:.6f}",
                "y_mm": f"{y_px * PIXEL_TO_ORIGINAL_TIF_MM:.6f}",
                "target_file": target_file,
                "layer": layer,
                "feature": f"reviewed_text_{Path(crop_path).stem or index}",
                "font_name": font_name,
                "font_mm": f"{font_mm:g}",
            }
        )
    return reviewed


def confirmed_text_review_rows(rows: list[dict[str, object]]) -> list[dict[str, str]]:
    confirmed: list[dict[str, str]] = []
    for index, row in enumerate(rows, start=1):
        text = _clean_cell(row, "review_text")
        if not text:
            continue

        category = _clean_cell(row, "category") or "unknown"
        target_file = _clean_cell(row, "suggested_target_file") or _target_file_for_category(category)
        layer = _clean_cell(row, "suggested_layer") or _layer_for_category(category)
        font_name, font_mm = _font_for_row(category, text)
        x_px = float(_clean_cell(row, "x_px"))
        y_px = float(_clean_cell(row, "y_px"))

        confirmed.append(
            {
                "source_index": str(index),
                "category": category,
                "crop_path": _clean_cell(row, "crop_path"),
                "ocr_text": _clean_cell(row, "ocr_text"),
                "suggested_text": _clean_cell(row, "suggested_text"),
                "confirmed_text": text,
                "excel_text": text,
                "review_note": _clean_cell(row, "review_note"),
                "checked": "no",
                "review_status": "manual_confirmed",
                "x_px": f"{x_px:g}",
                "y_px": f"{y_px:g}",
                "x_mm": f"{x_px * PIXEL_TO_ORIGINAL_TIF_MM:.6f}",
                "y_mm": f"{y_px * PIXEL_TO_ORIGINAL_TIF_MM:.6f}",
                "target_file": target_file,
                "layer": layer,
                "feature": _feature_name({"crop_path": _clean_cell(row, "crop_path")}, index),
                "font_name": font_name,
                "font_mm": f"{font_mm:g}",
            }
        )
    return confirmed


def confirmed_text_features(
    rows: list[dict[str, str]],
    *,
    coordinate_scale: float = PIXEL_TO_ORIGINAL_TIF_MM,
) -> list[dict[str, object]]:
    features: list[dict[str, object]] = []
    for row in rows:
        text = row["confirmed_text"]
        font_name = row.get("font_name") or "SimSun"
        font_mm = float(row.get("font_mm") or 3.0)
        x_px = float(row["x_px"])
        y_px = float(row["y_px"])
        x = round(x_px * coordinate_scale, 6)
        y = round(y_px * coordinate_scale, 6)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "Layer": row["layer"],
                    "OGR_STYLE": mapgis_dxf_label_style(
                        text,
                        font_name,
                        font_mm,
                        coordinate_scale=coordinate_scale,
                    ),
                    "Target": "WT",
                    "Feature": row["feature"],
                    "Text": text,
                    "RawOCR": row.get("ocr_text", ""),
                    "SuggestedText": row.get("suggested_text", ""),
                    "Category": row.get("category", ""),
                    "TargetFile": row["target_file"],
                    "ObjectClass": row.get("category", ""),
                    "Checked": row.get("checked", "no"),
                    "ReviewStatus": row.get("review_status", "manual_confirmed"),
                    "ReviewNote": row.get("review_note", ""),
                    "FontName": font_name,
                    "FontMM": font_mm,
                    "SourceXPx": x_px,
                    "SourceYPx": y_px,
                },
                "geometry": {"type": "Point", "coordinates": [x, y]},
            }
        )
    return features
