from __future__ import annotations

from typing import TypeAlias


Point: TypeAlias = tuple[float, float]
Segment: TypeAlias = tuple[Point, Point]
AxisItem: TypeAlias = tuple[str, float, float, float]


def _rounded(value: float) -> float:
    return round(float(value), 6)


def _axis_item(segment: Segment, *, axis_tolerance: float) -> AxisItem | None:
    (x1, y1), (x2, y2) = segment
    dx = x2 - x1
    dy = y2 - y1
    if abs(dy) <= axis_tolerance and abs(dx) >= abs(dy):
        return ("h", (y1 + y2) / 2.0, min(x1, x2), max(x1, x2))
    if abs(dx) <= axis_tolerance and abs(dy) >= abs(dx):
        return ("v", (x1 + x2) / 2.0, min(y1, y2), max(y1, y2))
    return None


def _group_axis(group: list[AxisItem]) -> float:
    return sum(item[1] for item in group) / len(group)


def _cluster_axis_items(items: list[AxisItem], *, axis_tolerance: float) -> list[list[AxisItem]]:
    groups: list[list[AxisItem]] = []
    for item in sorted(items, key=lambda value: (value[0], value[1], value[2], value[3])):
        if (
            not groups
            or item[0] != groups[-1][0][0]
            or abs(item[1] - _group_axis(groups[-1])) > axis_tolerance
        ):
            groups.append([item])
        else:
            groups[-1].append(item)
    return groups


def _segments_from_intervals(
    orientation: str,
    axis: float,
    intervals: list[tuple[float, float]],
) -> list[Segment]:
    result: list[Segment] = []
    for start, end in intervals:
        if orientation == "h":
            result.append(((_rounded(start), _rounded(axis)), (_rounded(end), _rounded(axis))))
        else:
            result.append(((_rounded(axis), _rounded(start)), (_rounded(axis), _rounded(end))))
    return result


def _merge_small_gaps(
    intervals: list[tuple[float, float]], *, small_gap_tolerance: float
) -> list[tuple[float, float]]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged: list[tuple[float, float]] = []
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end + small_gap_tolerance:
            current_end = max(current_end, end)
        else:
            merged.append((current_start, current_end))
            current_start, current_end = start, end
    merged.append((current_start, current_end))
    return merged


def repair_major_axis_segments(
    segments: list[Segment],
    *,
    axis_tolerance: float,
    small_gap_tolerance: float,
    min_major_segments: int,
    min_major_span: float,
    min_major_total_length: float,
) -> list[Segment]:
    """Reconnect long horizontal/vertical cartographic frame lines without touching oblique linework."""
    axis_items: list[AxisItem] = []
    non_axis_segments: list[Segment] = []
    for segment in segments:
        item = _axis_item(segment, axis_tolerance=axis_tolerance)
        if item is None:
            non_axis_segments.append(segment)
        else:
            axis_items.append(item)

    repaired: list[Segment] = list(non_axis_segments)
    for group in _cluster_axis_items(axis_items, axis_tolerance=axis_tolerance):
        orientation = group[0][0]
        axis = _group_axis(group)
        intervals = [(item[2], item[3]) for item in group]
        min_start = min(start for start, _end in intervals)
        max_end = max(end for _start, end in intervals)
        span = max_end - min_start
        total_length = sum(end - start for start, end in intervals)
        is_major_axis = (
            len(group) >= min_major_segments
            and span >= min_major_span
            and total_length >= min_major_total_length
        )
        if is_major_axis:
            repaired.extend(_segments_from_intervals(orientation, axis, [(min_start, max_end)]))
        else:
            repaired.extend(
                _segments_from_intervals(
                    orientation,
                    axis,
                    _merge_small_gaps(intervals, small_gap_tolerance=small_gap_tolerance),
                )
            )
    return repaired


def _coverage_length(
    intervals: list[tuple[float, float]],
    start: float,
    end: float,
    *,
    gap_tolerance: float,
) -> float:
    if end < start:
        start, end = end, start
    covered = 0.0
    for interval_start, interval_end in _merge_small_gaps(
        intervals,
        small_gap_tolerance=gap_tolerance,
    ):
        covered += max(0.0, min(interval_end, end) - max(interval_start, start))
    return covered


def _axis_groups(axis_items: list[AxisItem], *, axis_tolerance: float) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    for group in _cluster_axis_items(axis_items, axis_tolerance=axis_tolerance):
        intervals = [(item[2], item[3]) for item in group]
        groups.append(
            {
                "orientation": group[0][0],
                "axis": _group_axis(group),
                "intervals": intervals,
                "start": min(start for start, _end in intervals),
                "end": max(end for _start, end in intervals),
            }
        )
    return groups


