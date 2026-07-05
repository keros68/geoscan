"""Deterministic centerline-tracing line vectorizer (the "trace" engine).

Pipeline (no AI, no randomness):

1. Binarize dark ink (same ``gray_threshold`` default as the Hough engine).
2. Thick-region shelling: distance transform keeps only pixels within
   ``max_stroke_halfwidth`` of background, so thin strokes are unaffected while
   solid shaded bands lose their interior — filled regions become outline
   loops instead of Hough's line flood or a medial-axis spine.
3. Vectorized Zhang–Suen thinning down to a 1-px skeleton.
4. Skeleton graph tracing: endpoints/junctions are nodes, degree-2 chains are
   walked into pixel paths; pure cycles are traced as loops.
5. Douglas–Peucker simplification, then straight/curve classification by max
   chord deviation. Straight results are 2-point LineStrings (compatible with
   the existing repair stage and native two-point WL writer); curves keep
   their N-point geometry.

All outputs are review candidates (``checked=no``); nothing here interprets
geology.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .candidates import feature
from .raster import image_point_to_map_point


@dataclass(frozen=True)
class TraceConfig:
    gray_threshold: int = 190
    max_stroke_halfwidth: float = 6.0
    simplify_tolerance: float = 1.8
    straight_max_deviation: float = 2.5
    min_trace_length: float = 60.0
    min_bbox_diagonal: float = 40.0


def binarize_dark_ink(rgb: np.ndarray, *, gray_threshold: int = 190) -> np.ndarray:
    """Boolean mask of dark (ink) pixels."""
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    _, dark = cv2.threshold(gray, gray_threshold, 255, cv2.THRESH_BINARY_INV)
    return dark > 0


def shell_thick_regions(mask: np.ndarray, *, max_stroke_halfwidth: float = 6.0) -> np.ndarray:
    """Keep only ink pixels within ``max_stroke_halfwidth`` of background.

    Strokes up to ``2 * max_stroke_halfwidth`` wide survive intact; solid
    fills are hollowed to an outline shell of that thickness.
    """
    ink = mask.astype(np.uint8)
    distance = cv2.distanceTransform(ink, cv2.DIST_L2, 3)
    return (distance > 0) & (distance <= float(max_stroke_halfwidth))


def _neighbor_planes(img: np.ndarray) -> tuple[np.ndarray, ...]:
    padded = np.pad(img, 1, constant_values=False)
    p2 = padded[:-2, 1:-1]  # N
    p3 = padded[:-2, 2:]  # NE
    p4 = padded[1:-1, 2:]  # E
    p5 = padded[2:, 2:]  # SE
    p6 = padded[2:, 1:-1]  # S
    p7 = padded[2:, :-2]  # SW
    p8 = padded[1:-1, :-2]  # W
    p9 = padded[:-2, :-2]  # NW
    return p2, p3, p4, p5, p6, p7, p8, p9


def zhang_suen_thin(mask: np.ndarray, *, max_iterations: int = 64) -> np.ndarray:
    """Vectorized Zhang–Suen thinning to a (mostly) 1-px-wide skeleton."""
    img = mask.astype(bool).copy()
    for _ in range(int(max_iterations)):
        changed = False
        for step in (0, 1):
            p2, p3, p4, p5, p6, p7, p8, p9 = _neighbor_planes(img)
            neighbors = (p2, p3, p4, p5, p6, p7, p8, p9)
            b = np.zeros(img.shape, dtype=np.uint8)
            for plane in neighbors:
                b += plane
            sequence = (p2, p3, p4, p5, p6, p7, p8, p9, p2)
            a = np.zeros(img.shape, dtype=np.uint8)
            for current, nxt in zip(sequence[:-1], sequence[1:]):
                a += (~current) & nxt
            if step == 0:
                cond = ~(p2 & p4 & p6) & ~(p4 & p6 & p8)
            else:
                cond = ~(p2 & p4 & p8) & ~(p2 & p6 & p8)
            remove = img & (b >= 2) & (b <= 6) & (a == 1) & cond
            if remove.any():
                img &= ~remove
                changed = True
        if not changed:
            break
    return img


_OFFSETS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))


def trace_skeleton_paths(skeleton: np.ndarray) -> list[list[tuple[int, int]]]:
    """Trace a 1-px skeleton into pixel paths as (x, y) image coordinates.

    Nodes are pixels with degree != 2 (endpoints, junctions); each degree-2
    chain between nodes becomes one path. Remaining pure cycles are traced as
    closed loops (first point repeated at the end).
    """
    ys, xs = np.nonzero(skeleton)
    pixels = set(zip(ys.tolist(), xs.tolist()))
    if not pixels:
        return []

    def neighbors(pixel: tuple[int, int]) -> list[tuple[int, int]]:
        # 8-connectivity, but prune diagonal shortcuts that are reachable via
        # an orthogonal neighbor: staircase skeletons otherwise form pixel
        # triangles that read as fake junctions and shred curves into chains.
        y, x = pixel
        result = []
        for dy, dx in _OFFSETS:
            candidate = (y + dy, x + dx)
            if candidate not in pixels:
                continue
            if dy != 0 and dx != 0 and ((y, x + dx) in pixels or (y + dy, x) in pixels):
                continue
            result.append(candidate)
        return result

    degree = {pixel: len(neighbors(pixel)) for pixel in pixels}
    nodes = {pixel for pixel, count in degree.items() if count != 2}

    visited_edges: set[frozenset[tuple[int, int]]] = set()
    consumed: set[tuple[int, int]] = set()
    paths: list[list[tuple[int, int]]] = []

    def walk(start: tuple[int, int], first: tuple[int, int]) -> list[tuple[int, int]]:
        path = [start, first]
        visited_edges.add(frozenset((start, first)))
        previous, current = start, first
        while current not in nodes:
            options = [
                candidate
                for candidate in neighbors(current)
                if candidate != previous
                and frozenset((current, candidate)) not in visited_edges
            ]
            if not options:
                break
            nxt = options[0]
            visited_edges.add(frozenset((current, nxt)))
            path.append(nxt)
            previous, current = current, nxt
        return path

    for node in sorted(nodes):
        for neighbor in neighbors(node):
            if frozenset((node, neighbor)) in visited_edges:
                continue
            path = walk(node, neighbor)
            consumed.update(path)
            if len(path) >= 2:
                paths.append(path)

    # Pure cycles: leftover degree-2 pixels not reachable from any node.
    remaining = {
        pixel for pixel in pixels if pixel not in consumed and degree[pixel] == 2
    }
    while remaining:
        start = sorted(remaining)[0]
        loop = [start]
        previous: tuple[int, int] | None = None
        current = start
        while True:
            options = [
                candidate for candidate in neighbors(current) if candidate != previous
            ]
            options = [candidate for candidate in options if candidate in remaining or candidate == start]
            if not options:
                break
            nxt = options[0]
            if nxt == start:
                loop.append(start)
                break
            loop.append(nxt)
            previous, current = current, nxt
        remaining.difference_update(loop)
        if len(loop) >= 4:
            paths.append(loop)

    # Return as (x, y) image coordinates.
    return [[(x, y) for (y, x) in path] for path in paths]


def _path_length(points: list[tuple[float, float]]) -> float:
    return sum(
        math.hypot(x2 - x1, y2 - y1)
        for (x1, y1), (x2, y2) in zip(points[:-1], points[1:])
    )


def _bbox_diagonal(points: list[tuple[float, float]]) -> float:
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def _max_chord_deviation(points: list[tuple[float, float]]) -> float:
    (x1, y1), (x2, y2) = points[0], points[-1]
    chord = math.hypot(x2 - x1, y2 - y1)
    if chord < 1e-9:
        return _bbox_diagonal(points)
    deviations = [
        abs((x2 - x1) * (y1 - y) - (x1 - x) * (y2 - y1)) / chord for x, y in points
    ]
    return max(deviations)


def simplify_path(
    points: list[tuple[int, int]], *, tolerance: float, closed: bool
) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return [(float(x), float(y)) for x, y in points]
    work = points[:-1] if closed and points[0] == points[-1] else points
    contour = np.asarray(work, dtype=np.float32).reshape(-1, 1, 2)
    approx = cv2.approxPolyDP(contour, float(tolerance), closed)
    simplified = [(float(x), float(y)) for x, y in approx.reshape(-1, 2)]
    if closed and len(simplified) >= 3:
        simplified.append(simplified[0])
    if len(simplified) < 2:
        simplified = [
            (float(points[0][0]), float(points[0][1])),
            (float(points[-1][0]), float(points[-1][1])),
        ]
    return simplified


def extract_traced_line_candidates(
    rgb: np.ndarray,
    *,
    config: TraceConfig | None = None,
) -> list[dict]:
    """Trace centerline candidates from an RGB raster. Deterministic, review-only."""
    config = config or TraceConfig()
    height = rgb.shape[0]
    ink = binarize_dark_ink(rgb, gray_threshold=config.gray_threshold)
    shell = shell_thick_regions(ink, max_stroke_halfwidth=config.max_stroke_halfwidth)
    max_iterations = int(math.ceil(config.max_stroke_halfwidth)) + 8
    skeleton = zhang_suen_thin(shell, max_iterations=max_iterations)
    paths = trace_skeleton_paths(skeleton)

    candidates: list[dict] = []
    for path in paths:
        closed = len(path) >= 4 and path[0] == path[-1]
        float_path = [(float(x), float(y)) for x, y in path]
        length = _path_length(float_path)
        if length < config.min_trace_length:
            continue
        if _bbox_diagonal(float_path) < config.min_bbox_diagonal:
            continue
        simplified = simplify_path(
            path, tolerance=config.simplify_tolerance, closed=closed
        )
        deviation = _max_chord_deviation(simplified)
        if closed:
            kind = "loop"
            geometry_points = simplified
        elif deviation <= config.straight_max_deviation:
            kind = "straight"
            geometry_points = [simplified[0], simplified[-1]]
        else:
            kind = "curve"
            geometry_points = simplified
        coordinates = [
            image_point_to_map_point(x, y, height=height) for x, y in geometry_points
        ]
        candidates.append(
            feature(
                geometry={"type": "LineString", "coordinates": coordinates},
                target="WL",
                cad_layer="T04_AUTO_LINE",
                feature_name="auto_traced_line",
                source="auto",
                confidence=min(0.99, max(0.5, length / max(rgb.shape[:2]))),
                note="自动中心线追踪候选；需人工复核是否为真实图线。",
                mapgis_no=10,
                extra={
                    "engine": "trace",
                    "trace_kind": kind,
                    "length_px": round(length, 2),
                    "point_count": len(geometry_points),
                    "max_chord_deviation_px": round(deviation, 2),
                },
            )
        )
    return candidates
