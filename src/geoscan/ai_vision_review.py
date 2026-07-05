from __future__ import annotations

import base64
import json
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PIL import Image


class AiVisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AiVisionConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout_seconds: int = 60
    max_tokens: int = 1024
    temperature: float = 0.0
    image_max_side: int = 1024
    image_jpeg_quality: int = 82


UrlopenCallable = Callable[[object, int], object]


def _redact_api_key(api_key: str) -> str:
    value = str(api_key or "").strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def normalize_chat_completions_url(base_url: str) -> str:
    value = str(base_url or "").strip().rstrip("/")
    if not value:
        raise AiVisionError("AI Base URL is required")
    if value.lower().endswith("/chat/completions"):
        return value
    return f"{value}/chat/completions"


def build_map_structure_prompt(map_id: str) -> str:
    return "\n".join(
        [
            f"你是 GeoScan 的地图结构复核员。当前图幅: {map_id}。",
            "任务：观察整张图，只给出会影响后续线条闭合、表格/图框重建、文字占位的关键区域建议。",
            "边界：review_only 必须为 true。不要输出最终矢量坐标，不要输出可直接写入 WL/WT/WP 的几何，不要写 checked=yes。",
            "不要解释或发明地质含义、地层代码、矿体边界或缺失内容。",
            "允许输出 bbox_hint，但它只能是 0..1 的图像区域提示，供人工或后续脚本裁图复核，绝不是最终矢量坐标。",
            "最多返回 8 个 regions。只返回一个 JSON 对象，不要 Markdown，不要额外说明。",
            "格式：",
            "{",
            '  "review_only": true,',
            f'  "map_id": "{map_id}",',
            '  "regions": [',
            '    {"role": "main_frame|grid|title|table|legend|responsibility_table|text_dense|small_boxes|uncertain", "bbox_hint": [0.0,0.0,1.0,1.0], "confidence": 0.0, "review_note": "..."}',
            "  ],",
            '  "line_strategy": ["..."],',
            '  "text_strategy": ["..."],',
            '  "risks": ["..."]',
            "}",
        ]
    )


