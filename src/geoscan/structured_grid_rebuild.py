from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias


Point: TypeAlias = tuple[float, float]
Segment: TypeAlias = tuple[Point, Point]
AxisItem: TypeAlias = tuple[str, float, float, float, Segment]
PreferredRegion: TypeAlias = Literal["lower_right", "lower_left", "upper_right", "upper_left", "largest"]


@dataclass(frozen=True)
class GridCandidate:
    bbox: tuple[float, float, float, float]
    horizontal_axes: tuple[float, ...]
    vertical_axes: tuple[float, ...]
    source_segment_count: int
    score: float


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _ordered_segment(segment: Segment) -> Segment:
    start = (_rounded(segment[0][0]), _rounded(segment[0][1]))
    end = (_rounded(segment[1][0]), _rounded(segment[1][1]))
    return (start, end) if start <= end else (end, start)


def _axis_item(segment: Segment, *, axis_tolerance: float) -> AxisItem | None:
    (x1, y1), (x2, y2) = segment
    dx = x2 - x1
    dy = y2 - y1
    if abs(dy) <= axis_tolerance and abs(dx) >= abs(dy):
        axis = (y1 + y2) / 2.0
        normalized = ((_rounded(min(x1, x2)), _rounded(axis)), (_rounded(max(x1, x2)), _rounded(axis)))
        return ("h", axis, min(x1, x2), max(x1, x2), normalized)
    if abs(dx) <= axis_tolerance and abs(dy) >= abs(dx):
        axis = (x1 + x2) / 2.0
        normalized = ((_rounded(axis), _rounded(min(y1, y2))), (_rounded(axis), _rounded(max(y1, y2))))
        return ("v", axis, min(y1, y2), max(y1, y2), normalized)
    return None


def _cluster_values(values: list[float], *, tolerance: float) -> tuple[float, ...]:
    if not values:
        return ()
    groups: list[list[float]] = []
    for value in sorted(float(item) for item in values):
        if not groups or abs(value - (sum(groups[-1]) / len(groups[-1]))) > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return tuple(_rounded(sum(group) / len(group)) for group in groups)


def _item_bbox(item: AxisItem, *, expand: float) -> tuple[float, float, float, float]:
    orientation, axis, start, end, _segment = item
    if orientation == "h":
        return (start - expand, axis - expand, end + expand, axis + expand)
    return (axis - expand, start - expand, axis + expand, end + expand)


