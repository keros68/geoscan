from __future__ import annotations

from typing import Any


CLASSIFICATION_FIELDS = [
    "target_type",
    "target_file",
    "object_class",
    "subclass",
    "ai_suggestion",
    "ai_reason",
    "review_status",
]


def _text(*values: Any) -> str:
    return " ".join(str(value or "") for value in values).lower()


def _has(value: str, *tokens: str) -> bool:
    return any(token.lower() in value for token in tokens)


def classify_object(
    *,
    target: str,
    cad_layer: str = "",
    feature_name: str = "",
    text_value: str = "",
    symbol_name: str = "",
    note: str = "",
) -> dict[str, str]:
    target_type = str(target or "").upper()
    haystack = _text(cad_layer, feature_name, text_value, symbol_name)
    note_text = _text(note)

    if target_type == "WT":
        target_file, object_class, subclass = _classify_wt(
            haystack,
            note_text=note_text,
            text_value=text_value,
            symbol_name=symbol_name,
        )
    elif target_type == "WL":
        target_file, object_class, subclass = _classify_wl(haystack)
    elif target_type == "WP":
        target_file, object_class, subclass = _classify_wp(haystack)
    else:
        target_file, object_class, subclass = "T04_UNKNOWN.OUT", "unknown", ""

    return {
        "target_type": target_type,
        "target_file": target_file,
        "object_class": object_class,
        "subclass": subclass,
        "ai_suggestion": "",
        "ai_reason": "",
        "review_status": "pending",
    }


def _classify_wt(haystack: str, *, note_text: str, text_value: str, symbol_name: str) -> tuple[str, str, str]:
    if symbol_name or _has(haystack, "t04_point_symbol", "point_symbol", "drill_or_sample", "sample_symbol"):
        return "T04_SYMBOL.WT", "point_symbol", str(symbol_name or "")
    if _has(haystack, "titleblock", "责任", "单位", "图号", "审核", "编图", "日期", "mapno", "director"):
        return "T04_TITLE.WT", "title_block_text", ""
    if _has(haystack, "map_title", "矿床", "勘探线剖面图", "比例尺"):
        return "T04_TITLE.WT", "title_text", "scale_text" if "比例尺" in text_value else ""
    if _has(haystack, "legend", "图例", "岩性界线", "钻孔平面位置", "推测岩性界线"):
        return "T04_LEGEND_TXT.WT", "legend_text", ""
    if _has(haystack, "coord", "coordinate", "坐标", "座标", "标高", "海拔"):
        return "T04_COORD_TXT.WT", "coordinate_text", ""
    if _has(haystack, "note", "缺失", "不清") or _has(note_text, "缺失", "不清"):
        return "T04_NOTE.WT", "review_note", ""
    return "T04_MAP_TXT.WT", "map_text", ""


def _classify_wl(haystack: str) -> tuple[str, str, str]:
    if _has(haystack, "frame", "outer_frame", "neatline"):
        return "T04_FRAME.WL", "frame_line", ""
    if _has(haystack, "t04_legend_pattern_line", "legend_refine", "hatch", "pattern", "mudstone", "sandstone"):
        return "T04_PATTERN.WL", "pattern_line", ""
    if _has(haystack, "grid", "坐标网", "座标网", "tick"):
        return "T04_GRID.WL", "grid_line", ""
    if _has(haystack, "table", "titleblock", "sample_table"):
        return "T04_TABLE.WL", "table_line", ""
    if _has(haystack, "legend"):
        return "T04_LEGEND.WL", "legend_line", ""
    if _has(haystack, "fault", "boundary", "geologic", "lithology", "岩性", "界线"):
        return "T04_GEO_LINE.WL", "geologic_line", ""
    if _has(haystack, "section", "plan", "trench", "drill", "sample", "剖面", "探槽", "钻孔"):
        return "T04_ENG_LINE.WL", "engineering_line", ""
    return "T04_ENG_LINE.WL", "engineering_line", "general_line"


def _classify_wp(haystack: str) -> tuple[str, str, str]:
    if _has(haystack, "legend"):
        return "T04_LEGEND_FILL.WP", "legend_fill", ""
    if _has(haystack, "ore", "矿体"):
        return "T04_ORE.WP", "ore_body", ""
    if _has(haystack, "repair", "缺失", "不清"):
        return "T04_REPAIR_MASK.WP", "repair_mask", ""
    return "T04_UNIT.WP", "geologic_unit", ""
