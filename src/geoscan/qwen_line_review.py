from __future__ import annotations

import base64
import csv
import json
from pathlib import Path
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from geoscan.ai_visual_judge import validate_ai_review_response
from geoscan.accuracy_ai_workbench import LINE_AI_ALLOWED_OBJECT_CLASSES


QWEN_LINE_RESPONSE_FIELDS = [
    "feature_id",
    "object_class",
    "confidence",
    "should_close",
    "missing_edges",
    "duplicate_group",
    "review_note",
]
QWEN_DISAGREEMENT_OBJECT_CLASSES = [
    "table_grid",
    "text_interference",
    "noise",
    "uncertain",
]
DEFAULT_SILICONFLOW_QWEN_URL = "https://api.siliconflow.cn/v1/chat/completions"
DEFAULT_QWEN_VL_MODEL = "Qwen/Qwen3-VL-32B-Thinking"
FORBIDDEN_QWEN_RESPONSE_KEYS = {
    "coordinate",
    "coordinates",
    "geometry",
    "x",
    "y",
    "x_px",
    "y_px",
    "crop_bbox",
    "crop_bbox_pixels",
    "checked",
    "checked_yes",
    "delete_geometry",
    "geological_interpretation",
}
ALLOWED_MISSING_EDGES = {"top", "bottom", "left", "right"}