def _best_axis_match(
    groups: list[dict[str, object]],
    *,
    orientation: str,
    axis: float,
    start: float,
    end: float,
    axis_tolerance: float,
    gap_tolerance: float,
) -> tuple[float, float]:
    span = max(abs(end - start), 1e-9)
    best = 0.0
    best_axis = axis
    for group in groups:
        if group["orientation"] != orientation:
            continue
        group_axis = float(group["axis"])
        if abs(group_axis - axis) > axis_tolerance:
            continue
        coverage = _coverage_length(
            list(group["intervals"]),  # type: ignore[arg-type]
            start,
            end,
            gap_tolerance=gap_tolerance,
        )
        coverage_fraction = coverage / span
        if coverage_fraction > best:
            best = coverage_fraction
            best_axis = group_axis
    return best, best_axis


def _dedupe_axis_segments(
    axis_items: list[AxisItem],
    non_axis_segments: list[Segment],
    *,
    axis_tolerance: float,
) -> list[Segment]:
    repaired: list[Segment] = list(non_axis_segments)
    for group in _cluster_axis_items(axis_items, axis_tolerance=axis_tolerance):
        orientation = group[0][0]
        axis = _group_axis(group)
        intervals = _merge_small_gaps(
            [(item[2], item[3]) for item in group],
            small_gap_tolerance=0.0,
        )
        repaired.extend(_segments_from_intervals(orientation, axis, intervals))
    return repaired


def find_axis_aligned_rectangle_closures(
    segments: list[Segment],
    *,
    axis_tolerance: float,
    corner_tolerance: float,
    min_width: float,
    min_height: float,
    min_side_coverage: float,
    max_width: float | None = None,
    max_height: float | None = None,
    min_present_sides: int = 4,
) -> list[dict[str, object]]:
    """Return confirmed rectangle closures without modifying any segment.

    Each closure dict carries the snapped ``bbox`` (x1, y1, x2, y2), the per-side
    evidence ``side_coverages`` (top/bottom/left/right), the four snapped ``sides``
    segments, and ``synthesized_sides`` (sides whose coverage fell below
    ``min_side_coverage``; empty when ``min_present_sides=4``). Shares the exact
    candidate/coverage logic used by :func:`close_axis_aligned_rectangles`.
    """
    if min_present_sides < 3 or min_present_sides > 4:
        raise ValueError("min_present_sides must be 3 or 4")

    axis_items: list[AxisItem] = []
    non_axis_segments: list[Segment] = []
    for segment in segments:
        item = _axis_item(segment, axis_tolerance=axis_tolerance)
        if item is None:
            non_axis_segments.append(segment)
        else:
            axis_items.append(item)

    groups = _axis_groups(axis_items, axis_tolerance=axis_tolerance)
    horizontal_groups = [group for group in groups if group["orientation"] == "h"]
    vertical_groups = [group for group in groups if group["orientation"] == "v"]
    rectangle_candidates: set[tuple[float, float, float, float]] = set()
    rectangle_sides: set[Segment] = set()

    def add_candidate(x1: float, y1: float, x2: float, y2: float) -> None:
        x_min, x_max = sorted((float(x1), float(x2)))
        y_min, y_max = sorted((float(y1), float(y2)))
        width = x_max - x_min
        height = y_max - y_min
        if width < min_width or height < min_height:
            return
        if max_width is not None and width > max_width:
            return
        if max_height is not None and height > max_height:
            return
        rectangle_candidates.add(
            (_rounded(x_min), _rounded(y_min), _rounded(x_max), _rounded(y_max))
        )

    for top_index, first in enumerate(horizontal_groups):
        for second in horizontal_groups[top_index + 1 :]:
            y1 = float(first["axis"])
            y2 = float(second["axis"])
            if abs(y2 - y1) < min_height:
                continue

            if (
                abs(float(first["start"]) - float(second["start"])) <= corner_tolerance
                and abs(float(first["end"]) - float(second["end"])) <= corner_tolerance
            ):
                add_candidate(
                    (float(first["start"]) + float(second["start"])) / 2.0,
                    y1,
                    (float(first["end"]) + float(second["end"])) / 2.0,
                    y2,
                )

            left_axes = [
                float(group["axis"])
                for group in vertical_groups
                if abs(float(group["axis"]) - float(first["start"])) <= corner_tolerance
                or abs(float(group["axis"]) - float(second["start"])) <= corner_tolerance
            ]
            right_axes = [
                float(group["axis"])
                for group in vertical_groups
                if abs(float(group["axis"]) - float(first["end"])) <= corner_tolerance
                or abs(float(group["axis"]) - float(second["end"])) <= corner_tolerance
            ]
            for left_axis in left_axes:
                for right_axis in right_axes:
                    add_candidate(left_axis, y1, right_axis, y2)

    if min_present_sides == 4:
        for left_index, first in enumerate(vertical_groups):
            for second in vertical_groups[left_index + 1 :]:
                x1 = float(first["axis"])
                x2 = float(second["axis"])
                if abs(x2 - x1) < min_width:
                    continue

                if (
                    abs(float(first["start"]) - float(second["start"])) <= corner_tolerance
                    and abs(float(first["end"]) - float(second["end"])) <= corner_tolerance
                ):
                    add_candidate(
                        x1,
                        (float(first["start"]) + float(second["start"])) / 2.0,
                        x2,
                        (float(first["end"]) + float(second["end"])) / 2.0,
                    )

                top_axes = [
                    float(group["axis"])
                    for group in horizontal_groups
                    if abs(float(group["axis"]) - float(first["start"])) <= corner_tolerance
                    or abs(float(group["axis"]) - float(second["start"])) <= corner_tolerance
                ]
                bottom_axes = [
                    float(group["axis"])
                    for group in horizontal_groups
                    if abs(float(group["axis"]) - float(first["end"])) <= corner_tolerance
                    or abs(float(group["axis"]) - float(second["end"])) <= corner_tolerance
                ]
                for top_axis in top_axes:
                    for bottom_axis in bottom_axes:
                        add_candidate(x1, top_axis, x2, bottom_axis)

    closures: list[dict[str, object]] = []
    for x_min, y_min, x_max, y_max in sorted(rectangle_candidates):
        top_coverage, top_axis = _best_axis_match(
            horizontal_groups,
            orientation="h",
            axis=y_min,
            start=x_min,
            end=x_max,
            axis_tolerance=corner_tolerance,
            gap_tolerance=corner_tolerance,
        )
        bottom_coverage, bottom_axis = _best_axis_match(
            horizontal_groups,
            orientation="h",
            axis=y_max,
            start=x_min,
            end=x_max,
            axis_tolerance=corner_tolerance,
            gap_tolerance=corner_tolerance,
        )
        left_coverage, left_axis = _best_axis_match(
            vertical_groups,
            orientation="v",
            axis=x_min,
            start=y_min,
            end=y_max,
            axis_tolerance=corner_tolerance,
            gap_tolerance=corner_tolerance,
        )
        right_coverage, right_axis = _best_axis_match(
            vertical_groups,
            orientation="v",
            axis=x_max,
            start=y_min,
            end=y_max,
            axis_tolerance=corner_tolerance,
            gap_tolerance=corner_tolerance,
        )
        side_coverages = (top_coverage, bottom_coverage, left_coverage, right_coverage)
        if sum(coverage >= min_side_coverage for coverage in side_coverages) < min_present_sides:
            continue

        x_left = left_axis if left_coverage >= min_side_coverage else x_min
        x_right = right_axis if right_coverage >= min_side_coverage else x_max
        y_top = top_axis if top_coverage >= min_side_coverage else y_min
        y_bottom = bottom_axis if bottom_coverage >= min_side_coverage else y_max
        x1, x2 = sorted((_rounded(x_left), _rounded(x_right)))
        y1, y2 = sorted((_rounded(y_top), _rounded(y_bottom)))
        top_side: Segment = ((x1, y1), (x2, y1))
        bottom_side: Segment = ((x1, y2), (x2, y2))
        left_side: Segment = ((x1, y1), (x1, y2))
        right_side: Segment = ((x2, y1), (x2, y2))
        side_map = {
            "top": (top_side, top_coverage),
            "bottom": (bottom_side, bottom_coverage),
            "left": (left_side, left_coverage),
            "right": (right_side, right_coverage),
        }
        closures.append(
            {
                "bbox": (x1, y1, x2, y2),
                "side_coverages": {name: value[1] for name, value in side_map.items()},
                "sides": (top_side, bottom_side, left_side, right_side),
                "synthesized_sides": tuple(
                    name for name, value in side_map.items() if value[1] < min_side_coverage
                ),
            }
        )
    return closures


