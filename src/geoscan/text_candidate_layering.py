from __future__ import annotations

import csv
from dataclasses import dataclass
import html
import json
from pathlib import Path
import re
from typing import Any


@dataclass(frozen=True)
class TextZone:
    category: str
    target_file: str
    layer: str
    bbox: tuple[float, float, float, float]
    priority_rank: int = 50


DEFAULT_SECTION_TEXT_ZONES: tuple[TextZone, ...] = (
    TextZone("title_text", "TEXT_TITLE.WT", "TEXT_TITLE_REVIEW", (0.25, 0.82, 0.75, 1.0), 0),
    TextZone("sample_table_text", "TEXT_SAMPLE_TABLE.WT", "TEXT_SAMPLE_TABLE_REVIEW", (0.12, 0.05, 0.68, 0.32), 5),
    TextZone("legend_text", "TEXT_LEGEND.WT", "TEXT_LEGEND_REVIEW", (0.62, 0.04, 0.84, 0.40), 1),
    TextZone("title_block_text", "TEXT_TITLE_BLOCK.WT", "TEXT_TITLE_BLOCK_REVIEW", (0.84, 0.02, 1.0, 0.34), 2),
)

_SPREADSHEET_NOTATION_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?E[+-]?\d+$", re.IGNORECASE)
_PUNCTUATION_ONLY_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)

HIGH_PRIORITY_CATEGORIES = {"title_text", "legend_text", "title_block_text"}
TEXT_AI_ALLOWED_ROLES = [
    "title_text",
    "scale_text",
    "legend_text",
    "title_block_text",
    "sample_table_text",
    "frame_coordinate_text",
    "map_body_annotation",
    "uncertain_text",
    "noise",
]
TEXT_AI_FORBIDDEN_RESPONSE_KEYS = {
    "coordinate",
    "coordinates",
    "geometry",
    "x",
    "y",
    "x_px",
    "y_px",
    "checked",
    "checked_yes",
    "geological_interpretation",
}
TEXT_AI_RESPONSE_TEMPLATE_FIELDS = [
    "candidate_id",
    "ocr_text",
    "ocr_confidence",
    "category",
    "is_text",
    "reject_as_noise",
    "suggested_text",
    "text_role",
    "confidence",
    "review_note",
]


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _normalized_point(x: float, y: float, *, page_width: float, page_height: float) -> tuple[float, float]:
    if page_width <= 0 or page_height <= 0:
        raise ValueError("page_width and page_height must be positive")
    return float(x) / float(page_width), float(y) / float(page_height)


def _zone_contains(zone: TextZone, x_norm: float, y_norm: float) -> bool:
    minx, miny, maxx, maxy = zone.bbox
    return minx <= x_norm <= maxx and miny <= y_norm <= maxy


def _fallback_zone(x_norm: float, y_norm: float) -> TextZone:
    if x_norm <= 0.08 or y_norm <= 0.08 or y_norm >= 0.92:
        return TextZone("frame_coordinate_text", "TEXT_FRAME.WT", "TEXT_FRAME_REVIEW", (0.0, 0.0, 1.0, 1.0), 6)
    if 0.30 <= y_norm <= 0.86:
        return TextZone("map_body_annotation", "TEXT_MAP_BODY.WT", "TEXT_MAP_BODY_REVIEW", (0.0, 0.0, 1.0, 1.0), 3)
    return TextZone("uncertain_text", "TEXT_REVIEW.WT", "TEXT_REVIEW", (0.0, 0.0, 1.0, 1.0), 9)


