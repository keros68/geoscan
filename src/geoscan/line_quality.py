from __future__ import annotations

import math
from collections import Counter
from typing import TypeAlias


Point: TypeAlias = tuple[float, float]
Segment: TypeAlias = tuple[Point, Point]


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _length(segment: Segment) -> float:
    (x1, y1), (x2, y2) = segment
    return math.hypot(x2 - x1, y2 - y1)


def _orientation(segment: Segment, *, axis_tolerance: float) -> str:
    (x1, y1), (x2, y2) = segment
    dx = x2 - x1
    dy = y2 - y1
    if abs(dy) <= axis_tolerance and abs(dx) >= abs(dy):
        return "h"
    if abs(dx) <= axis_tolerance and abs(dy) >= abs(dx):
        return "v"
    return "o"


def _axis_normalized(segment: Segment, *, axis_tolerance: float) -> Segment:
    (x1, y1), (x2, y2) = segment
    orientation = _orientation(segment, axis_tolerance=axis_tolerance)
    if orientation == "h":
        y = _rounded((y1 + y2) / 2.0)
        return ((_rounded(x1), y), (_rounded(x2), y))
    if orientation == "v":
        x = _rounded((x1 + x2) / 2.0)
        return ((x, _rounded(y1)), (x, _rounded(y2)))
    return ((_rounded(x1), _rounded(y1)), (_rounded(x2), _rounded(y2)))


def _between(value: float, a: float, b: float, *, tolerance: float) -> bool:
    return min(a, b) - tolerance <= value <= max(a, b) + tolerance


def _snap_horizontal_endpoint(
    point: Point,
    verticals: list[Segment],
    *,
    snap_tolerance: float,
) -> Point:
    x, y = point
    candidates: list[tuple[float, float]] = []
    for vertical in verticals:
        (vx1, vy1), (_vx2, vy2) = vertical
        distance = abs(vx1 - x)
        if distance <= snap_tolerance and _between(y, vy1, vy2, tolerance=snap_tolerance):
            candidates.append((distance, vx1))
    if not candidates:
        return point
    _distance, snapped_x = min(candidates)
    return (_rounded(snapped_x), _rounded(y))


def _snap_vertical_endpoint(
    point: Point,
    horizontals: list[Segment],
    *,
    snap_tolerance: float,
) -> Point:
    x, y = point
    candidates: list[tuple[float, float]] = []
    for horizontal in horizontals:
        (hx1, hy1), (hx2, _hy2) = horizontal
        distance = abs(hy1 - y)
        if distance <= snap_tolerance and _between(x, hx1, hx2, tolerance=snap_tolerance):
            candidates.append((distance, hy1))
    if not candidates:
        return point
    _distance, snapped_y = min(candidates)
    return (_rounded(x), _rounded(snapped_y))


def snap_axis_intersections_preserving_count(
    segments: list[Segment],
    *,
    axis_tolerance: float,
    snap_tolerance: float,
) -> tuple[list[Segment], dict[str, int]]:
    normalized = [_axis_normalized(segment, axis_tolerance=axis_tolerance) for segment in segments]
    horizontals = [segment for segment in normalized if _orientation(segment, axis_tolerance=axis_tolerance) == "h"]
    verticals = [segment for segment in normalized if _orientation(segment, axis_tolerance=axis_tolerance) == "v"]

    snapped: list[Segment] = []
    changed_endpoints = 0
    changed_segments = 0
    for original, segment in zip(segments, normalized):
        orientation = _orientation(segment, axis_tolerance=axis_tolerance)
        start, end = segment
        if orientation == "h":
            new_start = _snap_horizontal_endpoint(start, verticals, snap_tolerance=snap_tolerance)
            new_end = _snap_horizontal_endpoint(end, verticals, snap_tolerance=snap_tolerance)
        elif orientation == "v":
            new_start = _snap_vertical_endpoint(start, horizontals, snap_tolerance=snap_tolerance)
            new_end = _snap_vertical_endpoint(end, horizontals, snap_tolerance=snap_tolerance)
        else:
            new_start, new_end = start, end

        new_segment = (new_start, new_end)
        changed_endpoints += int(new_start != original[0]) + int(new_end != original[1])
        changed_segments += int(new_segment != original)
        snapped.append(new_segment)

    return snapped, {
        "segment_count": len(segments),
        "changed_segments": changed_segments,
        "changed_endpoints": changed_endpoints,
    }


def line_quality_report(
    segments: list[Segment],
    *,
    short_length: float,
    endpoint_tolerance: float,
) -> dict[str, int]:
    endpoints = [point for segment in segments for point in segment]
    endpoint_groups: list[list[Point]] = []
    for point in endpoints:
        for group in endpoint_groups:
            gx = sum(item[0] for item in group) / len(group)
            gy = sum(item[1] for item in group) / len(group)
            if math.hypot(point[0] - gx, point[1] - gy) <= endpoint_tolerance:
                group.append(point)
                break
        else:
            endpoint_groups.append([point])

    group_sizes = Counter(len(group) for group in endpoint_groups)
    return {
        "segment_count": len(segments),
        "short_line_candidates": sum(1 for segment in segments if _length(segment) < short_length),
        "endpoint_group_count": len(endpoint_groups),
        "open_endpoint_count": group_sizes[1],
        "connected_endpoint_count": sum(size for size in group_sizes if size > 1),
    }
