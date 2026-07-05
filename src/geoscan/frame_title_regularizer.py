from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from geoscan.candidates import feature_collection
from geoscan.line_repair import (
    close_axis_aligned_rectangles,
    repair_major_axis_segments,
)


Point: TypeAlias = tuple[float, float]
Segment: TypeAlias = tuple[Point, Point]

BLOCKED_OBJECT_CLASSES = {"geologic_line", "fault_line", "ore_body", "geologic_boundary"}


@dataclass(frozen=True)
class RegularizerConfig:
    target_file: str
    layer: str
    object_class: str
    axis_tolerance: float = 1.0
    small_gap_tolerance: float = 5.0
    min_major_segments: int = 2
    min_major_span: float = 80.0
    min_major_total_length: float = 80.0
    corner_tolerance: float = 3.0
    min_width: float = 20.0
    min_height: float = 20.0
    min_side_coverage: float = 0.7
    seed_wl: str = ""
    seed_capacity: int = 0


def _round_point(point: list[float] | tuple[float, float]) -> Point:
    return (round(float(point[0]), 6), round(float(point[1]), 6))


def _ordered_segment(segment: Segment) -> Segment:
    first, second = segment
    return (first, second) if first <= second else (second, first)


def _feature_segment(item: dict[str, Any]) -> Segment | None:
    geometry = item.get("geometry") or {}
    if geometry.get("type") != "LineString":
        return None
    coordinates = geometry.get("coordinates") or []
    if len(coordinates) != 2:
        return None
    return (_round_point(coordinates[0]), _round_point(coordinates[1]))


def _bbox(segment: Segment) -> list[float]:
    xs = [segment[0][0], segment[1][0]]
    ys = [segment[0][1], segment[1][1]]
    return [min(xs), min(ys), max(xs), max(ys)]


def _segments_equal(a: Segment, b: Segment) -> bool:
    return _ordered_segment(a) == _ordered_segment(b)


def _repair_method(segment: Segment, source_segments: list[Segment]) -> str:
    if any(_segments_equal(segment, source) for source in source_segments):
        return "observed_regularized"
    return "inferred_from_rectangular_structure"


def _feature_from_segment(
    segment: Segment,
    *,
    config: RegularizerConfig,
    index: int,
    source_segments: list[Segment],
) -> dict[str, Any]:
    method = _repair_method(segment, source_segments)
    confidence = 0.9 if method == "observed_regularized" else 0.72
    return {
        "type": "Feature",
        "properties": {
            "Target": "WL",
            "TargetFile": config.target_file,
            "Layer": config.layer,
            "ObjectClass": config.object_class,
            "geometry_role": "frame_or_title_structure",
            "source_evidence": "raw_or_baseline_candidate",
            "source_ids": [],
            "repair_method": method,
            "confidence": confidence,
            "evidence_score": confidence,
            "ai_used": False,
            "ai_object_class": "",
            "ai_disagreement": False,
            "needs_review": "no" if method == "observed_regularized" else "yes",
            "checked": "no",
            "source_bbox_px": [],
            "regularized_bbox": _bbox(segment),
            "seed_wl": config.seed_wl,
            "seed_capacity": config.seed_capacity,
            "wl_object_index": index,
        },
        "geometry": {
            "type": "LineString",
            "coordinates": [[segment[0][0], segment[0][1]], [segment[1][0], segment[1][1]]],
        },
    }


def regularize_frame_title_lines(
    payload: dict[str, Any],
    config: RegularizerConfig,
) -> tuple[dict[str, Any], dict[str, int | float | str]]:
    if config.object_class in BLOCKED_OBJECT_CLASSES:
        raise ValueError(f"Refusing to regularize geological object class: {config.object_class}")

    source_segments: list[Segment] = []
    seen: set[Segment] = set()
    duplicate_count = 0
    skipped_non_two_point = 0
    for item in payload.get("features", []):
        segment = _feature_segment(item)
        if segment is None:
            skipped_non_two_point += 1
            continue
        ordered = _ordered_segment(segment)
        if ordered in seen:
            duplicate_count += 1
            continue
        seen.add(ordered)
        source_segments.append(segment)

    repaired = repair_major_axis_segments(
        source_segments,
        axis_tolerance=config.axis_tolerance,
        small_gap_tolerance=config.small_gap_tolerance,
        min_major_segments=config.min_major_segments,
        min_major_span=config.min_major_span,
        min_major_total_length=config.min_major_total_length,
    )
    closed = close_axis_aligned_rectangles(
        repaired,
        axis_tolerance=config.axis_tolerance,
        corner_tolerance=config.corner_tolerance,
        min_width=config.min_width,
        min_height=config.min_height,
        min_side_coverage=config.min_side_coverage,
    )
    final_segments = list(dict.fromkeys(_ordered_segment(segment) for segment in closed))
    features = [
        _feature_from_segment(segment, config=config, index=index + 1, source_segments=source_segments)
        for index, segment in enumerate(final_segments)
    ]
    stats: dict[str, int | float | str] = {
        "target_file": config.target_file,
        "input_features": len(payload.get("features", [])),
        "input_two_point_segments": len(source_segments) + duplicate_count,
        "duplicate_input_segments": duplicate_count,
        "skipped_non_two_point": skipped_non_two_point,
        "after_major_axis_repair": len(repaired),
        "regularized_features": len(features),
        "needs_review_features": sum(1 for item in features if item["properties"]["needs_review"] == "yes"),
    }
    return feature_collection(features), stats
