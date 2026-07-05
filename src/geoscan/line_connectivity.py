"""统一的"线条连接程度"档位 + 无 AI 的确定性断线桥接。

One user-facing knob ("line connectivity level") maps to every stage that
affects how connected the extracted linework is:

- Hough: ``max_line_gap`` (how large a break HoughLinesP may jump over);
- trace: morphological-close kernel applied to the ink mask before thinning
  (repairs 1–4 px scan breaks);
- repair stage: ``small_gap_tolerance`` for axis-aligned merging;
- AI enhance: ``max_gap_px`` / ``min_dark_coverage`` validator thresholds.

``bridge_line_candidates`` is the deterministic (no-AI) counterpart of the AI
enhance bridge: it exhaustively examines nearby endpoint pairs and connects
two candidates ONLY when the frozen raster shows ink along the whole bridge
(the line visibly exists on the map, extraction just broke it) AND the bridge
continues both lines' directions. Coordinates are never invented: a bridge
reuses the two existing endpoints verbatim. All outputs stay ``checked=no``.

"conservative" reproduces the pre-connectivity behavior exactly (no closing,
no bridging, historical thresholds), so old runs stay byte-comparable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from .candidates import feature


@dataclass(frozen=True)
class ConnectivityProfile:
    name: str
    # Hough engine: maxLineGap passed to HoughLinesP.
    hough_max_line_gap: int
    # Trace engine: MORPH_CLOSE kernel diameter on the ink mask (0 = off).
    trace_close_kernel_px: int
    # Deterministic endpoint bridging (this module).
    bridge_enabled: bool
    bridge_max_gap_px: float
    bridge_min_dark_coverage: float
    # Max angle (degrees) between the bridge and each line's end tangent —
    # rejects sideways jumps between parallel strokes (hatching, table rules).
    bridge_max_angle_deg: float
    # Nearly-closed polyline snap-close: a single open polyline whose ends are
    # within close_max_gap_px AND whose gap is a small fraction of its path
    # length is treated as a ring with a worn corner (legend boxes, closed
    # geological outlines) and gets an additive closing segment. 0 = off.
    close_max_gap_px: float
    close_max_gap_ratio: float
    # Raster-level small-box regularization: legend/title-block boxes whose
    # sides are visibly inked (>=3 of 4 sides) become one clean closed
    # rectangle; the broken stroke fragments inside move to a sidecar file.
    box_regularize: bool
    # Repair stage: axis-aligned small-gap merge tolerance.
    repair_small_gap_tolerance: float
    # AI enhance validator thresholds (only used when --ai-enhance is on).
    ai_max_gap_px: float
    ai_min_dark_coverage: float


CONNECTIVITY_PROFILES: dict[str, ConnectivityProfile] = {
    "conservative": ConnectivityProfile(
        name="conservative",
        hough_max_line_gap=8,
        trace_close_kernel_px=0,
        bridge_enabled=False,
        bridge_max_gap_px=0.0,
        bridge_min_dark_coverage=0.55,
        bridge_max_angle_deg=35.0,
        close_max_gap_px=0.0,
        close_max_gap_ratio=0.2,
        box_regularize=False,
        repair_small_gap_tolerance=16.0,
        ai_max_gap_px=60.0,
        ai_min_dark_coverage=0.55,
    ),
    "standard": ConnectivityProfile(
        name="standard",
        hough_max_line_gap=15,
        trace_close_kernel_px=3,
        bridge_enabled=True,
        bridge_max_gap_px=60.0,
        bridge_min_dark_coverage=0.55,
        bridge_max_angle_deg=35.0,
        close_max_gap_px=12.0,
        close_max_gap_ratio=0.2,
        box_regularize=True,
        repair_small_gap_tolerance=24.0,
        ai_max_gap_px=90.0,
        ai_min_dark_coverage=0.5,
    ),
    "aggressive": ConnectivityProfile(
        name="aggressive",
        hough_max_line_gap=25,
        trace_close_kernel_px=5,
        bridge_enabled=True,
        bridge_max_gap_px=100.0,
        bridge_min_dark_coverage=0.45,
        bridge_max_angle_deg=45.0,
        close_max_gap_px=20.0,
        close_max_gap_ratio=0.25,
        box_regularize=True,
        repair_small_gap_tolerance=32.0,
        ai_max_gap_px=120.0,
        ai_min_dark_coverage=0.45,
    ),
}

VALID_LINE_CONNECT_MODES = set(CONNECTIVITY_PROFILES)
DEFAULT_LINE_CONNECT = "conservative"

# Same ink-evidence sampling constants as the AI enhance validator.
BRIDGE_DARK_THRESHOLD = 140
BRIDGE_SAMPLE_WINDOW_PX = 2


def resolve_connectivity_profile(name: str) -> ConnectivityProfile:
    profile = CONNECTIVITY_PROFILES.get(str(name).strip().lower())
    if profile is None:
        raise ValueError(
            f"line connectivity must be one of {sorted(CONNECTIVITY_PROFILES)}"
        )
    return profile


def apply_connectivity_overrides(
    profile: ConnectivityProfile,
    *,
    bridge_gap_px: float | None = None,
    close_gap_px: float | None = None,
) -> ConnectivityProfile:
    """Per-run numeric overrides on top of a level (GUI/CLI fine-tuning).

    ``None`` keeps the level's value; ``0`` turns the pass off; a positive
    value enables the pass with that gap even on the conservative level.
    """
    if bridge_gap_px is not None:
        gap = max(0.0, float(bridge_gap_px))
        profile = replace(profile, bridge_enabled=gap > 0.0, bridge_max_gap_px=gap)
    if close_gap_px is not None:
        profile = replace(profile, close_max_gap_px=max(0.0, float(close_gap_px)))
    return profile


def dark_coverage_px(
    gray: np.ndarray,
    a: tuple[float, float],
    b: tuple[float, float],
    *,
    dark_threshold: int = BRIDGE_DARK_THRESHOLD,
    window: int = BRIDGE_SAMPLE_WINDOW_PX,
) -> float:
    """Fraction of sample points along a->b (PIXEL coords) with map ink nearby."""
    height, width = gray.shape[:2]
    gap = math.hypot(b[0] - a[0], b[1] - a[1])
    samples = max(int(round(gap)), 8)
    hits = 0
    for index in range(samples + 1):
        t = index / samples
        x = int(round(a[0] + (b[0] - a[0]) * t))
        y = int(round(a[1] + (b[1] - a[1]) * t))
        x0, x1 = max(0, x - window), min(width, x + window + 1)
        y0, y1 = max(0, y - window), min(height, y + window + 1)
        if x0 >= x1 or y0 >= y1:
            continue
        if int(gray[y0:y1, x0:x1].min()) <= dark_threshold:
            hits += 1
    return hits / (samples + 1)


def _angle_between_deg(u: tuple[float, float], v: tuple[float, float]) -> float:
    norm_u = math.hypot(*u)
    norm_v = math.hypot(*v)
    if norm_u < 1e-9 or norm_v < 1e-9:
        return 180.0
    cosine = (u[0] * v[0] + u[1] * v[1]) / (norm_u * norm_v)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


@dataclass(frozen=True)
class _Endpoint:
    feature_index: int
    which: str  # "start" | "end"
    map_point: tuple[float, float]
    pixel: tuple[float, float]
    # Direction of travel INTO this endpoint (pixel coords), i.e. prev -> tip.
    tangent: tuple[float, float]


def _feature_endpoints(
    features: list[dict[str, Any]], *, height: int
) -> list[_Endpoint]:
    endpoints: list[_Endpoint] = []
    for index, item in enumerate(features):
        geometry = item.get("geometry") or {}
        if geometry.get("type") != "LineString":
            continue
        coordinates = geometry.get("coordinates") or []
        if len(coordinates) < 2:
            continue
        if tuple(coordinates[0]) == tuple(coordinates[-1]):
            continue  # closed loop: nothing to bridge
        for which, tip, prev in (
            ("start", coordinates[0], coordinates[1]),
            ("end", coordinates[-1], coordinates[-2]),
        ):
            tip_map = (float(tip[0]), float(tip[1]))
            prev_map = (float(prev[0]), float(prev[1]))
            tip_px = (tip_map[0], float(height) - tip_map[1])
            prev_px = (prev_map[0], float(height) - prev_map[1])
            endpoints.append(
                _Endpoint(
                    feature_index=index,
                    which=which,
                    map_point=tip_map,
                    pixel=tip_px,
                    tangent=(tip_px[0] - prev_px[0], tip_px[1] - prev_px[1]),
                )
            )
    return endpoints


def regularize_small_boxes(
    features: list[dict[str, Any]],
    gray: np.ndarray,
    *,
    profile: ConnectivityProfile,
    cluster_gap_px: float = 12.0,
    min_side_px: float = 20.0,
    max_side_px: float = 220.0,
    min_aspect: float = 0.4,
    max_aspect: float = 2.6,
    wall_band_px: float = 8.0,
    min_wall_fraction: float = 0.85,
    max_interior_dark_fraction: float = 0.75,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Rebuild broken legend/title-block boxes from their stroke fragments.

    Contour-based detection cannot see a BROKEN box (each wall fragment is its
    own contour), so this works from the extracted vectors instead: nearby
    small fragments are clustered, and a cluster whose combined bbox is
    box-shaped AND whose four bbox sides are visibly inked on the raster
    (>=3 sides well covered) becomes one clean closed rectangle. Fragments
    hugging the rectangle walls are superseded (returned separately for a
    sidecar file); interior detail (hatching, dividers) is kept.

    Returns (kept_features, rectangle_features, superseded_features, report).
    """
    report: dict[str, Any] = {
        "enabled": bool(profile.box_regularize),
        "profile": profile.name,
        "fragments": 0,
        "clusters": 0,
        "rectangles": 0,
        "superseded": 0,
    }
    if not profile.box_regularize or not features:
        return features, [], [], report

    height = int(gray.shape[0])

    # Pixel-space bbox per small LineString feature.
    fragment_bboxes: dict[int, tuple[float, float, float, float]] = {}
    for index, item in enumerate(features):
        geometry = item.get("geometry") or {}
        if geometry.get("type") != "LineString":
            continue
        coordinates = geometry.get("coordinates") or []
        if len(coordinates) < 2:
            continue
        xs = [float(point[0]) for point in coordinates]
        ys = [float(height) - float(point[1]) for point in coordinates]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        if bbox[2] - bbox[0] > max_side_px or bbox[3] - bbox[1] > max_side_px:
            continue
        fragment_bboxes[index] = bbox
    report["fragments"] = len(fragment_bboxes)
    if not fragment_bboxes:
        return features, [], [], report

    # Union-find clustering: fragments whose bboxes come within
    # cluster_gap_px of each other belong to the same box hypothesis.
    parent = {index: index for index in fragment_bboxes}

    def _find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def _near(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
        return not (
            a[2] + cluster_gap_px < b[0]
            or b[2] + cluster_gap_px < a[0]
            or a[3] + cluster_gap_px < b[1]
            or b[3] + cluster_gap_px < a[1]
        )

    indices = sorted(fragment_bboxes)
    for pos, i in enumerate(indices):
        for j in indices[pos + 1 :]:
            if _near(fragment_bboxes[i], fragment_bboxes[j]):
                parent[_find(i)] = _find(j)
    clusters: dict[int, list[int]] = {}
    for index in indices:
        clusters.setdefault(_find(index), []).append(index)
    report["clusters"] = len(clusters)

    grid_height, grid_width = gray.shape[:2]

    def _band_dark_fraction(x_lo: float, y_lo: float, x_hi: float, y_hi: float) -> float:
        ax1 = max(0, int(x_lo))
        ay1 = max(0, int(y_lo))
        ax2 = min(grid_width, int(x_hi))
        ay2 = min(grid_height, int(y_hi))
        if ax2 <= ax1 or ay2 <= ay1:
            return 0.0
        band = gray[ay1:ay2, ax1:ax2]
        return float(np.mean(band <= BRIDGE_DARK_THRESHOLD))

    def _exterior_bands(
        x1: float, y1: float, x2: float, y2: float
    ) -> tuple[float, float, float, float]:
        """Ink fraction in 6-px bands sitting 5 px OUTSIDE each side."""
        return (
            _band_dark_fraction(x1, y1 - 11, x2, y1 - 5),
            _band_dark_fraction(x1, y2 + 5, x2, y2 + 11),
            _band_dark_fraction(x1 - 11, y1, x1 - 5, y2),
            _band_dark_fraction(x2 + 5, y1, x2 + 11, y2),
        )

    def _box_gates_pass(
        x1: float, y1: float, x2: float, y2: float
    ) -> tuple[float, float, float, float] | None:
        width, box_height = x2 - x1, y2 - y1
        if not (min_side_px <= width <= max_side_px and min_side_px <= box_height <= max_side_px):
            return None
        aspect = width / max(box_height, 1e-6)
        if aspect < min_aspect or aspect > max_aspect:
            return None
        sides = (
            dark_coverage_px(gray, (x1, y1), (x2, y1), window=3),
            dark_coverage_px(gray, (x1, y2), (x2, y2), window=3),
            dark_coverage_px(gray, (x1, y1), (x1, y2), window=3),
            dark_coverage_px(gray, (x2, y1), (x2, y2), window=3),
        )
        if sum(side >= 0.6 for side in sides) < 3 or min(sides) < 0.3:
            return None
        # A real box has open space just outside its walls; a hypothesis whose
        # "wall" is actually an interior hatch/grid line has heavy ink right
        # outside one side. Crossing leader lines only tint a band slightly.
        exterior = _exterior_bands(x1, y1, x2, y2)
        if max(exterior) > 0.6 or sum(exterior) / 4.0 > 0.3:
            return None
        return sides

    def _expand_to_box(
        x1: float, y1: float, x2: float, y2: float
    ) -> tuple[float, float, float, float, tuple[float, float, float, float]] | None:
        """Snap missing walls outward to nearby strong ink lines.

        A cluster covering only part of a box (a whole wall never made it
        into the vectors) has a flat/narrow bbox that fails the gates above.
        The true walls still exist as ink: probe outward from each side for
        offsets where a full-length line is >=85% covered, then test the few
        resulting rectangle hypotheses with the same gates.
        """
        max_grow = max_side_px

        def _side_candidates(fixed_a: float, fixed_b: float, base: float, sign: float, horizontal: bool) -> list[float]:
            candidates = [base]
            offset = 4.0
            while offset <= max_grow and len(candidates) < 4:
                position = base + sign * offset
                if horizontal:
                    coverage = dark_coverage_px(gray, (fixed_a, position), (fixed_b, position), window=2)
                else:
                    coverage = dark_coverage_px(gray, (position, fixed_a), (position, fixed_b), window=2)
                if coverage >= 0.85 and (len(candidates) == 1 or position * sign > candidates[-1] * sign + 3):
                    candidates.append(position)
                offset += 2.0
            return candidates

        top_options = _side_candidates(x1, x2, y1, -1.0, horizontal=True)
        bottom_options = _side_candidates(x1, x2, y2, 1.0, horizontal=True)
        left_options = _side_candidates(y1, y2, x1, -1.0, horizontal=False)
        right_options = _side_candidates(y1, y2, x2, 1.0, horizontal=False)
        best: tuple[float, float, float, float, tuple[float, float, float, float]] | None = None
        best_growth = float("inf")
        for ty in top_options:
            for by in bottom_options:
                for lx in left_options:
                    for rx in right_options:
                        growth = (y1 - ty) + (by - y2) + (x1 - lx) + (rx - x2)
                        if growth <= 0.0 or growth >= best_growth:
                            continue
                        sides = _box_gates_pass(lx, ty, rx, by)
                        if sides is not None:
                            best = (lx, ty, rx, by, sides)
                            best_growth = growth
        return best

    raw_rectangles: list[dict[str, Any]] = []
    for members in clusters.values():
        boxes = [fragment_bboxes[i] for i in members]
        x1 = min(b[0] for b in boxes)
        y1 = min(b[1] for b in boxes)
        x2 = max(b[2] for b in boxes)
        y2 = max(b[3] for b in boxes)
        expanded = False
        sides = _box_gates_pass(x1, y1, x2, y2)
        if sides is None:
            # Fragments alone may under-span the true box; try snapping to ink.
            grown = _expand_to_box(x1, y1, x2, y2)
            if grown is None:
                continue
            x1, y1, x2, y2, sides = grown
            expanded = True
        width, box_height = x2 - x1, y2 - y1
        # Solid fills are areas, not boxes: reject mostly-dark interiors.
        ix1, ix2 = int(x1 + width * 0.25), int(x2 - width * 0.25)
        iy1, iy2 = int(y1 + box_height * 0.25), int(y2 - box_height * 0.25)
        if ix2 > ix1 and iy2 > iy1:
            interior = gray[max(0, iy1) : iy2, max(0, ix1) : ix2]
            if interior.size and float(np.mean(interior <= BRIDGE_DARK_THRESHOLD)) > max_interior_dark_fraction:
                continue

        raw_rectangles.append(
            {"bbox": (x1, y1, x2, y2), "sides": sides, "members": list(members), "expanded": expanded}
        )

    # Expansion can rebuild the same box from several partial clusters, and a
    # stub cluster can sit inside a rebuilt box: keep only the outermost of
    # any contained pair, folding the inner cluster's members into it so its
    # wall fragments still get superseded.
    def _contains(
        outer: tuple[float, float, float, float],
        inner: tuple[float, float, float, float],
        margin: float = 6.0,
    ) -> bool:
        return (
            outer[0] - margin <= inner[0]
            and outer[1] - margin <= inner[1]
            and outer[2] + margin >= inner[2]
            and outer[3] + margin >= inner[3]
        )

    accepted: list[dict[str, Any]] = []
    for hypothesis in sorted(
        raw_rectangles,
        key=lambda h: (h["bbox"][2] - h["bbox"][0]) * (h["bbox"][3] - h["bbox"][1]),
        reverse=True,
    ):
        container = next(
            (a for a in accepted if _contains(a["bbox"], hypothesis["bbox"])), None
        )
        if container is not None:
            container["members"].extend(hypothesis["members"])
            continue
        accepted.append(hypothesis)

    # A box with an interior hatch line can come out as two OVERLAPPING
    # half-boxes (each snapped to the hatch line as a false wall). Two
    # substantially overlapping rectangles describe the same box: replace
    # them with their union when the union itself passes the gates,
    # otherwise keep the larger. Adjacent (non-overlapping) boxes such as
    # title-block cells are never touched by this.
    def _area(b: tuple[float, float, float, float]) -> float:
        return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])

    def _overlap_area(
        a: tuple[float, float, float, float], b: tuple[float, float, float, float]
    ) -> float:
        return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
            0.0, min(a[3], b[3]) - max(a[1], b[1])
        )

    merged = True
    while merged:
        merged = False
        for i in range(len(accepted)):
            for j in range(i + 1, len(accepted)):
                a, b = accepted[i]["bbox"], accepted[j]["bbox"]
                overlap = _overlap_area(a, b)
                if overlap < 0.15 * min(_area(a), _area(b)):
                    continue
                union = (
                    min(a[0], b[0]),
                    min(a[1], b[1]),
                    max(a[2], b[2]),
                    max(a[3], b[3]),
                )
                union_sides = _box_gates_pass(*union)
                members = accepted[i]["members"] + accepted[j]["members"]
                if union_sides is not None:
                    accepted[i] = {
                        "bbox": union,
                        "sides": union_sides,
                        "members": members,
                        "expanded": True,
                    }
                else:
                    keep = i if _area(a) >= _area(b) else j
                    accepted[keep] = {**accepted[keep], "members": members}
                    if keep != i:
                        accepted[i] = accepted[keep]
                del accepted[j]
                merged = True
                break
            if merged:
                break

    rectangles: list[dict[str, Any]] = []
    superseded_indices: set[int] = set()
    for hypothesis in accepted:
        x1, y1, x2, y2 = hypothesis["bbox"]
        sides = hypothesis["sides"]
        # Supersede only wall-hugging fragments; keep interior detail.
        for member in hypothesis["members"]:
            item = features[member]
            coordinates = (item.get("geometry") or {}).get("coordinates") or []
            near_wall = 0
            for point in coordinates:
                px = float(point[0])
                py = float(height) - float(point[1])
                distance = min(px - x1, x2 - px, py - y1, y2 - py)
                if abs(distance) <= wall_band_px:
                    near_wall += 1
            if coordinates and near_wall / len(coordinates) >= min_wall_fraction:
                superseded_indices.add(member)

        map_y1, map_y2 = float(height) - y2, float(height) - y1
        rectangles.append(
            feature(
                geometry={
                    "type": "LineString",
                    "coordinates": [
                        [x1, map_y1],
                        [x2, map_y1],
                        [x2, map_y2],
                        [x1, map_y2],
                        [x1, map_y1],
                    ],
                },
                target="WL",
                cad_layer="T04_AUTO_LINE",
                feature_name="auto_small_box",
                source="auto_box",
                confidence=round(min(0.99, sum(sides) / 4.0), 4),
                note="图例/图签小框规整候选（四边在栅格上有墨迹证据）；需人工复核。",
                mapgis_no=10,
                extra={
                    "box_bbox_px": [round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                    "box_side_coverage": [round(side, 3) for side in sides],
                    "box_member_count": len(hypothesis["members"]),
                    "box_expanded_from_raster": bool(hypothesis["expanded"]),
                },
            )
        )
    report["rectangles"] = len(rectangles)
    report["superseded"] = len(superseded_indices)

    kept = [item for index, item in enumerate(features) if index not in superseded_indices]
    superseded = [features[index] for index in sorted(superseded_indices)]
    return kept, rectangles, superseded, report


def close_nearly_closed_polylines(
    features: list[dict[str, Any]],
    *,
    profile: ConnectivityProfile,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Snap-close polylines that are one worn corner away from being a ring.

    A single open LineString whose two ends are within ``close_max_gap_px``
    and whose gap is at most ``close_max_gap_ratio`` of its path length is a
    ring in all but a few pixels (legend boxes, closed outlines). An ADDITIVE
    two-point closing segment is emitted; the original polyline is untouched.
    Complementary to ``bridge_line_candidates``, which only joins endpoints of
    DIFFERENT features and refuses sharp turns — ring gaps sit on corners of
    the SAME feature, so neither pass can duplicate the other's segment.
    """
    report: dict[str, Any] = {
        "enabled": profile.close_max_gap_px > 0.0,
        "profile": profile.name,
        "candidates": 0,
        "accepted": 0,
        "rejected_ratio": 0,
    }
    if profile.close_max_gap_px <= 0.0:
        return [], report

    closures: list[dict[str, Any]] = []
    for index, item in enumerate(features):
        geometry = item.get("geometry") or {}
        if geometry.get("type") != "LineString":
            continue
        coordinates = geometry.get("coordinates") or []
        # A 2-point line has no shape to close; ≥3 points can form a ring.
        if len(coordinates) < 3:
            continue
        if tuple(coordinates[0]) == tuple(coordinates[-1]):
            continue  # already closed
        first = (float(coordinates[0][0]), float(coordinates[0][1]))
        last = (float(coordinates[-1][0]), float(coordinates[-1][1]))
        gap = math.hypot(last[0] - first[0], last[1] - first[1])
        if gap <= 0.0 or gap > profile.close_max_gap_px:
            continue
        report["candidates"] += 1
        length = sum(
            math.hypot(x2 - x1, y2 - y1)
            for (x1, y1), (x2, y2) in zip(coordinates[:-1], coordinates[1:])
        )
        if length <= 0.0 or gap > profile.close_max_gap_ratio * length:
            report["rejected_ratio"] += 1
            continue
        report["accepted"] += 1
        closures.append(
            feature(
                geometry={
                    "type": "LineString",
                    "coordinates": [list(last), list(first)],
                },
                target="WL",
                cad_layer="T04_AUTO_LINE",
                feature_name="auto_close_line",
                source="auto_close",
                confidence=0.5,
                note="近闭合折线自动收口段（缺口远小于周长）；需人工复核是否应闭合。",
                mapgis_no=10,
                extra={
                    "close_gap_px": round(gap, 2),
                    "close_path_length_px": round(length, 2),
                    "closes_feature_index": index,
                },
            )
        )
    return closures, report


def bridge_line_candidates(
    features: list[dict[str, Any]],
    gray: np.ndarray,
    *,
    profile: ConnectivityProfile,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Deterministically bridge nearby endpoints backed by raster ink.

    ``features`` hold map coordinates (y flipped: map_y = height - y_px);
    ``gray`` is the frozen raster the candidates were extracted from. Returns
    (new bridge features, report). Input features are never modified.
    """
    report: dict[str, Any] = {
        "enabled": bool(profile.bridge_enabled),
        "profile": profile.name,
        "endpoints": 0,
        "pairs_within_gap": 0,
        "accepted": 0,
        "rejected_angle": 0,
        "rejected_no_ink": 0,
    }
    if not profile.bridge_enabled or not features:
        return [], report

    height = int(gray.shape[0])
    endpoints = _feature_endpoints(features, height=height)
    report["endpoints"] = len(endpoints)
    if len(endpoints) < 2:
        return [], report

    # Spatial hash so large maps stay O(n): only neighboring cells can hold
    # an endpoint within bridge_max_gap_px.
    cell = max(profile.bridge_max_gap_px, 1.0)
    grid: dict[tuple[int, int], list[int]] = {}
    for idx, endpoint in enumerate(endpoints):
        key = (int(endpoint.pixel[0] // cell), int(endpoint.pixel[1] // cell))
        grid.setdefault(key, []).append(idx)

    pairs: list[tuple[float, int, int]] = []
    for (cx, cy), members in grid.items():
        neighbor_indices: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                neighbor_indices.extend(grid.get((cx + dx, cy + dy), ()))
        for i in members:
            a = endpoints[i]
            for j in neighbor_indices:
                if j <= i:
                    continue
                b = endpoints[j]
                if a.feature_index == b.feature_index:
                    continue
                gap = math.hypot(
                    b.pixel[0] - a.pixel[0], b.pixel[1] - a.pixel[1]
                )
                if gap <= 0.0 or gap > profile.bridge_max_gap_px:
                    continue
                pairs.append((gap, i, j))
    pairs.sort(key=lambda row: (row[0], row[1], row[2]))
    report["pairs_within_gap"] = len(pairs)

    used: set[int] = set()
    bridges: list[dict[str, Any]] = []
    for gap, i, j in pairs:
        if i in used or j in used:
            continue
        a, b = endpoints[i], endpoints[j]
        bridge_vec = (b.pixel[0] - a.pixel[0], b.pixel[1] - a.pixel[1])
        # Combined path travels ...prev_a -> a -> b -> prev_b..., so the
        # bridge must continue a's tangent and line b must continue the
        # bridge (b's tangent reversed points from b back into its line).
        angle_a = _angle_between_deg(a.tangent, bridge_vec)
        angle_b = _angle_between_deg(bridge_vec, (-b.tangent[0], -b.tangent[1]))
        if (
            angle_a > profile.bridge_max_angle_deg
            or angle_b > profile.bridge_max_angle_deg
        ):
            report["rejected_angle"] += 1
            continue
        coverage = dark_coverage_px(gray, a.pixel, b.pixel)
        if coverage < profile.bridge_min_dark_coverage:
            report["rejected_no_ink"] += 1
            continue
        used.update((i, j))
        report["accepted"] += 1
        bridges.append(
            feature(
                geometry={
                    "type": "LineString",
                    "coordinates": [list(a.map_point), list(b.map_point)],
                },
                target="WL",
                cad_layer="T04_AUTO_LINE",
                feature_name="auto_bridge_line",
                source="auto_bridge",
                confidence=round(min(0.99, coverage), 4),
                note="确定性桥接段（两端点间沿线有墨迹证据）；需人工复核。",
                mapgis_no=10,
                extra={
                    "bridge_gap_px": round(gap, 2),
                    "bridge_dark_coverage": round(coverage, 3),
                    "bridge_between": [a.feature_index, b.feature_index],
                    "bridge_endpoints": [a.which, b.which],
                },
            )
        )
    return bridges, report