def classify_text_candidate(
    x: float,
    y: float,
    text: str,
    confidence: float,
    *,
    page_width: float,
    page_height: float,
    zones: tuple[TextZone, ...] | list[TextZone] = DEFAULT_SECTION_TEXT_ZONES,
) -> dict[str, Any]:
    x_norm, y_norm = _normalized_point(x, y, page_width=page_width, page_height=page_height)
    matching = [zone for zone in zones if _zone_contains(zone, x_norm, y_norm)]
    zone = sorted(matching, key=lambda item: item.priority_rank)[0] if matching else _fallback_zone(x_norm, y_norm)
    confidence_value = _safe_float(confidence)
    if confidence_value >= 0.95:
        priority = "high"
    elif confidence_value >= 0.80:
        priority = "medium"
    else:
        priority = "low"
    return {
        "category": zone.category,
        "suggested_target_file": zone.target_file,
        "suggested_layer": zone.layer,
        "review_priority": priority,
        "import_status": "review_required",
        "ocr_text": _clean_text(text),
        "ocr_confidence": confidence_value,
        "x_norm": round(x_norm, 6),
        "y_norm": round(y_norm, 6),
    }


def text_quality_flags(text: object, confidence: object, *, low_confidence_threshold: float = 0.55) -> list[str]:
    value = _clean_text(text)
    flags: list[str] = []
    if not value:
        flags.append("empty_text")
    if _safe_float(confidence) < low_confidence_threshold:
        flags.append("low_confidence")
    if value and _SPREADSHEET_NOTATION_RE.match(value):
        flags.append("spreadsheet_notation")
    if value and _PUNCTUATION_ONLY_RE.match(value):
        flags.append("punctuation_only")
    return flags


def _is_manual_confirmed(row: dict[str, object]) -> bool:
    return str(row.get("review_status") or "") == "manual_confirmed" and bool(_clean_text(row.get("confirmed_text")))


def _review_priority(category: str, confidence: float, flags: list[str]) -> str:
    if flags:
        return "skip"
    if category in HIGH_PRIORITY_CATEGORIES and confidence >= 0.80:
        return "high"
    if confidence >= 0.80:
        return "medium"
    return "low"


def layer_text_candidate(row: dict[str, object]) -> dict[str, Any]:
    item = dict(row)
    text = _clean_text(item.get("ocr_text"))
    confidence = _safe_float(item.get("ocr_confidence") or item.get("confidence"))
    category = _clean_text(item.get("category")) or "uncertain_text"
    flags = text_quality_flags(text, confidence)

    item["quality_flags"] = flags
    item["checked"] = "no"
    if _is_manual_confirmed(item):
        item["candidate_tier"] = "promote_ready"
        item["output_text"] = _clean_text(item.get("confirmed_text"))
        item["review_priority"] = "confirmed"
        item["import_status"] = "ready_after_manual_confirmation"
    elif flags:
        item["candidate_tier"] = "reject_noise"
        item["output_text"] = ""
        item["review_priority"] = "skip"
        item["import_status"] = "rejected_before_review"
    else:
        item["candidate_tier"] = "review_queue"
        item["output_text"] = ""
        item["review_priority"] = _review_priority(category, confidence, flags)
        item["import_status"] = "review_required"
    return item


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _candidate_id(row: dict[str, object], index: int) -> str:
    for key in ("candidate_id", "crop_path", "feature_id", "id"):
        raw = _clean_text(row.get(key))
        if raw:
            return re.sub(r"\.[A-Za-z0-9]+$", "", raw)
    return f"text_candidate_{index:04d}"


def _bbox_pixels(
    row: dict[str, object],
    *,
    image_width: int,
    image_height: int,
    padding_px: int,
) -> tuple[int, int, int, int]:
    left = _safe_int(row.get("bbox_left_px"))
    top = _safe_int(row.get("bbox_top_px"))
    right = _safe_int(row.get("bbox_right_px"))
    bottom = _safe_int(row.get("bbox_bottom_px"))
    if right <= left or bottom <= top:
        x = _safe_float(row.get("x_px"), image_width / 2.0)
        y_bottom_origin = _safe_float(row.get("y_px"), image_height / 2.0)
        y = image_height - y_bottom_origin
        half_width = max(40, padding_px * 4)
        half_height = max(18, padding_px * 2)
        left = int(round(x - half_width))
        right = int(round(x + half_width))
        top = int(round(y - half_height))
        bottom = int(round(y + half_height))
    left = max(0, left - padding_px)
    top = max(0, top - padding_px)
    right = min(image_width, right + padding_px)
    bottom = min(image_height, bottom + padding_px)
    if right <= left:
        right = min(image_width, left + 1)
    if bottom <= top:
        bottom = min(image_height, top + 1)
    return left, top, right, bottom