def close_axis_aligned_rectangles(
    segments: list[Segment],
    *,
    axis_tolerance: float,
    corner_tolerance: float,
    min_width: float,
    min_height: float,
    min_side_coverage: float,
    max_width: float | None = None,
    max_height: float | None = None,
    min_present_sides: int = 4,
) -> list[Segment]:
    """Complete large axis-aligned frame/table rectangles from strong side evidence.

    ``min_present_sides`` defaults to 4 to preserve the conservative historical behavior.
    Set it to 3 for cartographic frame/table passes where three evidenced sides are enough
    to infer the missing side.
    """
    closures = find_axis_aligned_rectangle_closures(
        segments,
        axis_tolerance=axis_tolerance,
        corner_tolerance=corner_tolerance,
        min_width=min_width,
        min_height=min_height,
        min_side_coverage=min_side_coverage,
        max_width=max_width,
        max_height=max_height,
        min_present_sides=min_present_sides,
    )
    rectangle_sides: set[Segment] = set()
    for closure in closures:
        rectangle_sides.update(closure["sides"])  # type: ignore[arg-type]

    if not rectangle_sides:
        return list(segments)

    axis_items: list[AxisItem] = []
    non_axis_segments: list[Segment] = []
    for segment in segments:
        item = _axis_item(segment, axis_tolerance=axis_tolerance)
        if item is None:
            non_axis_segments.append(segment)
        else:
            axis_items.append(item)
    for side in rectangle_sides:
        item = _axis_item(side, axis_tolerance=axis_tolerance)
        if item is not None:
            axis_items.append(item)
    return _dedupe_axis_segments(axis_items, non_axis_segments, axis_tolerance=axis_tolerance)
