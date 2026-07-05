"""Optional AI enhancement stage: nominated by AI, validated by code.

Authority split ("度" control):

1. The model only NOMINATES operations from a closed vocabulary, referencing
   candidate ids that already exist. It never supplies coordinates.
2. Deterministic validators decide: bridge coordinates are computed by snapping
   to existing endpoints, and every bridge must be supported by dark pixels on
   the frozen raster (the line visibly exists on the map, extraction just broke
   it). Text suggestions must stay within a small edit distance of the OCR text
   or hit the map-furniture dictionary; anything that looks like a geological
   formation code is rejected outright (never invent geological content).
3. Accepted results land in a NEW enhanced layer / sidecar CSV. The raw,
   repaired and text candidate files are byte-asserted unchanged. Everything
   stays ``checked=no``; humans accept in MapGIS.

Tightening or loosening the stage means changing the validator thresholds
(``AiEnhanceThresholds``), never granting the model more authority.
"""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .ai_vision_review import (
    AiVisionConfig,
    UrlopenCallable,
    _chat_completion,
    _extract_json_payload,
    _image_data_url,
    _redact_api_key,
    _safe_request_snapshot,
    _write_json,
    normalize_chat_completions_url,
)

ENHANCE_DIR_NAME = "AI_ENHANCE"
VALID_LINE_OPS = {"bridge_gap"}
ENDPOINTS = {"start", "end"}

# Map-furniture words/patterns that are safe to auto-suggest. Deliberately
# excludes anything geological (formation codes, unit names, boundaries).
TEXT_DICTIONARY_PATTERNS = (
    r"^\d{1,4}:\d{2,7}$",          # scale, e.g. 1:500
    r"^比例尺$",
    r"^[0-9]+(\.[0-9]+)?\s*(m|米|km|公里)?$",  # plain measurements
    r".*剖面图$",
    r".*平面图$",
    r".*柱状图$",
    r".*勘探线.*",
    r"^图例$",
    r"^单位$",
    r"^高程$",
    r"^孔深$",
    r"^钻孔$",
)

# Suggestions that look like geological formation codes are always rejected —
# correcting them is interpretation, not OCR cleanup.
FORMATION_CODE_PATTERN = re.compile(r"^[A-Z][a-z]{0,2}\d{0,2}[a-z]?(\^?[0-9+\-]*)?$")


@dataclass(frozen=True)
class AiEnhanceThresholds:
    """The actual dial for how much the AI stage may change. All deterministic."""

    max_gap_px: float = 60.0
    min_dark_coverage: float = 0.55
    dark_threshold: int = 140
    sample_window_px: int = 2
    max_text_edit_distance: int = 2


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _endpoint(feature: dict[str, Any], which: str) -> tuple[float, float] | None:
    geometry = feature.get("geometry") or {}
    if geometry.get("type") != "LineString":
        return None
    coordinates = geometry.get("coordinates") or []
    if len(coordinates) < 2:
        return None
    point = coordinates[0] if which == "start" else coordinates[-1]
    return float(point[0]), float(point[1])


