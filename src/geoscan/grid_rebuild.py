from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

from geoscan.table_repair import reconstruct_axis_grid_segments


Point: TypeAlias = tuple[float, float]
Segment: TypeAlias = tuple[Point, Point]


@dataclass(frozen=True)
class AxisGridRebuildConfig:
    roi_bbox: list[float]
    axis_tolerance: float
    target_file: str
    layer: str
    object_class: str
    confidence: float = 0.82


def _line_bbox(feature: dict[str, Any]) -> list[float]:
    coordinates = (feature.get("geometry") or {}).get("coordinates") or []
    xs = [float(point[0]) for point in coordinates]
    ys = [float(point[1]) for point in coordinates]
    return [min(xs), min(ys), max(xs), max(ys)]


def _bbox_intersects(a: list[float], b: list[float]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def filter_line_payload_to_bbox(payload: dict[str, Any], bbox: list[float]) -> dict[str, Any]:
    features = [
        feature
        for feature in payload.get("features", [])
        if (feature.get("geometry") or {}).get("type") == "LineString"
        and _bbox_intersects(_line_bbox(feature), bbox)
    ]
    return {"type": "FeatureCollection", "features": features}


def payload_to_two_point_segments(payload: dict[str, Any]) -> list[Segment]:
    segments: list[Segment] = []
    for feature in payload.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "LineString":
            continue
        coordinates = geometry.get("coordinates") or []
        if len(coordinates) != 2:
            continue
        segments.append(
            (
                (float(coordinates[0][0]), float(coordinates[0][1])),
                (float(coordinates[1][0]), float(coordinates[1][1])),
            )
        )
    return segments


def _segment_bbox(segment: Segment) -> list[float]:
    xs = [segment[0][0], segment[1][0]]
    ys = [segment[0][1], segment[1][1]]
    return [min(xs), min(ys), max(xs), max(ys)]


def _feature_from_segment(segment: Segment, config: AxisGridRebuildConfig, index: int) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {
            "id": f"grid_rebuilt_{index:04d}",
            "Target": "WL",
            "TargetFile": config.target_file,
            "Layer": config.layer,
            "ObjectClass": config.object_class,
            "geometry_role": "axis_aligned_grid_structure",
            "source_evidence": "roi_axis_levels",
            "source_ids": [],
            "repair_method": "grid_rebuilt",
            "confidence": config.confidence,
            "evidence_score": config.confidence,
            "ai_used": False,
            "ai_object_class": "",
            "ai_disagreement": False,
            "needs_review": "yes",
            "checked": "no",
            "source_bbox_px": [],
            "regularized_bbox": _segment_bbox(segment),
            "wl_object_index": index,
        },
        "geometry": {
            "type": "LineString",
            "coordinates": [[segment[0][0], segment[0][1]], [segment[1][0], segment[1][1]]],
        },
    }


def rebuild_axis_grid_payload(
    source_payload: dict[str, Any],
    config: AxisGridRebuildConfig,
) -> tuple[dict[str, Any], dict[str, Any]]:
    roi_payload = filter_line_payload_to_bbox(source_payload, config.roi_bbox)
    roi_segments = payload_to_two_point_segments(roi_payload)
    rebuilt_segments = reconstruct_axis_grid_segments(
        roi_segments,
        axis_tolerance=config.axis_tolerance,
    )
    features = [
        _feature_from_segment(segment, config, index)
        for index, segment in enumerate(rebuilt_segments, start=1)
    ]
    stats = {
        "input_roi_features": len(roi_payload["features"]),
        "input_roi_segments": len(roi_segments),
        "rebuilt_features": len(features),
        "axis_tolerance": config.axis_tolerance,
    }
    return {"type": "FeatureCollection", "features": features}, stats
