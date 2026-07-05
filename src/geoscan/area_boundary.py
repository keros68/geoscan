from __future__ import annotations

from typing import Any


def _point(point: list) -> list[float]:
    if len(point) < 2:
        raise ValueError("Boundary points must contain x and y values")
    return [round(float(point[0]), 9), round(float(point[1]), 9)]


def _closed_ring(ring: list) -> list[list[float]]:
    if len(ring) < 3:
        raise ValueError("Polygon boundary rings must contain at least three points")
    result = [_point(point) for point in ring]
    if result[0] != result[-1]:
        result.append(list(result[0]))
    return result


def _polygon_rings(feature: dict[str, Any]) -> list[list[list[float]]]:
    geometry = feature.get("geometry") or {}
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if geometry_type == "Polygon":
        return [_closed_ring(ring) for ring in coordinates]
    if geometry_type == "MultiPolygon":
        return [_closed_ring(ring) for polygon in coordinates for ring in polygon]
    raise ValueError(f"Unsupported WP boundary geometry type: {geometry_type}")


def _ring_segments(ring: list[list[float]]) -> list[list[list[float]]]:
    return [[ring[index], ring[index + 1]] for index in range(len(ring) - 1)]


def closed_boundary_features_from_wp_payload(payload: dict, *, boundary_layer: str) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for index, feature in enumerate(payload.get("features", []), start=1):
        properties = feature.get("properties") or {}
        source_feature = str(properties.get("Feature") or f"wp_feature_{index:04d}")
        source_target_file = str(properties.get("TargetFile") or "")
        for ring_index, ring in enumerate(_polygon_rings(feature), start=1):
            suffix = "" if ring_index == 1 else f"_ring_{ring_index}"
            features.append(
                {
                    "type": "Feature",
                    "properties": {
                        "Layer": boundary_layer,
                        "Target": "WL",
                        "Feature": f"{source_feature}_boundary{suffix}",
                        "SourceFeature": source_feature,
                        "SourceTargetFile": source_target_file,
                        "ObjectClass": "area_boundary_line",
                        "Checked": str(properties.get("Checked") or "no"),
                        "Note": "Derived closed boundary line from WP polygon; use closed WL topology before creating MapGIS area.",
                    },
                    "geometry": {"type": "MultiLineString", "coordinates": [ring]},
                }
            )
    return features


def closed_boundary_segment_features_from_wp_payload(payload: dict, *, boundary_layer: str) -> list[dict[str, Any]]:
    features: list[dict[str, Any]] = []
    for index, feature in enumerate(payload.get("features", []), start=1):
        properties = feature.get("properties") or {}
        source_feature = str(properties.get("Feature") or f"wp_feature_{index:04d}")
        source_target_file = str(properties.get("TargetFile") or "")
        for ring_index, ring in enumerate(_polygon_rings(feature), start=1):
            ring_suffix = "" if ring_index == 1 else f"_ring_{ring_index}"
            for segment_index, segment in enumerate(_ring_segments(ring), start=1):
                features.append(
                    {
                        "type": "Feature",
                        "properties": {
                            "Layer": boundary_layer,
                            "Target": "WL",
                            "Feature": f"{source_feature}_boundary{ring_suffix}_seg_{segment_index:03d}",
                            "SourceFeature": source_feature,
                            "SourceTargetFile": source_target_file,
                            "ObjectClass": "area_boundary_segment_line",
                            "SegmentIndex": segment_index,
                            "Checked": str(properties.get("Checked") or "no"),
                            "Note": "Derived boundary segment line from WP polygon; use as separate MapGIS lines for area topology tests.",
                        },
                        "geometry": {"type": "LineString", "coordinates": segment},
                    }
                )
    return features


def wp_payload_boundary_report(payload: dict) -> dict[str, int]:
    feature_count = 0
    ring_count = 0
    closed_ring_count = 0
    segment_count = 0
    for feature in payload.get("features", []):
        feature_count += 1
        geometry = feature.get("geometry") or {}
        raw_rings: list
        if geometry.get("type") == "Polygon":
            raw_rings = geometry.get("coordinates") or []
        elif geometry.get("type") == "MultiPolygon":
            raw_rings = [ring for polygon in geometry.get("coordinates") or [] for ring in polygon]
        else:
            raise ValueError(f"Unsupported WP boundary geometry type: {geometry.get('type')}")
        for ring in raw_rings:
            ring_count += 1
            closed = bool(ring and _point(ring[0]) == _point(ring[-1]))
            if closed:
                closed_ring_count += 1
            segment_count += max(len(_closed_ring(ring)) - 1, 0)
    return {
        "feature_count": feature_count,
        "ring_count": ring_count,
        "closed_ring_count": closed_ring_count,
        "segment_count": segment_count,
        "open_ring_count": ring_count - closed_ring_count,
    }