def _dark_coverage(
    gray: np.ndarray, a: tuple[float, float], b: tuple[float, float], thresholds: AiEnhanceThresholds
) -> float:
    """Fraction of sample points along a->b whose neighborhood has map ink."""
    height, width = gray.shape[:2]
    gap = math.hypot(b[0] - a[0], b[1] - a[1])
    samples = max(int(round(gap)), 8)
    window = thresholds.sample_window_px
    hits = 0
    for index in range(samples + 1):
        t = index / samples
        x = int(round(a[0] + (b[0] - a[0]) * t))
        y = int(round(a[1] + (b[1] - a[1]) * t))
        x0, x1 = max(0, x - window), min(width, x + window + 1)
        y0, y1 = max(0, y - window), min(height, y + window + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        if int(gray[y0:y1, x0:x1].min()) <= thresholds.dark_threshold:
            hits += 1
    return hits / (samples + 1)


def validate_bridge_op(
    op: dict[str, Any],
    *,
    features_by_id: dict[str, dict[str, Any]],
    gray: np.ndarray,
    thresholds: AiEnhanceThresholds,
) -> dict[str, Any]:
    """Deterministic gate for one nominated bridge. Never trusts model geometry."""
    id_a = str(op.get("candidate_a") or "").strip()
    id_b = str(op.get("candidate_b") or "").strip()
    end_a = str(op.get("endpoint_a") or "").strip().lower()
    end_b = str(op.get("endpoint_b") or "").strip().lower()
    base = {
        "op": "bridge_gap",
        "candidate_a": id_a,
        "candidate_b": id_b,
        "endpoint_a": end_a,
        "endpoint_b": end_b,
        "reason": str(op.get("reason") or "").strip()[:300],
    }
    if id_a not in features_by_id or id_b not in features_by_id:
        return {**base, "accepted": False, "rejected_because": "unknown_candidate_id"}
    if id_a == id_b:
        return {**base, "accepted": False, "rejected_because": "same_candidate"}
    if end_a not in ENDPOINTS or end_b not in ENDPOINTS:
        return {**base, "accepted": False, "rejected_because": "invalid_endpoint"}
    point_a = _endpoint(features_by_id[id_a], end_a)
    point_b = _endpoint(features_by_id[id_b], end_b)
    if point_a is None or point_b is None:
        return {**base, "accepted": False, "rejected_because": "not_a_linestring"}
    gap = math.hypot(point_b[0] - point_a[0], point_b[1] - point_a[1])
    if gap <= 0.0:
        return {**base, "accepted": False, "rejected_because": "zero_gap"}
    if gap > thresholds.max_gap_px:
        return {**base, "accepted": False, "rejected_because": "gap_too_large", "gap_px": round(gap, 2)}
    coverage = _dark_coverage(gray, point_a, point_b, thresholds)
    if coverage < thresholds.min_dark_coverage:
        return {
            **base,
            "accepted": False,
            "rejected_because": "no_raster_evidence",
            "gap_px": round(gap, 2),
            "dark_coverage": round(coverage, 3),
        }
    return {
        **base,
        "accepted": True,
        "gap_px": round(gap, 2),
        "dark_coverage": round(coverage, 3),
        "point_a": [round(point_a[0], 3), round(point_a[1], 3)],
        "point_b": [round(point_b[0], 3), round(point_b[1], 3)],
    }


def _edit_distance(a: str, b: str) -> int:
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,
                    current[j - 1] + 1,
                    previous[j - 1] + (char_a != char_b),
                )
            )
        previous = current
    return previous[-1]


def _matches_dictionary(text: str) -> bool:
    return any(re.match(pattern, text) for pattern in TEXT_DICTIONARY_PATTERNS)


def validate_text_op(
    op: dict[str, Any],
    *,
    texts_by_id: dict[str, str],
    thresholds: AiEnhanceThresholds,
) -> dict[str, Any]:
    candidate_id = str(op.get("candidate_id") or "").strip()
    suggested = str(op.get("suggested_text") or "").strip()
    base = {
        "candidate_id": candidate_id,
        "suggested_text": suggested,
        "reason": str(op.get("reason") or "").strip()[:300],
    }
    if candidate_id not in texts_by_id:
        return {**base, "accepted": False, "rejected_because": "unknown_candidate_id"}
    original = texts_by_id[candidate_id]
    base["original_text"] = original
    if not suggested or suggested == original:
        return {**base, "accepted": False, "rejected_because": "empty_or_unchanged"}
    if FORMATION_CODE_PATTERN.match(suggested) and not _matches_dictionary(suggested):
        return {**base, "accepted": False, "rejected_because": "geological_code_risk"}
    if _matches_dictionary(suggested):
        return {**base, "accepted": True, "accepted_via": "dictionary"}
    distance = _edit_distance(original, suggested)
    if distance <= thresholds.max_text_edit_distance:
        return {**base, "accepted": True, "accepted_via": "edit_distance", "edit_distance": distance}
    return {
        **base,
        "accepted": False,
        "rejected_because": "edit_distance_too_large",
        "edit_distance": distance,
    }


def build_enhance_prompt(
    map_id: str,
    *,
    line_rows: list[str],
    omitted_lines: int,
    text_rows: list[str],
    omitted_texts: int,
    thresholds: AiEnhanceThresholds,
) -> str:
    lines = [
        f"你是 GeoScan 的增强提名员。当前图幅: {map_id}。",
        "附图是修复后线候选的 QA 叠加图。你只提名操作，坐标由程序计算并用栅格证据验证。",
        "可用操作（严格限定，其他一律无效）：",
        f"1. bridge_gap：图上明显是同一条线但候选断开（缺口不超过 {thresholds.max_gap_px:.0f} 像素）时，"
        "提名把候选 A 的某端点与候选 B 的某端点桥接。",
        "2. 文字纠错：OCR 明显识别错的图面常用词（剖面图/勘探线/比例尺/数字等），给出更正建议。",
        "硬边界：",
        "- 只能引用下面列表中已存在的 id，禁止编造；",
        "- 禁止输出任何坐标数值；禁止提名删除；",
        "- 禁止更正或发明地层代号、地质名称等地质解释内容；",
        "- 禁止 checked=yes。",
        "",
        f"线候选清单（id|端点start x,y|端点end x,y|length_px），共 {len(line_rows)} 条"
        + (f"，另有 {omitted_lines} 条省略" if omitted_lines else "") + "：",
        *line_rows,
        "",
        f"文字候选清单（id|ocr_text），共 {len(text_rows)} 条"
        + (f"，另有 {omitted_texts} 条省略" if omitted_texts else "") + "：",
        *text_rows,
        "",
        "只返回一个 JSON 对象，不要 Markdown：",
        "{",
        '  "line_ops": [{"op": "bridge_gap", "candidate_a": "RL_0001", "endpoint_a": "end",'
        ' "candidate_b": "RL_0002", "endpoint_b": "start", "reason": "..."}],',
        '  "text_ops": [{"candidate_id": "TXT_0001", "suggested_text": "剖面图", "reason": "..."}]',
        "}",
    ]
    return "\n".join(lines)


