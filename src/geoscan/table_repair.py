from __future__ import annotations

import math
from typing import TypeAlias


Point: TypeAlias = tuple[float, float]
Segment: TypeAlias = tuple[Point, Point]


def _segment_length(segment: Segment) -> float:
    (x1, y1), (x2, y2) = segment
    return math.hypot(x2 - x1, y2 - y1)


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _axis_item(segment: Segment, *, axis_tolerance: float) -> tuple[str, float, float, float] | None:
    (x1, y1), (x2, y2) = segment
    dx = x2 - x1
    dy = y2 - y1
    if abs(dy) <= axis_tolerance and abs(dx) >= abs(dy):
        return ("h", (y1 + y2) / 2.0, min(x1, x2), max(x1, x2))
    if abs(dx) <= axis_tolerance and abs(dy) >= abs(dx):
        return ("v", (x1 + x2) / 2.0, min(y1, y2), max(y1, y2))
    return None


def _merge_intervals(
    items: list[tuple[float, float, float]], *, axis_tolerance: float, gap_tolerance: float
) -> list[tuple[float, float, float]]:
    if not items:
        return []
    items = sorted(items, key=lambda item: (item[0], item[1], item[2]))
    groups: list[list[tuple[float, float, float]]] = []
    for item in items:
        axis, start, end = item
        if not groups or abs(axis - _group_axis(groups[-1])) > axis_tolerance:
            groups.append([item])
        else:
            groups[-1].append(item)

    merged: list[tuple[float, float, float]] = []
    for group in groups:
        axis = _group_axis(group)
        intervals = sorted((start, end) for _axis, start, end in group)
        current_start, current_end = intervals[0]
        for start, end in intervals[1:]:
            if start <= current_end + gap_tolerance:
                current_end = max(current_end, end)
            else:
                merged.append((axis, current_start, current_end))
                current_start, current_end = start, end
        merged.append((axis, current_start, current_end))
    return merged


def _group_axis(group: list[tuple[float, float, float]]) -> float:
    return sum(item[0] for item in group) / len(group)


def _cluster_values(values: list[float], *, tolerance: float) -> list[float]:
    if not values:
        return []
    groups: list[list[float]] = []
    for value in sorted(float(item) for item in values):
        if not groups or abs(value - (sum(groups[-1]) / len(groups[-1]))) > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [_rounded(sum(group) / len(group)) for group in groups]


def merge_axis_segments(
    segments: list[Segment],
    *,
    axis_tolerance: float,
    gap_tolerance: float,
) -> list[Segment]:
    horizontal: list[tuple[float, float, float]] = []
    vertical: list[tuple[float, float, float]] = []
    for segment in segments:
        item = _axis_item(segment, axis_tolerance=axis_tolerance)
        if item is None:
            continue
        orientation, axis, start, end = item
        if orientation == "h":
            horizontal.append((axis, start, end))
        else:
            vertical.append((axis, start, end))

    result: list[Segment] = []
    for y, x1, x2 in _merge_intervals(horizontal, axis_tolerance=axis_tolerance, gap_tolerance=gap_tolerance):
        result.append(((_rounded(x1), _rounded(y)), (_rounded(x2), _rounded(y))))
    for x, y1, y2 in _merge_intervals(vertical, axis_tolerance=axis_tolerance, gap_tolerance=gap_tolerance):
        result.append(((_rounded(x), _rounded(y1)), (_rounded(x), _rounded(y2))))
    return result


def _split_segment(segment: Segment) -> tuple[Segment, Segment]:
    (x1, y1), (x2, y2) = segment
    midpoint = ((_rounded((x1 + x2) / 2.0), _rounded((y1 + y2) / 2.0)))
    return (((_rounded(x1), _rounded(y1)), midpoint), (midpoint, ((_rounded(x2), _rounded(y2)))))