def _bbox_intersects(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> bool:
    return not (
        first[2] < second[0]
        or first[0] > second[2]
        or first[3] < second[1]
        or first[1] > second[3]
    )


def _segment_bbox(segment: Segment) -> tuple[float, float, float, float]:
    return (
        min(segment[0][0], segment[1][0]),
        min(segment[0][1], segment[1][1]),
        max(segment[0][0], segment[1][0]),
        max(segment[0][1], segment[1][1]),
    )


def _component_indices(items: list[AxisItem], *, component_gap_tolerance: float) -> list[list[int]]:
    if not items:
        return []
    parents = list(range(len(items)))
    bboxes = [_item_bbox(item, expand=component_gap_tolerance) for item in items]

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for first_index in range(len(items)):
        for second_index in range(first_index + 1, len(items)):
            if _bbox_intersects(bboxes[first_index], bboxes[second_index]):
                union(first_index, second_index)

    grouped: dict[int, list[int]] = {}
    for index in range(len(items)):
        grouped.setdefault(find(index), []).append(index)
    return list(grouped.values())


def find_axis_grid_candidates(
    segments: list[Segment],
    *,
    axis_tolerance: float,
    component_gap_tolerance: float,
    min_horizontal_axes: int,
    min_vertical_axes: int,
    min_width: float,
    min_height: float,
    max_width: float | None = None,
    max_height: float | None = None,
) -> list[GridCandidate]:
    axis_items = [
        item for segment in segments if (item := _axis_item(segment, axis_tolerance=axis_tolerance)) is not None
    ]
    candidates: list[GridCandidate] = []
    for component in _component_indices(axis_items, component_gap_tolerance=component_gap_tolerance):
        component_items = [axis_items[index] for index in component]
        horizontal_axes = _cluster_values(
            [item[1] for item in component_items if item[0] == "h"],
            tolerance=axis_tolerance,
        )
        vertical_axes = _cluster_values(
            [item[1] for item in component_items if item[0] == "v"],
            tolerance=axis_tolerance,
        )
        if len(horizontal_axes) < min_horizontal_axes or len(vertical_axes) < min_vertical_axes:
            continue
        x_min = min(vertical_axes)
        x_max = max(vertical_axes)
        y_min = min(horizontal_axes)
        y_max = max(horizontal_axes)
        width = x_max - x_min
        height = y_max - y_min
        if width < min_width or height < min_height:
            continue
        if max_width is not None and width > max_width:
            continue
        if max_height is not None and height > max_height:
            continue
        grid_slots = max(1, (len(horizontal_axes) - 1) * (len(vertical_axes) - 1))
        score = float(len(horizontal_axes) + len(vertical_axes)) + min(10.0, grid_slots / 4.0)
        candidates.append(
            GridCandidate(
                bbox=(_rounded(x_min), _rounded(y_min), _rounded(x_max), _rounded(y_max)),
                horizontal_axes=horizontal_axes,
                vertical_axes=vertical_axes,
                source_segment_count=len(component_items),
                score=round(score, 6),
            )
        )
    return sorted(candidates, key=lambda item: (-item.score, item.bbox))


def rebuild_grid_candidate_segments(candidate: GridCandidate) -> list[Segment]:
    x_min, y_min, x_max, y_max = candidate.bbox
    segments: list[Segment] = []
    for y in candidate.horizontal_axes:
        segments.append(((_rounded(x_min), _rounded(y)), (_rounded(x_max), _rounded(y))))
    for x in candidate.vertical_axes:
        segments.append(((_rounded(x), _rounded(y_min)), (_rounded(x), _rounded(y_max))))
    return segments


def _candidate_center(candidate: GridCandidate) -> Point:
    x_min, y_min, x_max, y_max = candidate.bbox
    return ((_rounded((x_min + x_max) / 2.0)), (_rounded((y_min + y_max) / 2.0)))


def select_preferred_grid_candidate(
    candidates: list[GridCandidate],
    *,
    page_bbox: tuple[float, float, float, float],
    preferred_region: PreferredRegion,
) -> GridCandidate:
    if not candidates:
        raise ValueError("No grid candidates available")
    if preferred_region == "largest":
        return max(candidates, key=lambda item: (item.score, item.source_segment_count))

    x_min, y_min, x_max, y_max = page_bbox
    page_width = max(x_max - x_min, 1e-9)
    page_height = max(y_max - y_min, 1e-9)

    def region_score(candidate: GridCandidate) -> tuple[float, float]:
        cx, cy = _candidate_center(candidate)
        x_norm = (cx - x_min) / page_width
        y_norm = (cy - y_min) / page_height
        if "right" in preferred_region:
            horizontal = x_norm
        else:
            horizontal = 1.0 - x_norm
        if "lower" in preferred_region:
            vertical = 1.0 - y_norm
        else:
            vertical = y_norm
        return (candidate.score + horizontal * 8.0 + vertical * 8.0, candidate.source_segment_count)

    return max(candidates, key=region_score)


def _expanded_bbox(
    bbox: tuple[float, float, float, float],
    *,
    padding: float,
) -> tuple[float, float, float, float]:
    return (bbox[0] - padding, bbox[1] - padding, bbox[2] + padding, bbox[3] + padding)


def replace_segments_intersecting_bbox(
    base_segments: list[Segment],
    replacement_segments: list[Segment],
    *,
    bbox: tuple[float, float, float, float],
    padding: float = 0.0,
) -> tuple[list[Segment], dict[str, int]]:
    replacement_bbox = _expanded_bbox(bbox, padding=padding)
    preserved: list[Segment] = []
    removed = 0
    for segment in base_segments:
        if _bbox_intersects(_segment_bbox(segment), replacement_bbox):
            removed += 1
        else:
            preserved.append(_ordered_segment(segment))
    merged: list[Segment] = []
    seen: set[Segment] = set()
    for segment in [*preserved, *replacement_segments]:
        ordered = _ordered_segment(segment)
        if ordered in seen:
            continue
        seen.add(ordered)
        merged.append(ordered)
    return merged, {
        "base_segments": len(base_segments),
        "removed_segments": removed,
        "preserved_segments": len(preserved),
        "replacement_segments": len(replacement_segments),
        "merged_segments": len(merged),
    }