def _load_features(path: Path) -> tuple[bytes, list[dict[str, Any]]]:
    raw = path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    return raw, list(payload.get("features") or [])


def _candidate_id(feature: dict[str, Any]) -> str:
    return str((feature.get("properties") or {}).get("candidate_id") or "").strip()


def run_ai_enhance_stage(
    config: AiVisionConfig,
    *,
    output_root: Path,
    map_id: str,
    frozen_raster: Path,
    urlopen: UrlopenCallable | None = None,
    thresholds: AiEnhanceThresholds | None = None,
    max_lines_in_prompt: int = 300,
    max_texts_in_prompt: int = 200,
) -> dict[str, Any]:
    """Nominate -> validate -> write an ADDITIVE enhanced layer + sidecars."""
    thresholds = thresholds or AiEnhanceThresholds()
    output_root = Path(output_root)
    map_key = map_id.lower()
    line_dir = output_root / "04_LINE_WORKFLOW"
    repaired_path = line_dir / f"{map_key}_repaired_line_candidates.geojson"
    raw_path = line_dir / f"{map_key}_review_line_candidates.geojson"
    text_path = (
        output_root / "05_TEXT_WORKFLOW" / f"{map_key}_review_text_candidates.geojson"
    )
    if not repaired_path.is_file():
        raise FileNotFoundError(
            f"AI enhance runs on the repaired layer; missing {repaired_path}"
        )

    repaired_bytes, repaired_features = _load_features(repaired_path)
    raw_bytes = raw_path.read_bytes() if raw_path.is_file() else None
    features_by_id = {
        _candidate_id(item): item for item in repaired_features if _candidate_id(item)
    }
    if not features_by_id:
        raise ValueError("repaired layer has no candidate_id fields")

    text_bytes: bytes | None = None
    texts_by_id: dict[str, str] = {}
    if text_path.is_file():
        text_bytes, text_features = _load_features(text_path)
        for item in text_features:
            props = item.get("properties") or {}
            candidate_id = str(props.get("candidate_id") or "").strip()
            if candidate_id:
                texts_by_id[candidate_id] = str(props.get("ocr_text") or "").strip()

    gray = np.asarray(Image.open(frozen_raster).convert("L"))

    line_rows: list[str] = []
    for item in list(features_by_id.values())[:max_lines_in_prompt]:
        start = _endpoint(item, "start")
        end = _endpoint(item, "end")
        if start is None or end is None:
            continue
        props = item.get("properties") or {}
        line_rows.append(
            f"{_candidate_id(item)}|{start[0]:.0f},{start[1]:.0f}|{end[0]:.0f},{end[1]:.0f}|{props.get('length_px', '')}"
        )
    omitted_lines = max(0, len(features_by_id) - max_lines_in_prompt)
    text_rows = [
        f"{candidate_id}|{text}"
        for candidate_id, text in list(texts_by_id.items())[:max_texts_in_prompt]
    ]
    omitted_texts = max(0, len(texts_by_id) - max_texts_in_prompt)

    prompt = build_enhance_prompt(
        map_id,
        line_rows=line_rows,
        omitted_lines=omitted_lines,
        text_rows=text_rows,
        omitted_texts=omitted_texts,
        thresholds=thresholds,
    )
    content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    overlay_path = line_dir / "LINE_REPAIR_OVERLAY.png"
    if overlay_path.is_file():
        content_parts.append(
            {"type": "image_url", "image_url": {"url": _image_data_url(overlay_path)}}
        )
    messages = [{"role": "user", "content": content_parts}]

    enhance_dir = output_root / ENHANCE_DIR_NAME
    api_url = normalize_chat_completions_url(config.base_url)
    _write_json(
        enhance_dir / "ai_enhance_request.json",
        _safe_request_snapshot(config, api_url=api_url, messages=messages),
    )
    response_payload, content, api_url = _chat_completion(
        config, messages=messages, urlopen=urlopen
    )
    _write_json(enhance_dir / "ai_enhance_raw_response.json", response_payload)

    proposal = _extract_json_payload(content)
    if not isinstance(proposal, dict):
        proposal = {}

    line_results: list[dict[str, Any]] = []
    for op in proposal.get("line_ops") or []:
        if not isinstance(op, dict):
            continue
        op_name = str(op.get("op") or "").strip()
        if op_name not in VALID_LINE_OPS:
            line_results.append(
                {"op": op_name, "accepted": False, "rejected_because": "unknown_op"}
            )
            continue
        line_results.append(
            validate_bridge_op(
                op, features_by_id=features_by_id, gray=gray, thresholds=thresholds
            )
        )
    accepted_bridges = [row for row in line_results if row.get("accepted")]

    text_results: list[dict[str, Any]] = []
    for op in proposal.get("text_ops") or []:
        if not isinstance(op, dict):
            continue
        text_results.append(
            validate_text_op(op, texts_by_id=texts_by_id, thresholds=thresholds)
        )
    accepted_texts = [row for row in text_results if row.get("accepted")]

    # Enhanced line layer = verbatim repaired features + validated bridges.
    enhanced_features = [json.loads(json.dumps(item)) for item in repaired_features]
    for index, bridge in enumerate(accepted_bridges, start=1):
        enhanced_features.append(
            {
                "type": "Feature",
                "properties": {
                    "candidate_id": f"AIB_{index:04d}",
                    "target": "WL",
                    "source": "ai_enhance",
                    "repair_method": "ai_bridge_gap",
                    "bridges": [bridge["candidate_a"], bridge["candidate_b"]],
                    "gap_px": bridge["gap_px"],
                    "dark_coverage": bridge["dark_coverage"],
                    "confidence": bridge["dark_coverage"],
                    "needs_review": "yes",
                    "checked": "no",
                    "note": "AI 提名 + 栅格证据验证的桥接段；需人工复核。",
                },
                "geometry": {
                    "type": "LineString",
                    "coordinates": [bridge["point_a"], bridge["point_b"]],
                },
            }
        )
    enhanced_path = line_dir / f"{map_key}_ai_enhanced_line_candidates.geojson"
    enhanced_path.write_text(
        json.dumps(
            {"type": "FeatureCollection", "features": enhanced_features},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    suggestions_csv = enhance_dir / "ai_text_suggestions.csv"
    suggestions_csv.parent.mkdir(parents=True, exist_ok=True)
    with suggestions_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["candidate_id", "original_text", "suggested_text", "accepted", "via_or_reason", "checked"]
        )
        for row in text_results:
            writer.writerow(
                [
                    row.get("candidate_id", ""),
                    row.get("original_text", ""),
                    row.get("suggested_text", ""),
                    "yes" if row.get("accepted") else "no",
                    row.get("accepted_via") or row.get("rejected_because") or "",
                    "no",
                ]
            )

    # The enhancement is additive only: inputs must be byte-identical.
    assert repaired_path.read_bytes() == repaired_bytes, (
        "AI enhance must not modify the repaired candidate layer"
    )
    if raw_bytes is not None:
        assert raw_path.read_bytes() == raw_bytes, (
            "AI enhance must not modify the raw candidate layer"
        )
    if text_bytes is not None:
        assert text_path.read_bytes() == text_bytes, (
            "AI enhance must not modify the text candidate layer"
        )

    report = {
        "ok": True,
        "stage": "ai_enhance_after_repair_before_export",
        "created_at_utc": _utc_now(),
        "provider": config.provider,
        "api_url": api_url,
        "model": config.model,
        "map_id": map_id,
        "api_key_configured": bool(config.api_key.strip()),
        "api_key_redacted": _redact_api_key(config.api_key),
        "nomination_only": True,
        "ai_wrote_coordinates": False,
        "checked_yes_written": False,
        "inputs_unmodified": True,
        "thresholds": {
            "max_gap_px": thresholds.max_gap_px,
            "min_dark_coverage": thresholds.min_dark_coverage,
            "dark_threshold": thresholds.dark_threshold,
            "max_text_edit_distance": thresholds.max_text_edit_distance,
        },
        "line_ops_proposed": len(line_results),
        "line_ops_accepted": len(accepted_bridges),
        "line_ops_rejected": len(line_results) - len(accepted_bridges),
        "line_op_results": line_results,
        "text_ops_proposed": len(text_results),
        "text_ops_accepted": len(accepted_texts),
        "text_ops_rejected": len(text_results) - len(accepted_texts),
        "enhanced_geojson": str(enhanced_path),
        "enhanced_feature_count": len(enhanced_features),
        "text_suggestions_csv": str(suggestions_csv),
    }
    _write_json(enhance_dir / "AI_ENHANCE_REPORT.json", report)
    return report