def write_text_ai_review_requests(
    rows: list[dict[str, object]],
    *,
    output_dir: Path,
    raster_path: Path,
    provider: str = "none",
    include_priorities: tuple[str, ...] = ("high", "medium"),
    crop_padding_px: int = 12,
    max_requests: int | None = None,
) -> dict[str, Any]:
    """Write offline AI review requests for OCR text candidates.

    The request contract is deliberately review-only: it can suggest text and
    noise status, but it cannot output coordinates or mark content checked.
    """

    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)
    crops_dir = output_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    requests_path = output_dir / "text_ai_requests.jsonl"
    responses_path = output_dir / "text_ai_responses.jsonl"

    image = Image.open(raster_path).convert("RGB")
    image_width, image_height = image.size
    rows_out: list[dict[str, Any]] = []
    include_priority_set = set(include_priorities)

    for index, row in enumerate(rows, start=1):
        if str(row.get("candidate_tier") or "") != "review_queue":
            continue
        if str(row.get("review_priority") or "") not in include_priority_set:
            continue
        candidate_id = _candidate_id(row, index)
        crop_name = f"{candidate_id}.jpg"
        bbox = _bbox_pixels(
            row,
            image_width=image_width,
            image_height=image_height,
            padding_px=max(0, int(crop_padding_px)),
        )
        image.crop(bbox).save(crops_dir / crop_name, quality=92)
        rows_out.append(
            {
                "candidate_id": candidate_id,
                "provider": provider,
                "raster_path": str(raster_path),
                "crop_path": str(crops_dir / crop_name),
                "bbox_pixels": list(bbox),
                "category": _clean_text(row.get("category")) or "uncertain_text",
                "suggested_target_file": _clean_text(row.get("suggested_target_file")),
                "x_norm": row.get("x_norm", ""),
                "y_norm": row.get("y_norm", ""),
                "ocr_text": _clean_text(row.get("ocr_text")),
                "ocr_confidence": _safe_float(row.get("ocr_confidence")),
                "instruction": (
                    "Review OCR text only. Compare the crop with the OCR text. "
                    "Return constrained JSON. Do not output coordinates, do not infer geological meaning, "
                    "and do not mark any item checked."
                ),
                "allowed_outputs": {
                    "is_text": "boolean",
                    "reject_as_noise": "boolean",
                    "suggested_text": "string",
                    "text_role": TEXT_AI_ALLOWED_ROLES,
                    "confidence": "number from 0 to 1",
                    "review_note": "short evidence note",
                    "must_not_output": ["coordinates", "geological_interpretation", "checked_yes"],
                },
            }
        )
        if max_requests is not None and len(rows_out) >= max_requests:
            break

    requests_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows_out),
        encoding="utf-8",
    )
    if not responses_path.exists():
        responses_path.write_text("", encoding="utf-8")
    return {
        "provider": provider,
        "request_count": len(rows_out),
        "requests_path": str(requests_path),
        "responses_path": str(responses_path),
        "crops_dir": str(crops_dir),
        "max_requests": max_requests,
    }