class QwenLineReviewError(RuntimeError):
    pass


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _is_nonempty(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return bool(_clean_text(value))


def _extract_json_payload(content: str) -> Any:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return payload
    raise QwenLineReviewError("Qwen response did not contain a JSON object or array")


def _coerce_response_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("responses", "rows", "data", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        else:
            rows = [payload]
    else:
        raise QwenLineReviewError("Qwen JSON payload must be an object or array")
    if not all(isinstance(row, dict) for row in rows):
        raise QwenLineReviewError("Qwen response rows must be JSON objects")
    return rows


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = _clean_text(value).lower()
    if not text:
        return False
    if text in {"1", "true", "yes", "y", "是", "需要"}:
        return True
    if text in {"0", "false", "no", "n", "否", "不"}:
        return False
    raise ValueError(f"should_close must be boolean-like, got {value!r}")


def _coerce_confidence(value: object) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("confidence must be numeric")
    confidence = float(value)
    if confidence > 1.0 and confidence <= 100.0:
        confidence = confidence / 100.0
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("confidence must be from 0 to 1")
    return round(confidence, 6)


def _coerce_missing_edges(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        text = value
        for separator in ("；", ";", "，", ",", "|"):
            text = text.replace(separator, " ")
        items = [item.strip().lower() for item in text.split() if item.strip()]
    elif isinstance(value, list):
        items = [_clean_text(item).lower() for item in value if _clean_text(item)]
    else:
        raise ValueError("missing_edges must be a list or separator-delimited string")
    invalid = [item for item in items if item not in ALLOWED_MISSING_EDGES]
    if invalid:
        raise ValueError(f"invalid missing_edges: {invalid}")
    return items


def normalize_qwen_line_response(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    forbidden = [
        key
        for key, value in row.items()
        if key.strip().lower() in FORBIDDEN_QWEN_RESPONSE_KEYS and _is_nonempty(value)
    ]
    if forbidden:
        raise ValueError(f"Qwen response contained forbidden key(s): {sorted(forbidden)}")

    extra_keys = sorted(key for key in row if key not in QWEN_LINE_RESPONSE_FIELDS)
    feature_id = _clean_text(row.get("feature_id"))
    if not feature_id:
        raise ValueError("feature_id is required")
    object_class = _clean_text(row.get("object_class")).lower()
    if object_class not in LINE_AI_ALLOWED_OBJECT_CLASSES:
        raise ValueError(f"object_class must be one of {LINE_AI_ALLOWED_OBJECT_CLASSES}")

    normalized = {
        "feature_id": feature_id,
        "object_class": object_class,
        "confidence": _coerce_confidence(row.get("confidence")),
        "should_close": _coerce_bool(row.get("should_close")),
        "missing_edges": _coerce_missing_edges(row.get("missing_edges")),
        "duplicate_group": _clean_text(row.get("duplicate_group")),
        "review_note": _clean_text(row.get("review_note"))[:300],
    }
    normalized = validate_ai_review_response(normalized)
    return {field: normalized[field] for field in QWEN_LINE_RESPONSE_FIELDS}, {
        "ignored_extra_keys": extra_keys,
    }


def parse_qwen_line_response_content(
    content: str,
    *,
    expected_feature_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected = set(expected_feature_ids or [])
    rows = _coerce_response_list(_extract_json_payload(content))
    responses: list[dict[str, Any]] = []
    seen: set[str] = set()
    ignored_extra_key_count = 0
    invalid_rows: list[dict[str, Any]] = []
    unknown_feature_ids: list[str] = []

    for index, row in enumerate(rows, start=1):
        try:
            response, metadata = normalize_qwen_line_response(row)
        except ValueError as exc:
            invalid_rows.append({"row_index": index, "error": str(exc)})
            continue
        feature_id = response["feature_id"]
        if expected and feature_id not in expected:
            unknown_feature_ids.append(feature_id)
            continue
        if feature_id in seen:
            invalid_rows.append({"row_index": index, "feature_id": feature_id, "error": "duplicate feature_id"})
            continue
        seen.add(feature_id)
        ignored_extra_key_count += len(metadata["ignored_extra_keys"])
        responses.append(response)

    return responses, {
        "raw_item_count": len(rows),
        "valid_response_count": len(responses),
        "invalid_response_count": len(invalid_rows),
        "invalid_rows": invalid_rows,
        "ignored_extra_key_count": ignored_extra_key_count,
        "unknown_feature_id_count": len(unknown_feature_ids),
        "unknown_feature_ids": unknown_feature_ids,
        "missing_feature_ids": sorted(expected - seen),
        "missing_response_count": len(expected - seen) if expected else 0,
    }


def write_qwen_line_responses_jsonl(responses: list[dict[str, Any]], path: Path) -> None:
    rows: list[dict[str, Any]] = []
    for row in responses:
        normalized, _ = normalize_qwen_line_response(row)
        rows.append(normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def write_qwen_line_response_csv(responses: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=QWEN_LINE_RESPONSE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in responses:
            normalized, _ = normalize_qwen_line_response(row)
            writer.writerow(
                {
                    **normalized,
                    "missing_edges": ";".join(normalized["missing_edges"]),
                    "should_close": "true" if normalized["should_close"] else "false",
                }
            )


def _image_data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime_type = "image/png" if suffix == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _build_prompt(batch: list[dict[str, Any]]) -> str:
    feature_lines = [
        f"- {row['feature_id']}: priority={row.get('review_priority','')}, "
        f"hint={row.get('object_class_hint','')}, line_length={row.get('line_length','')}"
        for row in batch
    ]
    return "\n".join(
        [
            "你是地图线条结构审查员，只做结构判断，不解释地质含义。",
            "必须仅返回 JSON 数组，数组内每个对象只能包含这些字段：",
            ", ".join(QWEN_LINE_RESPONSE_FIELDS),
            "object_class 只能从以下值中选择：",
            ", ".join(LINE_AI_ALLOWED_OBJECT_CLASSES),
            "confidence 使用 0 到 1 的数字；should_close 使用布尔值；missing_edges 使用 top/bottom/left/right 数组。",
            "禁止输出坐标、几何、删除建议、checked/checked=yes 或地质解释。",
            "需要判断的 feature_id：",
            *feature_lines,
        ]
    )


def _build_disagreement_prompt(batch: list[dict[str, Any]]) -> str:
    feature_lines = [
        f"- {row['feature_id']}: first_qwen={row.get('first_pass_object_class','')}, "
        f"local_rule={row.get('local_object_class','')}, local_note={row.get('local_review_note','')}"
        for row in batch
    ]
    return "\n".join(
        [
            "你是地图线条二次复核员。任务是专门复核 Qwen 一审 table_grid 与本地规则 text_interference 的冲突项。",
            "不要默认“线就是表格线”。必须结合 original crop、放大 crop、带上下文的大 crop 判断。",
            "只能返回 JSON 数组，数组内每个对象只能包含这些字段：",
            ", ".join(QWEN_LINE_RESPONSE_FIELDS),
            "object_class 只能从以下值中选择：",
            ", ".join(QWEN_DISAGREEMENT_OBJECT_CLASSES),
            "分类标准：",
            "table_grid：属于表格、标题栏、图框、格网结构，附近有规则平行线、交叉线、单元格边界或明确闭合关系。",
            "text_interference：属于文字、数字、标注、OCR 笔画、字符横竖撇捺，即使看起来像短线也应归入此类。",
            "noise：孤立脏点、扫描噪声、无结构意义短线。",
            "uncertain：上下文仍不足以判断。",
            "特别规则：如果目标线紧贴汉字、数字、比例尺文字或表格内数字，且不能组成明确单元格边界，优先判 text_interference 或 uncertain，不要强行判 table_grid。",
            "confidence 使用 0 到 1 的数字；should_close 使用布尔值；missing_edges 使用 top/bottom/left/right 数组。",
            "禁止输出坐标、几何、删除建议、checked/checked=yes 或地质解释。",
            "需要复核的 feature_id：",
            *feature_lines,
        ]
    )


def call_siliconflow_qwen_line_batch(
    batch: list[dict[str, Any]],
    *,
    api_key: str,
    api_url: str = DEFAULT_SILICONFLOW_QWEN_URL,
    model: str = DEFAULT_QWEN_VL_MODEL,
    timeout_seconds: int = 120,
    max_tokens: int = 2048,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not api_key:
        raise QwenLineReviewError("SILICONFLOW_API_KEY or DASHSCOPE_API_KEY is required")
    content: list[dict[str, Any]] = [{"type": "text", "text": _build_prompt(batch)}]
    for request in batch:
        crop_path = Path(_clean_text(request.get("crop_path")))
        if not crop_path.exists():
            raise QwenLineReviewError(f"Missing crop image: {crop_path}")
        content.append({"type": "text", "text": f"feature_id: {request['feature_id']}"})
        content.append({"type": "image_url", "image_url": {"url": _image_data_url(crop_path)}})

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise QwenLineReviewError(f"Qwen HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise QwenLineReviewError(f"Qwen API call failed: {exc}") from exc

    try:
        content_text = response_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise QwenLineReviewError("Qwen response missing choices[0].message.content") from exc

    responses, parse_report = parse_qwen_line_response_content(
        content_text,
        expected_feature_ids=[str(row["feature_id"]) for row in batch],
    )
    parse_report.update(
        {
            "api_url": api_url,
            "model": model,
            "batch_request_count": len(batch),
            "timeout_seconds": timeout_seconds,
            "max_tokens": max_tokens,
        }
    )
    return responses, parse_report


def call_siliconflow_qwen_disagreement_batch(
    batch: list[dict[str, Any]],
    *,
    api_key: str,
    api_url: str = DEFAULT_SILICONFLOW_QWEN_URL,
    model: str = DEFAULT_QWEN_VL_MODEL,
    timeout_seconds: int = 180,
    max_tokens: int = 768,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not api_key:
        raise QwenLineReviewError("SILICONFLOW_API_KEY or DASHSCOPE_API_KEY is required")
    content: list[dict[str, Any]] = [{"type": "text", "text": _build_disagreement_prompt(batch)}]
    for request in batch:
        content.append({"type": "text", "text": f"feature_id: {request['feature_id']}"})
        for label, key in (
            ("original crop", "original_crop_path"),
            ("enlarged crop", "enlarged_crop_path"),
            ("context crop with target box", "context_crop_path"),
        ):
            crop_path = Path(_clean_text(request.get(key)))
            if not crop_path.exists():
                raise QwenLineReviewError(f"Missing {label}: {crop_path}")
            content.append({"type": "text", "text": f"{request['feature_id']} {label}"})
            content.append({"type": "image_url", "image_url": {"url": _image_data_url(crop_path)}})

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise QwenLineReviewError(f"Qwen HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise QwenLineReviewError(f"Qwen API call failed: {exc}") from exc

    try:
        content_text = response_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise QwenLineReviewError("Qwen response missing choices[0].message.content") from exc

    responses, parse_report = parse_qwen_line_response_content(
        content_text,
        expected_feature_ids=[str(row["feature_id"]) for row in batch],
    )
    parse_report.update(
        {
            "api_url": api_url,
            "model": model,
            "batch_request_count": len(batch),
            "timeout_seconds": timeout_seconds,
            "max_tokens": max_tokens,
        }
    )
    return responses, parse_report


ProviderCallable = Callable[[list[dict[str, Any]]], tuple[list[dict[str, Any]], dict[str, Any]]]
