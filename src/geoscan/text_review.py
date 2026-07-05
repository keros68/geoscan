from __future__ import annotations


PRIORITY_CATEGORIES = ("title_text", "legend_text", "title_block_text")
CATEGORY_ORDER = {category: index for index, category in enumerate(PRIORITY_CATEGORIES)}


def _confidence(row: dict[str, object]) -> float:
    try:
        return float(row.get("ocr_confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def priority_text_review_rows(
    rows: list[dict[str, object]],
    *,
    categories: tuple[str, ...] = PRIORITY_CATEGORIES,
) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    for row in rows:
        category = str(row.get("category") or "")
        if category not in categories:
            continue
        item = dict(row)
        item["checked"] = "no"
        item["review_status"] = "pending"
        item["review_text"] = ""
        item["review_note"] = ""
        selected.append(item)

    return sorted(
        selected,
        key=lambda row: (
            CATEGORY_ORDER.get(str(row.get("category") or ""), len(CATEGORY_ORDER)),
            -_confidence(row),
            str(row.get("crop_path") or ""),
        ),
    )