def load_text_ai_review_requests(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON on line {line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"{path}: request on line {line_number} must be a JSON object")
        candidate_id = _clean_text(row.get("candidate_id"))
        if not candidate_id:
            raise ValueError(f"{path}: candidate_id is required on line {line_number}")
        crop_path = _clean_text(row.get("crop_path"))
        if not crop_path:
            raise ValueError(f"{path}: crop_path is required on line {line_number}")
        rows.append(row)
    return rows


def _relative_display_path(path: Path, *, base_dir: Path) -> str:
    try:
        return path.resolve().relative_to(base_dir.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _write_text_ai_response_template(requests: list[dict[str, Any]], path: Path) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=TEXT_AI_RESPONSE_TEMPLATE_FIELDS)
        writer.writeheader()
        for request in requests:
            writer.writerow(
                {
                    "candidate_id": _clean_text(request.get("candidate_id")),
                    "ocr_text": _clean_text(request.get("ocr_text")),
                    "ocr_confidence": request.get("ocr_confidence", ""),
                    "category": _clean_text(request.get("category")),
                    "is_text": "",
                    "reject_as_noise": "",
                    "suggested_text": "",
                    "text_role": "",
                    "confidence": "",
                    "review_note": "",
                }
            )


def _render_text_ai_review_card(request: dict[str, Any], *, base_dir: Path) -> str:
    candidate_id = html.escape(_clean_text(request.get("candidate_id")))
    crop_path = Path(_clean_text(request.get("crop_path")))
    crop_src = html.escape(_relative_display_path(crop_path, base_dir=base_dir))
    ocr_text = html.escape(_clean_text(request.get("ocr_text")))
    confidence = html.escape(str(request.get("ocr_confidence", "")))
    category = html.escape(_clean_text(request.get("category")))
    bbox = html.escape(str(request.get("bbox_pixels", "")))
    return f"""<section class="candidate">
  <div class="crop"><img src="{crop_src}" alt="{candidate_id}"></div>
  <div class="content">
    <div class="candidate-id">{candidate_id}</div>
    <div class="ocr">{ocr_text}</div>
    <div class="meta">
      <span>category: {category}</span>
      <span>confidence: {confidence}</span>
      <span>bbox: {bbox}</span>
    </div>
  </div>
</section>"""


def _write_text_ai_review_html(requests: list[dict[str, Any]], path: Path) -> None:
    category_counts: dict[str, int] = {}
    for request in requests:
        category = _clean_text(request.get("category")) or "uncertain_text"
        category_counts[category] = category_counts.get(category, 0) + 1
    summary = " ".join(
        f"<span>{html.escape(category)} <strong>{count}</strong></span>"
        for category, count in sorted(category_counts.items())
    )
    cards = "\n".join(_render_text_ai_review_card(request, base_dir=path.parent) for request in requests)
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Phase6 文字 AI/人工复核</title>
  <style>
    :root {{
      --ink: #1c2428;
      --muted: #667174;
      --line: #d9dfdc;
      --paper: #f8faf7;
      --accent: #1f6f62;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
      letter-spacing: 0;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 2;
      background: #fff;
      border-bottom: 1px solid var(--line);
      padding: 24px 30px 16px;
    }}
    h1 {{
      margin: 0 0 10px;
      font-size: 23px;
      font-weight: 650;
    }}
    .summary {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px 18px;
      color: var(--muted);
      font-size: 14px;
    }}
    main {{
      max-width: 1280px;
      margin: 0 auto;
      padding: 20px 24px 46px;
    }}
    .candidate {{
      display: grid;
      grid-template-columns: 260px minmax(0, 1fr);
      gap: 18px;
      align-items: center;
      padding: 14px 0;
      border-bottom: 1px solid var(--line);
    }}
    .crop {{
      width: 260px;
      min-height: 76px;
      background: #fff;
      border: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: center;
      overflow: hidden;
    }}
    .crop img {{
      max-width: 100%;
      max-height: 150px;
    }}
    .candidate-id {{
      color: var(--accent);
      font-size: 13px;
      margin-bottom: 5px;
    }}
    .ocr {{
      font-size: 22px;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }}
    .meta {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 13px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px 14px;
    }}
    @media (max-width: 760px) {{
      header {{ position: static; padding: 20px 16px 14px; }}
      main {{ padding: 12px 16px 32px; }}
      .candidate {{ grid-template-columns: 1fr; gap: 10px; }}
      .crop {{ width: 100%; }}
      .ocr {{ font-size: 19px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Phase6 文字 AI/人工复核</h1>
    <div class="summary">
      <span>请求 <strong>{len(requests)}</strong></span>
      {summary}
    </div>
  </header>
  <main>
    {cards}
  </main>
</body>
</html>
"""
    path.write_text(html_text, encoding="utf-8")


def write_text_ai_review_workbench(
    requests: list[dict[str, Any]],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "text_ai_review_page.html"
    template_csv = output_dir / "text_ai_response_template.csv"
    _write_text_ai_response_template(requests, template_csv)
    _write_text_ai_review_html(requests, html_path)
    category_counts: dict[str, int] = {}
    for request in requests:
        category = _clean_text(request.get("category")) or "uncertain_text"
        category_counts[category] = category_counts.get(category, 0) + 1
    return {
        "request_count": len(requests),
        "html": str(html_path),
        "response_template_csv": str(template_csv),
        "category_counts": dict(sorted(category_counts.items())),
    }


def validate_text_ai_review_response(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("Text AI response row must be a JSON object")
    forbidden = sorted(TEXT_AI_FORBIDDEN_RESPONSE_KEYS.intersection(row))
    if forbidden:
        raise ValueError(f"Text AI response contains forbidden field(s): {forbidden}")

    candidate_id = _clean_text(row.get("candidate_id"))
    if not candidate_id:
        raise ValueError("candidate_id is required")

    raw_is_text = row.get("is_text", False)
    if not isinstance(raw_is_text, bool):
        raise ValueError("is_text must be a boolean")

    raw_reject = row.get("reject_as_noise", False)
    if not isinstance(raw_reject, bool):
        raise ValueError("reject_as_noise must be a boolean")

    text_role = _clean_text(row.get("text_role")) or "uncertain_text"
    if text_role not in TEXT_AI_ALLOWED_ROLES:
        raise ValueError(f"text_role must be one of {TEXT_AI_ALLOWED_ROLES}")

    raw_confidence = row.get("confidence")
    if isinstance(raw_confidence, bool) or raw_confidence is None:
        raise ValueError("confidence must be a number from 0 to 1")
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be a number from 0 to 1") from exc
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("confidence must be a number from 0 to 1")

    return {
        "candidate_id": candidate_id,
        "is_text": raw_is_text,
        "reject_as_noise": raw_reject,
        "suggested_text": _clean_text(row.get("suggested_text")),
        "text_role": text_role,
        "confidence": confidence,
        "review_note": _clean_text(row.get("review_note")),
    }


def load_text_ai_review_responses(path: Path) -> dict[str, dict[str, Any]]:
    responses: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON on line {line_number}") from exc
        try:
            response = validate_text_ai_review_response(row)
        except ValueError as exc:
            raise ValueError(f"{path}: invalid text AI response on line {line_number}: {exc}") from exc
        candidate_id = response["candidate_id"]
        if candidate_id in responses:
            raise ValueError(f"{path}: duplicate candidate_id {candidate_id!r} on line {line_number}")
        responses[candidate_id] = response
    return responses


def _parse_bool_cell(value: object, *, default: bool) -> bool:
    text = _clean_text(value).lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "是", "对"}:
        return True
    if text in {"0", "false", "no", "n", "否", "不"}:
        return False
    raise ValueError(f"boolean cell must be true/false-like, got {value!r}")


def _confidence_cell(value: object, *, default: float = 1.0) -> float:
    text = _clean_text(value)
    if not text:
        return default
    confidence = _safe_float(text, default)
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("confidence must be a number from 0 to 1")
    return confidence


def _row_is_blank(row: dict[str, Any]) -> bool:
    return all(not _clean_text(value) for value in row.values())


def load_text_ai_response_template(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    responses: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "input_rows": 0,
        "skipped_blank_rows": 0,
        "response_count": 0,
        "suggested_text_count": 0,
        "blank_unreadable_count": 0,
        "source_column_counts": {"suggested_text": 0, "review_note": 0, "blank": 0},
    }
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        for line_number, row in enumerate(csv.DictReader(file), start=2):
            report["input_rows"] += 1
            if _row_is_blank(row):
                report["skipped_blank_rows"] += 1
                continue

            candidate_id = _clean_text(row.get("candidate_id"))
            if not candidate_id:
                raise ValueError(f"{path}: candidate_id is required on line {line_number}")

            suggested_text = _clean_text(row.get("suggested_text"))
            review_note = _clean_text(row.get("review_note"))
            source_column = "suggested_text" if suggested_text else "review_note" if review_note else "blank"
            correction_text = suggested_text or review_note
            text_role = _clean_text(row.get("text_role")) or _clean_text(row.get("category")) or "uncertain_text"
            confidence = _confidence_cell(row.get("confidence"), default=1.0)
            if correction_text:
                response = {
                    "candidate_id": candidate_id,
                    "is_text": _parse_bool_cell(row.get("is_text"), default=True),
                    "reject_as_noise": _parse_bool_cell(row.get("reject_as_noise"), default=False),
                    "suggested_text": correction_text,
                    "text_role": text_role,
                    "confidence": confidence,
                    "review_note": review_note if suggested_text else "manual_csv_correction_from_review_note",
                }
                report["suggested_text_count"] += 1
                report["source_column_counts"][source_column] += 1
            else:
                response = {
                    "candidate_id": candidate_id,
                    "is_text": _parse_bool_cell(row.get("is_text"), default=False),
                    "reject_as_noise": _parse_bool_cell(row.get("reject_as_noise"), default=False),
                    "suggested_text": "",
                    "text_role": text_role,
                    "confidence": confidence,
                    "review_note": "manual_blank_unreadable_or_out_of_scope",
                }
                report["blank_unreadable_count"] += 1
                report["source_column_counts"]["blank"] += 1
            responses.append(validate_text_ai_review_response(response))
    report["response_count"] = len(responses)
    return responses, report


def write_text_ai_responses_jsonl(responses: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [validate_text_ai_review_response(response) for response in responses]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def apply_text_ai_review_responses(
    rows: list[dict[str, Any]],
    responses: dict[str, dict[str, Any]],
    *,
    min_confidence: float = 0.7,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(min_confidence, bool) or min_confidence is None:
        raise ValueError("min_confidence must be a number from 0 to 1")
    try:
        confidence_threshold = float(min_confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_confidence must be a number from 0 to 1") from exc
    if confidence_threshold < 0.0 or confidence_threshold > 1.0:
        raise ValueError("min_confidence must be a number from 0 to 1")

    validated_responses: dict[str, dict[str, Any]] = {}
    for candidate_id, response in responses.items():
        validated = validate_text_ai_review_response(response)
        if str(candidate_id) != validated["candidate_id"]:
            raise ValueError(
                f"response key {candidate_id!r} does not match candidate_id {validated['candidate_id']!r}"
            )
        validated_responses[validated["candidate_id"]] = validated

    updated = [dict(row) for row in rows]
    report: dict[str, Any] = {
        "row_count": len(updated),
        "response_count": len(validated_responses),
        "matched": 0,
        "suggested_text": 0,
        "reject_recommended": 0,
        "low_confidence": 0,
        "unmatched_response_ids": [],
    }
    matched_ids: set[str] = set()

    for index, row in enumerate(updated, start=1):
        candidate_id = _candidate_id(row, index)
        response = validated_responses.get(candidate_id)
        if response is None:
            continue

        matched_ids.add(candidate_id)
        report["matched"] += 1
        row["ai_text_suggestion"] = response["suggested_text"]
        row["ai_text_role"] = response["text_role"]
        row["ai_text_confidence"] = response["confidence"]
        row["ai_is_text"] = response["is_text"]
        row["ai_reject_as_noise"] = response["reject_as_noise"]
        row["ai_review_note"] = response["review_note"]
        row["checked"] = "no"
        row.setdefault("output_text", "")

        if response["confidence"] < confidence_threshold:
            row["ai_used"] = False
            row["import_status"] = "ai_low_confidence_review_required"
            report["low_confidence"] += 1
            continue

        row["ai_used"] = True
        if response["reject_as_noise"] or not response["is_text"]:
            row["import_status"] = "ai_reject_recommended_review_required"
            report["reject_recommended"] += 1
        elif response["suggested_text"]:
            row["import_status"] = "ai_suggested_review_required"
            report["suggested_text"] += 1
        else:
            row["import_status"] = "ai_review_required"

    report["unmatched_response_ids"] = sorted(set(validated_responses) - matched_ids)
    return updated, report
