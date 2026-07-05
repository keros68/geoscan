from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

ALLOWED_OBJECT_CLASSES = {
    "map_frame",
    "inner_frame",
    "title_block_border",
    "title_block_split_line",
    "table_grid",
    "text_interference",
    "noise",
    "uncertain",
}
ALLOWED_MISSING_EDGES = {"top", "bottom", "left", "right"}
NON_KEEP_CLASSES = {"noise", "text_interference"}
REVIEW_ONLY_CLASSES = {"uncertain"}
STRUCTURE_CLASSES = ALLOWED_OBJECT_CLASSES - NON_KEEP_CLASSES - REVIEW_ONLY_CLASSES


def _feature_id(feature: dict[str, Any], index: int) -> str:
    props = feature.get("properties") or {}
    return str(props.get("id") or props.get("feature") or props.get("Feature") or f"feature_{index:04d}")


def _bbox(feature: dict[str, Any], pad: float = 10.0) -> list[float]:
    coordinates = (feature.get("geometry") or {}).get("coordinates") or []
    points = coordinates if coordinates and isinstance(coordinates[0], list) else []
    xs = [float(point[0]) for point in points]
    ys = [float(point[1]) for point in points]
    if not xs or not ys:
        return []
    return [min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad]


def validate_ai_review_response(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError("AI response row must be a JSON object")

    feature_id = str(row.get("feature_id") or "").strip()
    if not feature_id:
        raise ValueError("feature_id is required")

    object_class = str(row.get("object_class") or "").strip()
    if object_class not in ALLOWED_OBJECT_CLASSES:
        raise ValueError(f"object_class must be one of {sorted(ALLOWED_OBJECT_CLASSES)}")

    raw_confidence = row.get("confidence")
    if isinstance(raw_confidence, bool) or raw_confidence is None:
        raise ValueError("confidence must be a number from 0 to 1")
    try:
        confidence = float(raw_confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be a number from 0 to 1") from exc
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("confidence must be a number from 0 to 1")

    raw_should_close = row.get("should_close", False)
    if raw_should_close is None:
        should_close = False
    elif isinstance(raw_should_close, bool):
        should_close = raw_should_close
    else:
        raise ValueError("should_close must be a boolean")

    raw_missing_edges = row.get("missing_edges", [])
    if raw_missing_edges is None:
        raw_missing_edges = []
    if not isinstance(raw_missing_edges, list):
        raise ValueError("missing_edges must be a list")
    missing_edges: list[str] = []
    for edge in raw_missing_edges:
        if not isinstance(edge, str):
            raise ValueError("missing_edges entries must be strings")
        edge_name = edge.strip()
        if edge_name not in ALLOWED_MISSING_EDGES:
            raise ValueError(f"missing_edges entries must be one of {sorted(ALLOWED_MISSING_EDGES)}")
        missing_edges.append(edge_name)

    raw_duplicate_group = row.get("duplicate_group", "")
    if raw_duplicate_group is None:
        duplicate_group = ""
    elif isinstance(raw_duplicate_group, str):
        duplicate_group = raw_duplicate_group.strip()
    else:
        raise ValueError("duplicate_group must be a string")

    raw_review_note = row.get("review_note", "")
    if raw_review_note is None:
        review_note = ""
    elif isinstance(raw_review_note, str):
        review_note = raw_review_note.strip()
    else:
        raise ValueError("review_note must be a string")

    return {
        "feature_id": feature_id,
        "object_class": object_class,
        "confidence": confidence,
        "should_close": should_close,
        "missing_edges": missing_edges,
        "duplicate_group": duplicate_group,
        "review_note": review_note,
    }


def load_ai_review_responses(path: Path) -> dict[str, dict[str, Any]]:
    responses: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}: invalid JSON on line {line_number}") from exc
        try:
            response = validate_ai_review_response(row)
        except ValueError as exc:
            raise ValueError(f"{path}: invalid AI response on line {line_number}: {exc}") from exc
        feature_id = response["feature_id"]
        if feature_id in responses:
            raise ValueError(f"{path}: duplicate feature_id {feature_id!r} on line {line_number}")
        responses[feature_id] = response
    return responses


def apply_ai_review_responses(
    payload: dict[str, Any],
    responses: dict[str, dict[str, Any]],
    *,
    min_confidence: float = 0.7,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if isinstance(min_confidence, bool) or min_confidence is None:
        raise ValueError("min_confidence must be a number from 0 to 1")
    try:
        confidence_threshold = float(min_confidence)
    except (TypeError, ValueError) as exc:
        raise ValueError("min_confidence must be a number from 0 to 1") from exc
    if confidence_threshold < 0.0 or confidence_threshold > 1.0:
        raise ValueError("min_confidence must be a number from 0 to 1")

    validated_responses: dict[str, dict[str, Any]] = {}
    for feature_id, response in responses.items():
        validated = validate_ai_review_response(response)
        if str(feature_id) != validated["feature_id"]:
            raise ValueError(
                f"response key {feature_id!r} does not match feature_id {validated['feature_id']!r}"
            )
        validated_responses[validated["feature_id"]] = validated

    updated = deepcopy(payload)
    report: dict[str, Any] = {
        "feature_count": len(updated.get("features", [])),
        "response_count": len(validated_responses),
        "matched": 0,
        "structure_accepted": 0,
        "noise_or_text_interference": 0,
        "low_confidence": 0,
        "review_only": 0,
        "unmatched_response_ids": [],
    }
    matched_ids: set[str] = set()

    for index, feature in enumerate(updated.get("features", []), start=1):
        feature_id = _feature_id(feature, index)
        response = validated_responses.get(feature_id)
        if response is None:
            continue

        matched_ids.add(feature_id)
        report["matched"] += 1
        props = feature.get("properties")
        if not isinstance(props, dict):
            props = {}
            feature["properties"] = props
        props["ai_object_class"] = response["object_class"]
        props["ai_confidence"] = response["confidence"]
        props["ai_review_note"] = response["review_note"]
        props["ai_should_close"] = response["should_close"]
        props["ai_missing_edges"] = response["missing_edges"]
        props["ai_duplicate_group"] = response["duplicate_group"]

        if response["confidence"] < confidence_threshold:
            props["needs_review"] = "yes"
            props["ai_used"] = False
            report["low_confidence"] += 1
            continue

        object_class = response["object_class"]
        if object_class in NON_KEEP_CLASSES:
            props["ai_keep"] = "no"
            props["needs_review"] = "yes"
            props["ai_used"] = True
            report["noise_or_text_interference"] += 1
        elif object_class in STRUCTURE_CLASSES:
            props["ai_keep"] = "yes"
            props["needs_review"] = "no"
            props["ai_used"] = True
            report["structure_accepted"] += 1
        else:
            props["needs_review"] = "yes"
            props["ai_used"] = False
            report["review_only"] += 1

    report["unmatched_response_ids"] = sorted(set(validated_responses) - matched_ids)
    return updated, report


def write_ai_review_requests(
    payload: dict[str, Any],
    *,
    output_dir: Path,
    raster_path: Path,
    provider: str = "none",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    requests_path = output_dir / "ai_requests.jsonl"
    responses_path = output_dir / "ai_responses.jsonl"
    rows: list[dict[str, Any]] = []
    for index, feature in enumerate(payload.get("features", []), start=1):
        props = feature.get("properties") or {}
        if str(props.get("needs_review", "no")).lower() != "yes":
            continue
        rows.append(
            {
                "feature_id": _feature_id(feature, index),
                "provider": provider,
                "raster_path": str(raster_path),
                "crop_bbox": _bbox(feature),
                "object_class": str(props.get("ObjectClass") or props.get("object_class") or ""),
                "geometry": feature.get("geometry"),
                "instruction": (
                    "Judge cartographic structure only. Do not output coordinates. "
                    "Do not infer geological meaning. Return constrained JSON."
                ),
                "allowed_outputs": {
                    "object_class": [
                        "map_frame",
                        "inner_frame",
                        "title_block_border",
                        "title_block_split_line",
                        "table_grid",
                        "text_interference",
                        "noise",
                        "uncertain",
                    ],
                    "missing_edges": ["top", "bottom", "left", "right"],
                },
            }
        )
    requests_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    if not responses_path.exists():
        responses_path.write_text("", encoding="utf-8")
    return {
        "provider": provider,
        "request_count": len(rows),
        "requests_path": str(requests_path),
        "responses_path": str(responses_path),
    }
