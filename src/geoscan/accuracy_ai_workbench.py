from __future__ import annotations

import csv
from copy import deepcopy
import html
import json
import math
from pathlib import Path
from typing import Any


LINE_AI_ALLOWED_OBJECT_CLASSES = [
    "map_frame",
    "inner_frame",
    "title_block_border",
    "title_block_split_line",
    "table_grid",
    "text_interference",
    "noise",
    "uncertain",
]
LINE_AI_RESPONSE_TEMPLATE_FIELDS = [
    "feature_id",
    "crop_path",
    "current_object_class",
    "review_priority",
    "line_length",
    "object_class",
    "confidence",
    "should_close",
    "missing_edges",
    "duplicate_group",
    "review_note",
]
LINE_AI_FORBIDDEN_OUTPUTS = [
    "coordinates",
    "checked_yes",
    "delete_geometry",
    "geological_interpretation",
]
LINE_AI_FORBIDDEN_RESPONSE_KEYS = {
    "coordinate",
    "coordinates",
    "geometry",
    "x",
    "y",
    "x_px",
    "y_px",
    "checked",
    "checked_yes",
    "delete_geometry",
    "geological_interpretation",
}


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_bool_cell(value: object, *, default: bool = False) -> bool:
    text = _clean_text(value).lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "是", "对", "需要"}:
        return True
    if text in {"0", "false", "no", "n", "否", "不"}:
        return False
    raise ValueError(f"boolean cell must be true/false-like, got {value!r}")


def _split_missing_edges(value: object) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    for separator in ("；", ";", "，", ",", "|"):
        text = text.replace(separator, " ")
    return [item.strip() for item in text.split() if item.strip()]


def _row_is_blank(row: dict[str, Any]) -> bool:
    return all(not _clean_text(value) for value in row.values())


def _feature_id(feature: dict[str, Any], index: int) -> str:
    props = feature.get("properties") or {}
    for key in ("Feature", "feature", "id", "candidate_id"):
        value = _clean_text(props.get(key))
        if value:
            return value
    return f"line_feature_{index:04d}"


def _line_points(feature: dict[str, Any]) -> list[tuple[float, float]]:
    geometry = feature.get("geometry") or {}
    if geometry.get("type") != "LineString":
        return []
    points: list[tuple[float, float]] = []
    for item in geometry.get("coordinates") or []:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        points.append((float(item[0]), float(item[1])))
    return points


def _line_length(points: list[tuple[float, float]]) -> float:
    if len(points) < 2:
        return 0.0
    total = 0.0
    for start, end in zip(points, points[1:]):
        total += math.dist(start, end)
    return round(total, 6)