def fit_axis_segments_to_count(segments: list[Segment], *, target_count: int) -> list[Segment]:
    if target_count < 0:
        raise ValueError("target_count must be non-negative")
    if len(segments) > target_count:
        raise ValueError("Cannot fit axis segments by removing lines without losing geometry")

    fitted = list(segments)
    while len(fitted) < target_count:
        if not fitted:
            raise ValueError("Cannot split an empty segment list")
        index = max(range(len(fitted)), key=lambda item: _segment_length(fitted[item]))
        first, second = _split_segment(fitted[index])
        fitted[index : index + 1] = [first, second]
    return fitted


def segment_midpoint(segment: Segment) -> Point:
    (x1, y1), (x2, y2) = segment
    return ((_rounded((x1 + x2) / 2.0), _rounded((y1 + y2) / 2.0)))


def segment_midpoint_in_bbox(segment: Segment, bbox: tuple[float, float, float, float]) -> bool:
    x_min, y_min, x_max, y_max = bbox
    x, y = segment_midpoint(segment)
    return x_min <= x <= x_max and y_min <= y <= y_max


def reconstruct_axis_grid_segments(
    segments: list[Segment],
    *,
    axis_tolerance: float,
) -> list[Segment]:
    horizontal_axes: list[float] = []
    vertical_axes: list[float] = []
    for segment in segments:
        item = _axis_item(segment, axis_tolerance=axis_tolerance)
        if item is None:
            continue
        orientation, axis, _start, _end = item
        if orientation == "h":
            horizontal_axes.append(axis)
        else:
            vertical_axes.append(axis)

    y_levels = _cluster_values(horizontal_axes, tolerance=axis_tolerance)
    x_levels = _cluster_values(vertical_axes, tolerance=axis_tolerance)
    if len(x_levels) < 2 or len(y_levels) < 2:
        raise ValueError("Cannot reconstruct a grid without at least two x and y levels")

    x_min, x_max = min(x_levels), max(x_levels)
    y_min, y_max = min(y_levels), max(y_levels)

    result: list[Segment] = []
    for y in y_levels:
        result.append(((_rounded(x_min), _rounded(y)), (_rounded(x_max), _rounded(y))))
    for x in x_levels:
        result.append(((_rounded(x), _rounded(y_min)), (_rounded(x), _rounded(y_max))))
    return result


def hybrid_table_grid_segments(
    segments: list[Segment],
    *,
    focus_bbox: tuple[float, float, float, float],
    axis_tolerance: float,
    lower_y_cutoff: float,
    lower_gap_tolerance: float,
) -> list[Segment]:
    focus_segments = [
        segment for segment in segments if segment_midpoint_in_bbox(segment, focus_bbox)
    ]
    outside_segments = [
        segment for segment in segments if not segment_midpoint_in_bbox(segment, focus_bbox)
    ]
    upper_segments = [
        segment for segment in focus_segments if segment_midpoint(segment)[1] >= lower_y_cutoff
    ]
    lower_segments = [
        segment for segment in focus_segments if segment_midpoint(segment)[1] < lower_y_cutoff
    ]

    repaired: list[Segment] = list(outside_segments)
    if upper_segments:
        repaired.extend(
            reconstruct_axis_grid_segments(
                upper_segments,
                axis_tolerance=axis_tolerance,
            )
        )
    if lower_segments:
        repaired.extend(
            merge_axis_segments(
                lower_segments,
                axis_tolerance=axis_tolerance,
                gap_tolerance=lower_gap_tolerance,
            )
        )
    return repaired


def remove_short_horizontal_segments_in_bbox(
    segments: list[Segment],
    *,
    cleanup_bbox: tuple[float, float, float, float],
    axis_tolerance: float,
    min_length: float,
) -> list[Segment]:
    result: list[Segment] = []
    for segment in segments:
        item = _axis_item(segment, axis_tolerance=axis_tolerance)
        if item is None:
            result.append(segment)
            continue

        orientation, _axis, _start, _end = item
        if (
            orientation == "h"
            and segment_midpoint_in_bbox(segment, cleanup_bbox)
            and _segment_length(segment) < min_length
        ):
            continue
        result.append(segment)
    return result
