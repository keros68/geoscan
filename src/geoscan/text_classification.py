from __future__ import annotations


def classify_ocr_text_region(
    x: float,
    y: float,
    text: str,
    confidence: float,
) -> dict[str, object]:
    x = float(x)
    y = float(y)
    confidence = float(confidence)
    text = str(text or "")

    if 3900.0 <= y <= 4500.0 and 4500.0 <= x <= 7800.0:
        category = "title_text"
        target_file = "T04_TITLE.WT"
        layer = "T04_TITLE_TEXT_REVIEW"
    elif 1600.0 <= x <= 7900.0 and 450.0 <= y <= 1750.0:
        category = "sample_table_text"
        target_file = "T04_SAMPLE_TABLE_TEXT.WT"
        layer = "T04_SAMPLE_TABLE_TEXT_REVIEW"
    elif 7900.0 <= x <= 10300.0 and 450.0 <= y <= 1700.0:
        category = "legend_text"
        target_file = "T04_LEGEND_TXT.WT"
        layer = "T04_LEGEND_TEXT_REVIEW"
    elif 10300.0 <= x <= 12066.0 and 200.0 <= y <= 1400.0:
        category = "title_block_text"
        target_file = "T04_TITLE_BLOCK_TEXT.WT"
        layer = "T04_TITLE_BLOCK_TEXT_REVIEW"
    elif y < 450.0 or y > 3700.0 or x < 900.0:
        category = "frame_coordinate_text"
        target_file = "T04_FRAME_TEXT.WT"
        layer = "T04_FRAME_TEXT_REVIEW"
    elif 1750.0 < y <= 3900.0:
        category = "map_body_annotation"
        target_file = "T04_MAP_TXT.WT"
        layer = "T04_MAP_BODY_TEXT_REVIEW"
    else:
        category = "uncertain_text"
        target_file = "T04_TEXT_REVIEW.WT"
        layer = "T04_TEXT_REVIEW"

    if confidence >= 0.95:
        priority = "high"
    elif confidence >= 0.80:
        priority = "medium"
    else:
        priority = "low"

    return {
        "category": category,
        "suggested_target_file": target_file,
        "suggested_layer": layer,
        "review_priority": priority,
        "import_status": "review_required",
        "ocr_text": text,
        "ocr_confidence": confidence,
    }