def _extract_json_payload(content: str) -> Any:
    text = str(content or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return payload
    raise AiVisionError("AI response did not contain a JSON object")


def _coerce_confidence(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if confidence > 1.0 and confidence <= 100.0:
        confidence = confidence / 100.0
    return max(0.0, min(1.0, confidence))


def _normalize_bbox_hint(value: object) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    result: list[float] = []
    for item in value:
        if isinstance(item, bool):
            return None
        try:
            result.append(max(0.0, min(1.0, float(item))))
        except (TypeError, ValueError):
            return None
    if result[2] < result[0]:
        result[0], result[2] = result[2], result[0]
    if result[3] < result[1]:
        result[1], result[3] = result[3], result[1]
    return result


def _normalize_regions(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    regions: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, dict):
            continue
        region: dict[str, Any] = {
            "role": str(row.get("role") or "uncertain").strip() or "uncertain",
            "review_note": str(row.get("review_note") or "").strip()[:500],
        }
        bbox = _normalize_bbox_hint(row.get("bbox_hint"))
        if bbox is not None:
            region["bbox_hint"] = bbox
        confidence = _coerce_confidence(row.get("confidence"))
        if confidence is not None:
            region["confidence"] = confidence
        regions.append(region)
    return regions


def _normalize_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip()[:500] for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()[:500]]
    return []


def _normalize_analysis(payload: Any, *, map_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AiVisionError("AI JSON payload must be an object")
    return {
        "review_only": True,
        "map_id": str(payload.get("map_id") or map_id),
        "bbox_not_final_geometry": True,
        "ai_wrote_coordinates": False,
        "checked_yes_written": False,
        "geological_content_modified": False,
        "regions": _normalize_regions(payload.get("regions")),
        "line_strategy": _normalize_string_list(payload.get("line_strategy")),
        "text_strategy": _normalize_string_list(payload.get("text_strategy")),
        "risks": _normalize_string_list(payload.get("risks")),
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _image_data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime_type = "image/png" if suffix == ".png" else "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _prepare_image_for_ai(image_path: Path, ai_dir: Path, *, max_side: int, quality: int) -> Path:
    ai_dir.mkdir(parents=True, exist_ok=True)
    prepared = ai_dir / "ai_visual_input.jpg"
    side = max(512, min(1600, int(max_side or 1024)))
    jpeg_quality = max(55, min(95, int(quality or 82)))
    with Image.open(image_path) as image:
        rgb = image.convert("RGB")
        rgb.thumbnail((side, side), Image.Resampling.LANCZOS)
        rgb.save(prepared, quality=jpeg_quality, optimize=True)
    return prepared


def _validate_config(config: AiVisionConfig) -> str:
    provider = str(config.provider or "").strip()
    if not provider or provider == "none":
        raise AiVisionError("AI Provider must not be none for this action")
    if not str(config.api_key or "").strip():
        raise AiVisionError("AI API Key is required")
    if not str(config.model or "").strip():
        raise AiVisionError("AI Model is required")
    return normalize_chat_completions_url(config.base_url)


def _chat_completion(
    config: AiVisionConfig,
    *,
    messages: list[dict[str, Any]],
    urlopen: UrlopenCallable | None = None,
) -> tuple[dict[str, Any], str, str]:
    api_url = _validate_config(config)
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    request = urllib.request.Request(
        api_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = urlopen or urllib.request.urlopen
    try:
        with opener(request, timeout=config.timeout_seconds) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise AiVisionError(f"AI HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise AiVisionError(f"AI API call failed: {exc}") from exc

    try:
        content = response_payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AiVisionError("AI response missing choices[0].message.content") from exc
    return response_payload, str(content), api_url


def _safe_request_snapshot(config: AiVisionConfig, *, api_url: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "provider": config.provider,
        "api_url": api_url,
        "model": config.model,
        "timeout_seconds": config.timeout_seconds,
        "max_tokens": config.max_tokens,
        "temperature": config.temperature,
        "api_key_configured": bool(config.api_key.strip()),
        "api_key_redacted": _redact_api_key(config.api_key),
        "messages": messages,
    }


def test_ai_connection(
    config: AiVisionConfig,
    *,
    urlopen: UrlopenCallable | None = None,
) -> dict[str, Any]:
    messages = [
        {
            "role": "user",
            "content": 'Return exactly this JSON object if the connection works: {"ok": true, "message": "connected"}',
        }
    ]
    response_payload, content, api_url = _chat_completion(config, messages=messages, urlopen=urlopen)
    parsed: Any
    try:
        parsed = _extract_json_payload(content)
    except AiVisionError:
        parsed = None
    return {
        "ok": True,
        "provider": config.provider,
        "api_url": api_url,
        "model": config.model,
        "api_key_configured": bool(config.api_key.strip()),
        "api_key_redacted": _redact_api_key(config.api_key),
        "response_content": content[:1000],
        "parsed_json": parsed,
        "response_id": response_payload.get("id") if isinstance(response_payload, dict) else None,
    }


test_ai_connection.__test__ = False


def analyze_map_image_with_ai(
    config: AiVisionConfig,
    *,
    image_path: Path,
    output_root: Path,
    map_id: str,
    urlopen: UrlopenCallable | None = None,
) -> dict[str, Any]:
    if not Path(image_path).is_file():
        raise FileNotFoundError(image_path)
    output_root = Path(output_root)
    ai_dir = output_root / "AI_VISUAL_REVIEW"
    prepared_image = _prepare_image_for_ai(
        Path(image_path),
        ai_dir,
        max_side=config.image_max_side,
        quality=config.image_jpeg_quality,
    )
    prompt = build_map_structure_prompt(map_id)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": _image_data_url(prepared_image)}},
            ],
        }
    ]
    api_url = normalize_chat_completions_url(config.base_url)
    _write_json(
        ai_dir / "ai_visual_request.json",
        _safe_request_snapshot(config, api_url=api_url, messages=messages),
    )

    response_payload, content, api_url = _chat_completion(config, messages=messages, urlopen=urlopen)
    _write_json(ai_dir / "ai_visual_raw_response.json", response_payload)
    analysis = _normalize_analysis(_extract_json_payload(content), map_id=map_id)
    analysis_path = ai_dir / "ai_visual_analysis.json"
    _write_json(analysis_path, analysis)

    report = {
        "ok": True,
        "provider": config.provider,
        "api_url": api_url,
        "model": config.model,
        "map_id": map_id,
        "review_only": True,
        "analysis_path": str(analysis_path),
        "request_path": str(ai_dir / "ai_visual_request.json"),
        "raw_response_path": str(ai_dir / "ai_visual_raw_response.json"),
        "prepared_image_path": str(prepared_image),
        "api_key_configured": bool(config.api_key.strip()),
        "api_key_redacted": _redact_api_key(config.api_key),
        "writes_coordinates": False,
        "writes_checked_yes": False,
    }
    _write_json(ai_dir / "AI_VISUAL_REVIEW_REPORT.json", report)

    source_copy = ai_dir / f"{str(map_id).lower()}_source_reference{Path(image_path).suffix.lower()}"
    if not source_copy.exists():
        shutil.copy2(image_path, source_copy)
    return report
