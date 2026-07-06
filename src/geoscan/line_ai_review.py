"""Review-only Qwen/AI pass over the repaired line candidate layer.

Runs AFTER the repaired stage and BEFORE any DXF/native WL export. The model
may only rank/classify/suggest against candidate ids that already exist in the
repaired layer:

- it never writes, moves, or invents coordinates;
- it never writes ``checked=yes``;
- its output is a sidecar JSON (``AI_LINE_REVIEW``); the repaired GeoJSON is
  byte-identical before and after this stage (asserted);
- any candidate id in the response that does not exist is dropped and counted;
- the API key comes from the per-session GUI/CLI value and is only ever
  written redacted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .candidates import utc_now as _utc_now
from .ai_vision_review import (
    AiVisionConfig,
    UrlopenCallable,
    _chat_completion,
    _coerce_confidence,
    _extract_json_payload,
    _image_data_url,
    _normalize_regions,
    _redact_api_key,
    _safe_request_snapshot,
    _write_json,
    normalize_chat_completions_url,
)

REVIEW_DIR_NAME = "AI_LINE_REVIEW"
VALID_SUGGESTIONS = {"accept", "reject", "unsure"}


def build_repaired_line_review_prompt(
    map_id: str, candidate_lines: list[str], *, omitted_count: int
) -> str:
    lines = [
        f"你是 GeoScan 的修复线候选复核员。当前图幅: {map_id}。",
        "附图是 QA 叠加图：红色为原始 Hough 候选，绿色为保守修复后的候选（共线合并、四边证据矩形规整）。",
        "任务：只做审查建议。对下面列出的修复产生的候选（按 candidate_id）给出 accept/reject/unsure，",
        "标出疑似文字笔画伪线的 candidate_id，并给出区域分类建议（表格/图框/文字密集区等）。",
        "硬边界：",
        "- 只能引用下面列表中已存在的 candidate_id，禁止编造 id；",
        "- 禁止输出任何最终矢量坐标或可直接写入 WL/WT/WP 的几何；",
        "- bbox_hint 只能是 0..1 的图像比例提示，仅供人工裁图复核；",
        "- 禁止 checked=yes；禁止发明地质含义。",
        "",
        f"修复候选清单（格式 id|method|length_px），共 {len(candidate_lines)} 条"
        + (f"，另有 {omitted_count} 条因篇幅省略" if omitted_count else "") + "：",
        *candidate_lines,
        "",
        "只返回一个 JSON 对象，不要 Markdown：",
        "{",
        '  "review_only": true,',
        '  "closure_suggestions": [{"candidate_id": "RL_0001", "suggestion": "accept|reject|unsure", "reason": "..."}],',
        '  "suspect_line_ids": ["RL_0002"],',
        '  "regions": [{"role": "table|frame|text_dense|map_body|uncertain", "bbox_hint": [0,0,1,1], "confidence": 0.0, "review_note": "..."}]',
        "}",
    ]
    return "\n".join(lines)


def sanitize_review_payload(
    payload: Any, *, known_ids: set[str]
) -> dict[str, Any]:
    """Keep only suggestions that reference existing candidate ids; strip geometry."""
    if not isinstance(payload, dict):
        payload = {}
    dropped_unknown_ids: list[str] = []
    suggestions: list[dict[str, Any]] = []
    for row in payload.get("closure_suggestions") or []:
        if not isinstance(row, dict):
            continue
        candidate_id = str(row.get("candidate_id") or "").strip()
        if candidate_id not in known_ids:
            if candidate_id:
                dropped_unknown_ids.append(candidate_id)
            continue
        suggestion = str(row.get("suggestion") or "").strip().lower()
        if suggestion not in VALID_SUGGESTIONS:
            suggestion = "unsure"
        entry: dict[str, Any] = {
            "candidate_id": candidate_id,
            "suggestion": suggestion,
            "reason": str(row.get("reason") or "").strip()[:500],
        }
        confidence = _coerce_confidence(row.get("confidence"))
        if confidence is not None:
            entry["confidence"] = confidence
        suggestions.append(entry)

    suspect_ids: list[str] = []
    for value in payload.get("suspect_line_ids") or []:
        candidate_id = str(value or "").strip()
        if candidate_id in known_ids:
            suspect_ids.append(candidate_id)
        elif candidate_id:
            dropped_unknown_ids.append(candidate_id)

    return {
        "review_only": True,
        "ai_wrote_coordinates": False,
        "checked_yes_written": False,
        "closure_suggestions": suggestions,
        "suspect_line_ids": sorted(set(suspect_ids)),
        "regions": _normalize_regions(payload.get("regions")),
        "dropped_unknown_ids": sorted(set(dropped_unknown_ids)),
    }


def run_repaired_line_ai_review(
    config: AiVisionConfig,
    *,
    output_root: Path,
    map_id: str,
    urlopen: UrlopenCallable | None = None,
    max_candidates_in_prompt: int = 400,
) -> dict[str, Any]:
    output_root = Path(output_root)
    map_key = map_id.lower()
    repaired_path = (
        output_root / "04_LINE_WORKFLOW" / f"{map_key}_repaired_line_candidates.geojson"
    )
    if not repaired_path.is_file():
        raise FileNotFoundError(
            f"AI line review runs on the repaired layer; missing {repaired_path}"
        )
    overlay_path = output_root / "04_LINE_WORKFLOW" / "LINE_REPAIR_OVERLAY.png"

    repaired_bytes = repaired_path.read_bytes()
    payload = json.loads(repaired_bytes.decode("utf-8"))
    features = list(payload.get("features") or [])
    known_ids = {
        str(item.get("properties", {}).get("candidate_id") or "")
        for item in features
    }
    known_ids.discard("")
    if not known_ids:
        raise ValueError(
            "repaired layer has no candidate_id fields; regenerate it with the current "
            "line_repair_stage before running AI review"
        )

    repaired_new = [
        item
        for item in features
        if item.get("properties", {}).get("repair_method")
        not in {"passthrough", "passthrough_untouched"}
    ]
    listed = repaired_new[:max_candidates_in_prompt]
    omitted_count = len(repaired_new) - len(listed)
    candidate_lines = [
        "{}|{}|{}".format(
            item["properties"].get("candidate_id"),
            item["properties"].get("repair_method"),
            item["properties"].get("length_px"),
        )
        for item in listed
    ]

    prompt = build_repaired_line_review_prompt(
        map_id, candidate_lines, omitted_count=omitted_count
    )
    content_parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    if overlay_path.is_file():
        content_parts.append(
            {"type": "image_url", "image_url": {"url": _image_data_url(overlay_path)}}
        )
    messages = [{"role": "user", "content": content_parts}]

    review_dir = output_root / REVIEW_DIR_NAME
    api_url = normalize_chat_completions_url(config.base_url)
    _write_json(
        review_dir / "ai_line_review_request.json",
        _safe_request_snapshot(config, api_url=api_url, messages=messages),
    )

    response_payload, content, api_url = _chat_completion(
        config, messages=messages, urlopen=urlopen
    )
    _write_json(review_dir / "ai_line_review_raw_response.json", response_payload)

    review = sanitize_review_payload(_extract_json_payload(content), known_ids=known_ids)
    review.update(
        {
            "map_id": map_id,
            "created_at_utc": _utc_now(),
            "repaired_geojson": str(repaired_path),
            "reviewed_candidate_count": len(listed),
            "omitted_candidate_count": omitted_count,
            "overlay_image_used": str(overlay_path) if overlay_path.is_file() else None,
        }
    )
    review_path = review_dir / "AI_LINE_REVIEW.json"
    _write_json(review_path, review)

    assert repaired_path.read_bytes() == repaired_bytes, (
        "AI review must not modify the repaired candidate layer"
    )

    report = {
        "ok": True,
        "stage": "after_repaired_before_export",
        "provider": config.provider,
        "api_url": api_url,
        "model": config.model,
        "map_id": map_id,
        "review_only": True,
        "review_path": str(review_path),
        "request_path": str(review_dir / "ai_line_review_request.json"),
        "raw_response_path": str(review_dir / "ai_line_review_raw_response.json"),
        "api_key_configured": bool(config.api_key.strip()),
        "api_key_redacted": _redact_api_key(config.api_key),
        "writes_coordinates": False,
        "writes_checked_yes": False,
        "repaired_layer_unmodified": True,
        "suggestion_count": len(review["closure_suggestions"]),
        "suspect_count": len(review["suspect_line_ids"]),
        "dropped_unknown_id_count": len(review["dropped_unknown_ids"]),
    }
    _write_json(review_dir / "AI_LINE_REVIEW_REPORT.json", report)
    return report