def _line_bbox(points: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _truthy_review_flag(value: object) -> bool:
    return _clean_text(value).lower() in {"1", "true", "yes", "y", "是", "需要", "review", "review_required"}


def _line_review_score(feature: dict[str, Any], length: float, *, short_length_threshold: float) -> tuple[float, str, list[str]]:
    props = feature.get("properties") or {}
    score = 0.0
    reasons: list[str] = []
    object_class = _clean_text(props.get("ObjectClass") or props.get("object_class")).lower()
    review_status = _clean_text(props.get("ReviewStatus") or props.get("review_status")).lower()

    if _truthy_review_flag(props.get("needs_review")):
        score += 100.0
        reasons.append("needs_review_flag")
    if length > 0 and length <= short_length_threshold:
        score += 80.0
        reasons.append("short_line")
    if any(token in review_status for token in ("gap", "break", "broken", "missing", "candidate")):
        score += 70.0
        reasons.append("review_status_candidate")
    if any(token in object_class for token in ("table", "grid", "title", "frame")):
        score += 30.0
        reasons.append("structure_class_hint")
    if 0 < length <= short_length_threshold * 3:
        score += 20.0
        reasons.append("compact_segment")

    if score >= 120:
        priority = "high"
    elif score >= 70:
        priority = "medium"
    else:
        priority = "low"
    return score, priority, reasons


def rank_line_features_for_ai_review(
    payload: dict[str, Any],
    *,
    max_features: int = 80,
    short_length_threshold: float = 8.0,
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for index, feature in enumerate(payload.get("features", []), start=1):
        points = _line_points(feature)
        if len(points) < 2:
            continue
        length = _line_length(points)
        score, priority, reasons = _line_review_score(
            feature,
            length,
            short_length_threshold=short_length_threshold,
        )
        if score <= 0:
            continue
        props = feature.get("properties") or {}
        ranked.append(
            {
                "feature_id": _feature_id(feature, index),
                "feature_index": index,
                "review_score": round(score, 6),
                "review_priority": priority,
                "review_reasons": reasons,
                "line_length": length,
                "object_class_hint": _clean_text(props.get("ObjectClass") or props.get("object_class")),
                "checked": _clean_text(props.get("Checked") or props.get("checked")),
                "bbox": list(_line_bbox(points)),
                "feature": deepcopy(feature),
            }
        )
    ranked.sort(key=lambda item: (-float(item["review_score"]), float(item["line_length"]), item["feature_id"]))
    return ranked[: max(0, int(max_features))]


def _map_point_to_pixel(
    x: float,
    y: float,
    *,
    image_height: int,
    coordinate_scale: float,
) -> tuple[float, float]:
    if coordinate_scale <= 0:
        raise ValueError("coordinate_scale must be positive")
    return x / coordinate_scale, image_height - (y / coordinate_scale)


def _crop_bbox_pixels(
    bbox: list[float] | tuple[float, float, float, float],
    *,
    image_width: int,
    image_height: int,
    coordinate_scale: float,
    padding_px: int,
    min_crop_px: int,
) -> tuple[int, int, int, int]:
    minx, miny, maxx, maxy = [float(value) for value in bbox]
    px1, py1 = _map_point_to_pixel(minx, miny, image_height=image_height, coordinate_scale=coordinate_scale)
    px2, py2 = _map_point_to_pixel(maxx, maxy, image_height=image_height, coordinate_scale=coordinate_scale)
    left = min(px1, px2)
    right = max(px1, px2)
    top = min(py1, py2)
    bottom = max(py1, py2)

    if right - left < min_crop_px:
        center = (left + right) / 2.0
        left = center - min_crop_px / 2.0
        right = center + min_crop_px / 2.0
    if bottom - top < min_crop_px:
        center = (top + bottom) / 2.0
        top = center - min_crop_px / 2.0
        bottom = center + min_crop_px / 2.0

    pad = max(0, int(padding_px))
    left = max(0, int(math.floor(left - pad)))
    top = max(0, int(math.floor(top - pad)))
    right = min(image_width, int(math.ceil(right + pad)))
    bottom = min(image_height, int(math.ceil(bottom + pad)))
    if right <= left:
        right = min(image_width, left + 1)
    if bottom <= top:
        bottom = min(image_height, top + 1)
    return left, top, right, bottom


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_template(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=LINE_AI_RESPONSE_TEMPLATE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "feature_id": row["feature_id"],
                    "crop_path": row["crop_path"],
                    "current_object_class": row["object_class_hint"],
                    "review_priority": row["review_priority"],
                    "line_length": row["line_length"],
                    "object_class": "",
                    "confidence": "",
                    "should_close": "",
                    "missing_edges": "",
                    "duplicate_group": "",
                    "review_note": "",
                }
            )


def _write_html(path: Path, rows: list[dict[str, Any]]) -> None:
    cards: list[str] = []
    for row in rows:
        crop_path = html.escape(str(row["crop_path"]))
        feature_id = html.escape(str(row["feature_id"]))
        object_class = html.escape(str(row["object_class_hint"]))
        reasons = html.escape("; ".join(row.get("review_reasons") or []))
        cards.append(
            f"""
            <section class="card">
              <h2>{feature_id}</h2>
              <img src="{crop_path}" alt="{feature_id}">
              <p>priority: {html.escape(str(row["review_priority"]))}</p>
              <p>current class: {object_class}</p>
              <p>length: {html.escape(str(row["line_length"]))}</p>
              <p>reasons: {reasons}</p>
            </section>
            """
        )
    path.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Line AI Review Workbench</title>
  <style>
    body {{ font-family: Arial, "Microsoft YaHei", sans-serif; margin: 24px; background: #f8f8f8; color: #222; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }}
    .card {{ background: white; border: 1px solid #ddd; border-radius: 6px; padding: 12px; }}
    .card img {{ max-width: 100%; border: 1px solid #ccc; background: white; }}
    h1 {{ font-size: 22px; }}
    h2 {{ font-size: 15px; word-break: break-all; }}
    p {{ margin: 6px 0; font-size: 13px; }}
  </style>
</head>
<body>
  <h1>Line AI Review Workbench</h1>
  <p>Review only. Do not output coordinates, checked=yes, geometry deletion, or geological interpretation.</p>
  <div class="grid">
    {''.join(cards)}
  </div>
</body>
</html>
""",
        encoding="utf-8",
    )


def write_line_ai_review_workbench(
    payload: dict[str, Any],
    *,
    output_dir: Path,
    raster_path: Path,
    provider: str = "none",
    max_requests: int = 80,
    crop_padding_px: int = 20,
    min_crop_px: int = 48,
    coordinate_scale: float = 25.4 / 300.0,
    short_length_threshold: float = 8.0,
) -> dict[str, Any]:
    from PIL import Image

    output_dir.mkdir(parents=True, exist_ok=True)
    crops_dir = output_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    requests_path = output_dir / "line_ai_requests.jsonl"
    responses_path = output_dir / "line_ai_responses.jsonl"
    template_path = output_dir / "line_ai_response_template.csv"
    html_path = output_dir / "line_ai_review_page.html"

    image = Image.open(raster_path).convert("RGB")
    image_width, image_height = image.size
    ranked = rank_line_features_for_ai_review(
        payload,
        max_features=max_requests,
        short_length_threshold=short_length_threshold,
    )

    request_rows: list[dict[str, Any]] = []
    template_rows: list[dict[str, Any]] = []
    for item in ranked:
        crop_name = f"{item['feature_id']}.jpg"
        crop_path = crops_dir / crop_name
        crop_bbox = _crop_bbox_pixels(
            item["bbox"],
            image_width=image_width,
            image_height=image_height,
            coordinate_scale=coordinate_scale,
            padding_px=crop_padding_px,
            min_crop_px=min_crop_px,
        )
        image.crop(crop_bbox).save(crop_path, quality=92)
        request_row = {
            "feature_id": item["feature_id"],
            "provider": provider,
            "raster_path": str(raster_path),
            "crop_path": str(crop_path),
            "crop_bbox_pixels": list(crop_bbox),
            "object_class_hint": item["object_class_hint"],
            "checked": item["checked"],
            "review_priority": item["review_priority"],
            "review_reasons": item["review_reasons"],
            "line_length": item["line_length"],
            "instruction": (
                "Judge cartographic structure only from this crop and metadata. "
                "Return constrained JSON/CSV fields. Do not output coordinates, do not delete geometry, "
                "do not mark checked=yes, and do not infer geological meaning."
            ),
            "allowed_outputs": {
                "object_class": LINE_AI_ALLOWED_OBJECT_CLASSES,
                "confidence": "number from 0 to 1",
                "should_close": "boolean",
                "missing_edges": ["top", "bottom", "left", "right"],
                "duplicate_group": "string",
                "review_note": "short evidence note",
                "must_not_output": LINE_AI_FORBIDDEN_OUTPUTS,
            },
        }
        request_rows.append(request_row)
        template_rows.append({**item, "crop_path": str(crop_path)})

    _write_jsonl(requests_path, request_rows)
    if not responses_path.exists():
        responses_path.write_text("", encoding="utf-8")
    _write_template(template_path, template_rows)
    _write_html(html_path, template_rows)

    return {
        "provider": provider,
        "request_count": len(request_rows),
        "requests_path": str(requests_path),
        "responses_path": str(responses_path),
        "response_template_csv": str(template_path),
        "review_html": str(html_path),
        "crops_dir": str(crops_dir),
        "ranked_feature_count": len(ranked),
        "ai_provider_called": provider != "none",
        "coordinate_scale": coordinate_scale,
        "geometry_modified": False,
        "checked_yes_written": False,
    }


def build_accuracy_workbench_manifest(
    *,
    output_root: str | Path,
    line_report: dict[str, Any],
    text_report: dict[str, Any],
    source_paths: dict[str, Any],
) -> dict[str, Any]:
    return {
        "phase": "phase8_ai_accuracy_workbench",
        "purpose": "AI-assisted review workbench for OCR and structural-line accuracy; review-only by design.",
        "output_root": str(output_root),
        "source_paths": source_paths,
        "line_request_count": int(line_report.get("request_count") or 0),
        "text_request_count": int(text_report.get("request_count") or 0),
        "outputs": {
            "line_ai_review": line_report,
            "text_ai_review": text_report,
        },
        "ai_provider_called": bool(line_report.get("ai_provider_called") or text_report.get("ai_provider_called") or False),
        "checked_yes_written": False,
        "current_best_modified": False,
        "geological_content_modified": False,
        "unconfirmed_promoted": False,
        "geometry_modified_by_ai": False,
        "boundary": {
            "ai_role": "review_suggestion_only",
            "forbidden_ai_outputs": LINE_AI_FORBIDDEN_OUTPUTS,
            "final_coordinates_from": "rule_pipeline_or_human_review_not_ai",
        },
    }


def _response_fields_blank(row: dict[str, Any]) -> bool:
    return all(
        not _clean_text(row.get(field))
        for field in (
            "object_class",
            "confidence",
            "should_close",
            "missing_edges",
            "duplicate_group",
            "review_note",
        )
    )


def _validate_no_forbidden_response_keys(row: dict[str, Any]) -> None:
    forbidden = sorted(
        key
        for key, value in row.items()
        if key in LINE_AI_FORBIDDEN_RESPONSE_KEYS and _clean_text(value)
    )
    if forbidden:
        raise ValueError(f"Line AI response contains forbidden field(s): {forbidden}")


def load_line_ai_response_template(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from geoscan.ai_visual_judge import validate_ai_review_response

    responses: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "input_rows": 0,
        "skipped_blank_rows": 0,
        "response_count": 0,
        "object_class_counts": {},
    }
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        for line_number, row in enumerate(csv.DictReader(file), start=2):
            report["input_rows"] += 1
            if _row_is_blank(row) or _response_fields_blank(row):
                report["skipped_blank_rows"] += 1
                continue
            _validate_no_forbidden_response_keys(row)
            feature_id = _clean_text(row.get("feature_id"))
            if not feature_id:
                raise ValueError(f"{path}: feature_id is required on line {line_number}")
            response = validate_ai_review_response(
                {
                    "feature_id": feature_id,
                    "object_class": _clean_text(row.get("object_class")),
                    "confidence": _safe_float(row.get("confidence"), -1.0),
                    "should_close": _parse_bool_cell(row.get("should_close"), default=False),
                    "missing_edges": _split_missing_edges(row.get("missing_edges")),
                    "duplicate_group": _clean_text(row.get("duplicate_group")),
                    "review_note": _clean_text(row.get("review_note")),
                }
            )
            responses.append(response)
            class_counts = report["object_class_counts"]
            object_class = response["object_class"]
            class_counts[object_class] = class_counts.get(object_class, 0) + 1
    report["response_count"] = len(responses)
    report["object_class_counts"] = dict(sorted(report["object_class_counts"].items()))
    return responses, report


def write_line_ai_responses_jsonl(responses: list[dict[str, Any]], path: Path) -> None:
    from geoscan.ai_visual_judge import validate_ai_review_response

    rows: list[dict[str, Any]] = []
    for response in responses:
        _validate_no_forbidden_response_keys(response)
        rows.append(validate_ai_review_response(response))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def apply_line_ai_response_template(
    payload: dict[str, Any],
    template_path: Path,
    *,
    min_confidence: float = 0.7,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from geoscan.ai_visual_judge import apply_ai_review_responses

    responses, template_report = load_line_ai_response_template(template_path)
    response_map = {response["feature_id"]: response for response in responses}
    updated, report = apply_ai_review_responses(payload, response_map, min_confidence=min_confidence)
    report["template_report"] = template_report
    report["checked_yes_written"] = False
    report["geometry_modified_by_ai"] = False
    return updated, report


def load_line_ai_review_requests(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON on line {line_number}") from exc
        feature_id = _clean_text(row.get("feature_id"))
        crop_path = _clean_text(row.get("crop_path"))
        if not feature_id:
            raise ValueError(f"{path}: feature_id is required on line {line_number}")
        if not crop_path:
            raise ValueError(f"{path}: crop_path is required on line {line_number}")
        rows.append(row)
    return rows


def write_line_ai_contact_sheets(
    requests: list[dict[str, Any]],
    *,
    output_dir: Path,
    per_sheet: int = 20,
    columns: int = 4,
    thumb_size: tuple[int, int] = (260, 130),
) -> dict[str, Any]:
    from PIL import Image, ImageDraw, ImageFont

    output_dir.mkdir(parents=True, exist_ok=True)
    rows_per_sheet = max(1, math.ceil(max(1, per_sheet) / max(1, columns)))
    columns = max(1, int(columns))
    per_sheet = max(1, int(per_sheet))
    cell_w = int(thumb_size[0]) + 24
    cell_h = int(thumb_size[1]) + 64
    sheet_paths: list[str] = []
    font = ImageFont.load_default()

    for sheet_index, start in enumerate(range(0, len(requests), per_sheet), start=1):
        chunk = requests[start : start + per_sheet]
        row_count = max(1, math.ceil(len(chunk) / columns))
        canvas = Image.new("RGB", (cell_w * columns, cell_h * row_count), "white")
        draw = ImageDraw.Draw(canvas)
        for offset, request in enumerate(chunk):
            col = offset % columns
            row = offset // columns
            left = col * cell_w + 12
            top = row * cell_h + 12
            crop = Image.open(request["crop_path"]).convert("RGB")
            crop.thumbnail(thumb_size)
            frame = Image.new("RGB", thumb_size, "white")
            frame.paste(crop, ((thumb_size[0] - crop.width) // 2, (thumb_size[1] - crop.height) // 2))
            canvas.paste(frame, (left, top + 34))
            draw.rectangle([left, top + 34, left + thumb_size[0], top + 34 + thumb_size[1]], outline=(180, 180, 180))
            label = f"{start + offset + 1:02d} {request['feature_id']}"
            draw.text((left, top), label[:42], fill=(0, 0, 0), font=font)
            detail = f"{request.get('review_priority','')} {request.get('object_class_hint','')} L={request.get('line_length','')}"
            draw.text((left, top + 16), detail[:42], fill=(80, 80, 80), font=font)
        sheet_path = output_dir / f"line_ai_contact_sheet_{sheet_index:03d}.jpg"
        canvas.save(sheet_path, quality=92)
        sheet_paths.append(str(sheet_path))

    prompt_path = output_dir / "line_ai_contact_sheet_prompt.md"
    index_path = output_dir / "line_ai_contact_sheet_index.json"
    prompt_path.write_text(
        "\n".join(
            [
                "# Line AI Contact Sheet Prompt",
                "",
                "Judge each crop as cartographic structure only.",
                "",
                "Allowed object_class values:",
                ", ".join(LINE_AI_ALLOWED_OBJECT_CLASSES),
                "",
                "Forbidden outputs:",
                ", ".join(LINE_AI_FORBIDDEN_OUTPUTS),
                "",
                "Return CSV/JSON rows with: feature_id, object_class, confidence, should_close, missing_edges, duplicate_group, review_note.",
                "Do not output coordinates. Do not infer geological meaning. Do not mark checked=yes.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    index_path.write_text(
        json.dumps(
            {
                "request_count": len(requests),
                "sheet_count": len(sheet_paths),
                "sheets": sheet_paths,
                "requests": [
                    {
                        "feature_id": request.get("feature_id"),
                        "crop_path": request.get("crop_path"),
                        "object_class_hint": request.get("object_class_hint"),
                        "review_priority": request.get("review_priority"),
                        "line_length": request.get("line_length"),
                    }
                    for request in requests
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "request_count": len(requests),
        "sheet_count": len(sheet_paths),
        "sheets": sheet_paths,
        "prompt_path": str(prompt_path),
        "index_path": str(index_path),
        "rows_per_sheet": rows_per_sheet,
    }


def _max_run_ratio(mask: Any, *, axis: int) -> float:
    import numpy as np

    if mask.size == 0:
        return 0.0
    if axis == 1:
        lines = mask
        denom = mask.shape[1]
    else:
        lines = mask.T
        denom = mask.shape[0]
    max_run = 0
    for line in lines:
        padded = np.concatenate(([False], line.astype(bool), [False]))
        changes = np.flatnonzero(padded[1:] != padded[:-1])
        if changes.size >= 2:
            lengths = changes[1::2] - changes[::2]
            if lengths.size:
                max_run = max(max_run, int(lengths.max()))
    return float(max_run) / float(max(1, denom))


def _connected_component_count(mask: Any) -> int:
    import numpy as np

    height, width = mask.shape
    visited = np.zeros(mask.shape, dtype=bool)
    count = 0
    for y in range(height):
        for x in range(width):
            if visited[y, x] or not mask[y, x]:
                continue
            stack = [(x, y)]
            visited[y, x] = True
            area = 0
            while stack:
                cx, cy = stack.pop()
                area += 1
                for nx in (cx - 1, cx, cx + 1):
                    for ny in (cy - 1, cy, cy + 1):
                        if nx < 0 or ny < 0 or nx >= width or ny >= height:
                            continue
                        if visited[ny, nx] or not mask[ny, nx]:
                            continue
                        visited[ny, nx] = True
                        stack.append((nx, ny))
            if area >= 4:
                count += 1
    return count


def _crop_visual_features(crop_path: Path) -> dict[str, Any]:
    import numpy as np
    from PIL import Image

    image = Image.open(crop_path).convert("L")
    if image.width > 220:
        ratio = 220 / float(image.width)
        image = image.resize((220, max(1, int(image.height * ratio))))
    if image.height > 160:
        ratio = 160 / float(image.height)
        image = image.resize((max(1, int(image.width * ratio)), 160))
    arr = np.asarray(image)
    threshold = min(210, max(120, int(np.percentile(arr, 35)) - 8))
    mask = arr < threshold
    dark_ratio = float(mask.mean()) if mask.size else 0.0
    return {
        "width": int(image.width),
        "height": int(image.height),
        "dark_ratio": round(dark_ratio, 6),
        "component_count": _connected_component_count(mask),
        "horizontal_run_ratio": round(_max_run_ratio(mask, axis=1), 6),
        "vertical_run_ratio": round(_max_run_ratio(mask, axis=0), 6),
    }


def classify_line_crop_for_prefill(request: dict[str, Any]) -> dict[str, Any]:
    feature_id = _clean_text(request.get("feature_id"))
    if not feature_id:
        raise ValueError("feature_id is required")
    crop_path = Path(_clean_text(request.get("crop_path")))
    if not crop_path.exists():
        raise FileNotFoundError(crop_path)

    features = _crop_visual_features(crop_path)
    line_length = _safe_float(request.get("line_length"))
    object_hint = _clean_text(request.get("object_class_hint")).lower()
    max_line_ratio = max(features["horizontal_run_ratio"], features["vertical_run_ratio"])
    component_count = int(features["component_count"])
    dark_ratio = float(features["dark_ratio"])

    if dark_ratio < 0.002:
        object_class = "noise"
        confidence = 0.72
        note = "local_visual_prefill: very few dark pixels in crop"
    elif component_count >= 6 and (line_length <= 18 or max_line_ratio < 0.68):
        object_class = "text_interference"
        confidence = min(0.92, 0.72 + component_count / 100.0)
        note = "local_visual_prefill: many compact dark components, likely text strokes"
    elif max_line_ratio >= 0.45:
        object_class = "table_grid" if ("grid" in object_hint or "table" in object_hint or "regularized" in object_hint) else "inner_frame"
        confidence = min(0.94, max(0.74, max_line_ratio))
        note = "local_visual_prefill: dominant straight run in crop"
    elif component_count >= 5:
        object_class = "text_interference"
        confidence = 0.7
        note = "local_visual_prefill: multiple small components without dominant structure line"
    else:
        object_class = "uncertain"
        confidence = 0.55
        note = "local_visual_prefill: insufficient evidence for automatic class"

    return {
        "feature_id": feature_id,
        "object_class": object_class,
        "confidence": round(float(confidence), 3),
        "should_close": False,
        "missing_edges": [],
        "duplicate_group": "",
        "review_note": note,
        "visual_features": features,
    }


def write_line_ai_prefill_template(
    requests: list[dict[str, Any]],
    *,
    output_csv: Path,
    output_jsonl: Path,
) -> dict[str, Any]:
    responses = [classify_line_crop_for_prefill(request) for request in requests]
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=LINE_AI_RESPONSE_TEMPLATE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for request, response in zip(requests, responses):
            writer.writerow(
                {
                    "feature_id": response["feature_id"],
                    "crop_path": request.get("crop_path", ""),
                    "current_object_class": request.get("object_class_hint", ""),
                    "review_priority": request.get("review_priority", ""),
                    "line_length": request.get("line_length", ""),
                    "object_class": response["object_class"],
                    "confidence": response["confidence"],
                    "should_close": "true" if response["should_close"] else "false",
                    "missing_edges": ";".join(response["missing_edges"]),
                    "duplicate_group": response["duplicate_group"],
                    "review_note": response["review_note"],
                }
            )
    write_line_ai_responses_jsonl(responses, output_jsonl)
    counts: dict[str, int] = {}
    for response in responses:
        object_class = response["object_class"]
        counts[object_class] = counts.get(object_class, 0) + 1
    return {
        "response_count": len(responses),
        "output_csv": str(output_csv),
        "output_jsonl": str(output_jsonl),
        "object_class_counts": dict(sorted(counts.items())),
        "prefill_method": "local_visual_heuristic_review_prefill",
        "ai_provider_called": False,
        "checked_yes_written": False,
    }


def _line_class_color(object_class: str, ai_keep: str) -> tuple[int, int, int]:
    object_class = object_class.lower()
    ai_keep = ai_keep.lower()
    if ai_keep == "no" or object_class in {"text_interference", "noise"}:
        return (220, 40, 40)
    if object_class in {"table_grid", "map_frame", "inner_frame", "title_block_border", "title_block_split_line"}:
        return (20, 150, 70)
    if object_class == "uncertain":
        return (40, 100, 220)
    return (130, 130, 130)


def write_line_ai_class_overlay(
    payload: dict[str, Any],
    *,
    raster_path: Path,
    output_path: Path,
    coordinate_scale: float = 25.4 / 300.0,
    max_width: int = 2400,
) -> dict[str, Any]:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.open(raster_path).convert("RGB")
    original_width, original_height = image.size
    display_scale = min(1.0, float(max_width) / float(original_width))
    if display_scale < 1.0:
        image = image.resize((int(original_width * display_scale), int(original_height * display_scale)))
    draw = ImageDraw.Draw(image)
    class_counts: dict[str, int] = {}

    for feature in payload.get("features", []):
        props = feature.get("properties") or {}
        object_class = _clean_text(props.get("ai_object_class")) or _clean_text(props.get("ObjectClass")) or "unreviewed"
        ai_keep = _clean_text(props.get("ai_keep"))
        class_counts[object_class] = class_counts.get(object_class, 0) + 1
        points = _line_points(feature)
        if len(points) < 2:
            continue
        pixel_points: list[tuple[float, float]] = []
        for x, y in points:
            px, py = _map_point_to_pixel(x, y, image_height=original_height, coordinate_scale=coordinate_scale)
            pixel_points.append((px * display_scale, py * display_scale))
        width = 3 if object_class in {"text_interference", "noise"} else 2
        draw.line(pixel_points, fill=_line_class_color(object_class, ai_keep), width=width)

    font = ImageFont.load_default()
    legend = [
        ("green", "structure/table"),
        ("red", "text/noise suggestion"),
        ("blue", "uncertain"),
        ("gray", "unreviewed"),
    ]
    x0, y0 = 12, 12
    for index, (_, label) in enumerate(legend):
        color = [(20, 150, 70), (220, 40, 40), (40, 100, 220), (130, 130, 130)][index]
        y = y0 + index * 18
        draw.rectangle([x0, y, x0 + 12, y + 12], fill=color)
        draw.text((x0 + 18, y - 1), label, fill=(0, 0, 0), font=font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, quality=92)
    return {
        "path": str(output_path),
        "width": image.width,
        "height": image.height,
        "display_scale": display_scale,
        "class_counts": dict(sorted(class_counts.items())),
    }
