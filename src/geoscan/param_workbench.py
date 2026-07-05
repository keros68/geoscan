"""Line-extraction parameter workbench (analysis-only).

Runs a parameter grid over ``extract_lines.extract_line_candidates`` on the
frozen input raster of an existing program run and writes per-combo candidate
GeoJSON, quality metrics, and overlay images under ``10_PARAM_WORKBENCH``.

Boundaries:

- never writes into ``04_LINE_WORKFLOW`` or any other existing stage folder;
- never changes production defaults (``min_line_length=130`` stays untouched);
- never writes ``checked=yes``;
- metric-based ranking is advisory only — human overlay review decides.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .candidates import write_geojson
from .extract_lines import extract_line_candidates
from .line_repair import (
    find_axis_aligned_rectangle_closures,
    repair_major_axis_segments,
)
from .raster import load_rgb

WORKBENCH_DIR_NAME = "10_PARAM_WORKBENCH"
REPORT_NAME = "PARAM_WORKBENCH_REPORT.json"
SUMMARY_NAME = "PARAM_WORKBENCH_SUMMARY.md"

DEFAULT_MIN_LINE_LENGTHS = (110, 130, 160, 190)
DEFAULT_MAX_LINE_GAPS = (8, 16, 24, 32)
DEFAULT_GRAY_THRESHOLDS = (170, 190, 210)

BASELINE_COMBO = (130, 8, 190, True)

SHORT_FRAGMENT_PX = 180.0
PRECISION_TOLERANCE_PX = 2.0
RECALL_TOLERANCE_PX = 3.0
TEXT_BBOX_PAD_PX = 4

DRY_RUN_AXIS_TOLERANCE = 2.0
DRY_RUN_SMALL_GAP_TOLERANCE = 16.0
DRY_RUN_CORNER_TOLERANCE = 6.0
DRY_RUN_MIN_RECT_SIDE = 40.0
DRY_RUN_MIN_SIDE_COVERAGE = 0.7

PARALLEL_MIN_AXIS_OFFSET = 0.5
PARALLEL_MAX_AXIS_OFFSET = 6.0
PARALLEL_MIN_OVERLAP_FRACTION = 0.5


@dataclass(frozen=True)
class ComboSpec:
    min_line_length: int
    max_line_gap: int
    gray_threshold: int
    use_canny: bool

    @property
    def key(self) -> str:
        mode = "canny" if self.use_canny else "mask"
        return (
            f"mll{self.min_line_length}_gap{self.max_line_gap}"
            f"_thr{self.gray_threshold}_{mode}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_line_length": self.min_line_length,
            "max_line_gap": self.max_line_gap,
            "gray_threshold": self.gray_threshold,
            "use_canny": self.use_canny,
        }


def default_grid() -> list[ComboSpec]:
    combos = [
        ComboSpec(mll, gap, threshold, True)
        for threshold in DEFAULT_GRAY_THRESHOLDS
        for mll in DEFAULT_MIN_LINE_LENGTHS
        for gap in DEFAULT_MAX_LINE_GAPS
    ]
    combos.extend(
        ComboSpec(BASELINE_COMBO[0], BASELINE_COMBO[1], threshold, False)
        for threshold in DEFAULT_GRAY_THRESHOLDS
    )
    return combos


# ---------------------------------------------------------------------------
# geometry helpers
# ---------------------------------------------------------------------------


def feature_image_segments(
    features: list[dict[str, Any]], *, height: int
) -> list[tuple[float, float, float, float]]:
    """Map-coordinate LineStrings -> (x1, y1, x2, y2) in image pixel coordinates."""
    segments: list[tuple[float, float, float, float]] = []
    for item in features:
        geometry = item.get("geometry") or {}
        if geometry.get("type") != "LineString":
            continue
        coordinates = geometry.get("coordinates") or []
        if len(coordinates) != 2:
            continue
        (x1, y1), (x2, y2) = coordinates
        segments.append((float(x1), float(height) - float(y1), float(x2), float(height) - float(y2)))
    return segments


def segment_lengths(segments: list[tuple[float, float, float, float]]) -> list[float]:
    return [
        float(np.hypot(x2 - x1, y2 - y1)) for x1, y1, x2, y2 in segments
    ]


def length_stats(lengths: list[float]) -> dict[str, Any]:
    if not lengths:
        return {"count": 0}
    values = np.asarray(sorted(lengths), dtype=np.float64)
    return {
        "count": int(values.size),
        "min": round(float(values[0]), 1),
        "p10": round(float(np.percentile(values, 10)), 1),
        "p25": round(float(np.percentile(values, 25)), 1),
        "median": round(float(np.percentile(values, 50)), 1),
        "p75": round(float(np.percentile(values, 75)), 1),
        "p90": round(float(np.percentile(values, 90)), 1),
        "max": round(float(values[-1]), 1),
    }


def orientation_bins(segments: list[tuple[float, float, float, float]]) -> dict[str, int]:
    bins = {
        "horizontal_+-5deg": 0,
        "vertical_85_95deg": 0,
        "diag_5_30deg": 0,
        "diag_30_60deg": 0,
        "diag_60_85deg": 0,
        "other": 0,
    }
    for x1, y1, x2, y2 in segments:
        angle = abs(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        if angle > 90.0:
            angle = 180.0 - angle
        if angle <= 5.0:
            bins["horizontal_+-5deg"] += 1
        elif angle >= 85.0:
            bins["vertical_85_95deg"] += 1
        elif angle < 30.0:
            bins["diag_5_30deg"] += 1
        elif angle <= 60.0:
            bins["diag_30_60deg"] += 1
        elif angle < 85.0:
            bins["diag_60_85deg"] += 1
        else:
            bins["other"] += 1
    return bins


def short_fragment_ratio(lengths: list[float], *, threshold_px: float = SHORT_FRAGMENT_PX) -> float:
    if not lengths:
        return 0.0
    short = sum(1 for value in lengths if value < threshold_px)
    return round(short / len(lengths), 4)


def parallel_duplicate_ratio(
    segments: list[tuple[float, float, float, float]],
    *,
    min_axis_offset: float = PARALLEL_MIN_AXIS_OFFSET,
    max_axis_offset: float = PARALLEL_MAX_AXIS_OFFSET,
    min_overlap_fraction: float = PARALLEL_MIN_OVERLAP_FRACTION,
) -> float:
    """Fraction of candidates that have a near-parallel twin within stroke width.

    Measures the Canny double-edge artifact: a thick stroke produces two edges a
    few pixels apart, both detected by Hough. Only near-axis-aligned candidates
    are compared (h vs h, v vs v).
    """
    if not segments:
        return 0.0
    axis_entries: dict[str, list[tuple[int, float, float, float]]] = {"h": [], "v": []}
    for index, (x1, y1, x2, y2) in enumerate(segments):
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        if dx >= dy * 4.0 and dx > 0:
            axis_entries["h"].append((index, (y1 + y2) / 2.0, min(x1, x2), max(x1, x2)))
        elif dy >= dx * 4.0 and dy > 0:
            axis_entries["v"].append((index, (x1 + x2) / 2.0, min(y1, y2), max(y1, y2)))
    duplicates: set[int] = set()
    for entries in axis_entries.values():
        entries.sort(key=lambda item: item[1])
        for i, (index_i, axis_i, start_i, end_i) in enumerate(entries):
            for index_j, axis_j, start_j, end_j in entries[i + 1 :]:
                offset = axis_j - axis_i
                if offset > max_axis_offset:
                    break
                if offset < min_axis_offset:
                    continue
                overlap = min(end_i, end_j) - max(start_i, start_j)
                shorter = max(min(end_i - start_i, end_j - start_j), 1e-9)
                if overlap / shorter >= min_overlap_fraction:
                    duplicates.add(index_i)
                    duplicates.add(index_j)
    return round(len(duplicates) / len(segments), 4)


def merge_potential(segments: list[tuple[float, float, float, float]]) -> dict[str, Any]:
    """Dry-run small-gap collinear merging; major-axis full-span bridging disabled."""
    pairs = [((x1, y1), (x2, y2)) for x1, y1, x2, y2 in segments]
    if not pairs:
        return {"input_count": 0, "merged_count": 0, "reduction_ratio": 0.0}
    merged = repair_major_axis_segments(
        pairs,
        axis_tolerance=DRY_RUN_AXIS_TOLERANCE,
        small_gap_tolerance=DRY_RUN_SMALL_GAP_TOLERANCE,
        min_major_segments=10**9,
        min_major_span=float("inf"),
        min_major_total_length=float("inf"),
    )
    return {
        "input_count": len(pairs),
        "merged_count": len(merged),
        "reduction_ratio": round(1.0 - len(merged) / len(pairs), 4),
    }


def closure_dry_run(segments: list[tuple[float, float, float, float]]) -> dict[str, int]:
    pairs = [((x1, y1), (x2, y2)) for x1, y1, x2, y2 in segments]
    counts: dict[str, int] = {}
    for sides in (4, 3):
        closures = find_axis_aligned_rectangle_closures(
            pairs,
            axis_tolerance=DRY_RUN_AXIS_TOLERANCE,
            corner_tolerance=DRY_RUN_CORNER_TOLERANCE,
            min_width=DRY_RUN_MIN_RECT_SIDE,
            min_height=DRY_RUN_MIN_RECT_SIDE,
            min_side_coverage=DRY_RUN_MIN_SIDE_COVERAGE,
            min_present_sides=sides,
        )
        counts[f"closable_rectangles_{sides}side"] = len(closures)
    return counts


# ---------------------------------------------------------------------------
# raster helpers
# ---------------------------------------------------------------------------


def dark_mask(gray: np.ndarray, *, gray_threshold: int) -> np.ndarray:
    _, mask = cv2.threshold(gray, gray_threshold, 255, cv2.THRESH_BINARY_INV)
    return mask


def distance_to_nonzero(mask: np.ndarray) -> np.ndarray:
    """Per-pixel distance to the nearest nonzero pixel of ``mask``."""
    inverted = np.where(mask > 0, 0, 255).astype(np.uint8)
    return cv2.distanceTransform(inverted, cv2.DIST_L2, 3)


def skeletonize(mask: np.ndarray, *, max_iterations: int = 128) -> np.ndarray:
    """Morphological skeleton (cv2.ximgproc.thinning is unavailable in this env)."""
    kernel = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    working = (mask > 0).astype(np.uint8) * 255
    skeleton = np.zeros_like(working)
    for _ in range(max_iterations):
        eroded = cv2.erode(working, kernel)
        opened = cv2.dilate(eroded, kernel)
        skeleton = cv2.bitwise_or(skeleton, cv2.subtract(working, opened))
        working = eroded
        if not working.any():
            break
    return skeleton


def rasterize_segments(
    segments: list[tuple[float, float, float, float]], *, shape: tuple[int, int]
) -> np.ndarray:
    canvas = np.zeros(shape, dtype=np.uint8)
    for x1, y1, x2, y2 in segments:
        cv2.line(canvas, (int(round(x1)), int(round(y1))), (int(round(x2)), int(round(y2))), 255, 1)
    return canvas


def precision_proxy(line_mask: np.ndarray, dark_distance: np.ndarray) -> float:
    """Fraction of candidate-line pixels lying on (near) dark source pixels."""
    pixels = line_mask > 0
    if not pixels.any():
        return 0.0
    return round(float((dark_distance[pixels] <= PRECISION_TOLERANCE_PX).mean()), 4)


def recall_proxy(line_mask: np.ndarray, skeleton: np.ndarray) -> float:
    """Fraction of dark-mask skeleton pixels covered by a nearby candidate line."""
    skeleton_pixels = skeleton > 0
    if not skeleton_pixels.any():
        return 0.0
    line_distance = distance_to_nonzero(line_mask)
    return round(float((line_distance[skeleton_pixels] <= RECALL_TOLERANCE_PX).mean()), 4)


def text_region_mask(
    text_features: list[dict[str, Any]], *, shape: tuple[int, int], pad_px: int = TEXT_BBOX_PAD_PX
) -> np.ndarray:
    mask = np.zeros(shape, dtype=np.uint8)
    height, width = shape
    for item in text_features:
        properties = item.get("properties") or {}
        try:
            left = int(properties["bbox_left_px"]) - pad_px
            top = int(properties["bbox_top_px"]) - pad_px
            right = int(properties["bbox_right_px"]) + pad_px
            bottom = int(properties["bbox_bottom_px"]) + pad_px
        except (KeyError, TypeError, ValueError):
            continue
        left = max(0, left)
        top = max(0, top)
        right = min(width - 1, right)
        bottom = min(height - 1, bottom)
        if right > left and bottom > top:
            mask[top : bottom + 1, left : right + 1] = 255
    return mask


def text_region_false_line_density(line_mask: np.ndarray, text_mask: np.ndarray) -> float:
    total = int(np.count_nonzero(line_mask))
    if total == 0:
        return 0.0
    inside = int(np.count_nonzero(line_mask[text_mask > 0]))
    return round(inside / total, 4)


# ---------------------------------------------------------------------------
# overlays
# ---------------------------------------------------------------------------


def compute_crop_regions(
    content_mask: np.ndarray,
    text_features: list[dict[str, Any]],
    *,
    crop_size: tuple[int, int] = (1200, 900),
) -> dict[str, tuple[int, int, int, int]]:
    """Fixed 1:1 review regions (left, top, right, bottom), identical across combos."""
    height, width = content_mask.shape
    crop_w, crop_h = crop_size
    rows = np.flatnonzero(content_mask.any(axis=1))
    cols = np.flatnonzero(content_mask.any(axis=0))
    if rows.size and cols.size:
        top, bottom = int(rows[0]), int(rows[-1])
        left, right = int(cols[0]), int(cols[-1])
    else:
        top, bottom, left, right = 0, height - 1, 0, width - 1

    def clamp(x: int, y: int) -> tuple[int, int, int, int]:
        x = max(0, min(x, width - crop_w))
        y = max(0, min(y, height - crop_h))
        return (x, y, x + crop_w, y + crop_h)

    regions = {
        "frame_top_left": clamp(left - 50, top - 50),
        "frame_bottom_right": clamp(right - crop_w + 50, bottom - crop_h + 50),
        "content_center": clamp((left + right - crop_w) // 2, (top + bottom - crop_h) // 2),
    }

    scale = 8
    small = cv2.resize(
        content_mask, (max(1, width // scale), max(1, height // scale)), interpolation=cv2.INTER_AREA
    ).astype(np.float32)
    kernel = (max(1, crop_h // scale), max(1, crop_w // scale))
    density = cv2.boxFilter(small, -1, (kernel[1], kernel[0]))
    y_idx, x_idx = np.unravel_index(int(np.argmax(density)), density.shape)
    regions["densest_line_region"] = clamp(int(x_idx) * scale - crop_w // 2, int(y_idx) * scale - crop_h // 2)

    centers = []
    for item in text_features:
        properties = item.get("properties") or {}
        try:
            centers.append(
                (
                    (int(properties["bbox_left_px"]) + int(properties["bbox_right_px"])) // 2,
                    (int(properties["bbox_top_px"]) + int(properties["bbox_bottom_px"])) // 2,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    if centers:
        points = np.asarray(centers, dtype=np.float64)
        best_index = 0
        best_neighbors = -1
        for index, point in enumerate(points):
            neighbors = int(
                np.count_nonzero(np.hypot(points[:, 0] - point[0], points[:, 1] - point[1]) < max(crop_w, crop_h))
            )
            if neighbors > best_neighbors:
                best_neighbors = neighbors
                best_index = index
        cx, cy = centers[best_index]
        regions["densest_text_region"] = clamp(cx - crop_w // 2, cy - crop_h // 2)
    return regions


def render_overlays(
    gray: np.ndarray,
    segments: list[tuple[float, float, float, float]],
    *,
    combo_dir: Path,
    crop_regions: dict[str, tuple[int, int, int, int]],
    overlay_max_dim: int = 1800,
) -> dict[str, Any]:
    height, width = gray.shape
    outputs: dict[str, Any] = {}

    scale = min(1.0, overlay_max_dim / max(height, width))
    small = cv2.resize(gray, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
    full = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)
    for x1, y1, x2, y2 in segments:
        cv2.line(
            full,
            (int(round(x1 * scale)), int(round(y1 * scale))),
            (int(round(x2 * scale)), int(round(y2 * scale))),
            (0, 0, 255),
            1,
        )
    full_path = combo_dir / "overlay_full.png"
    cv2.imwrite(str(full_path), full)
    outputs["overlay_full"] = str(full_path)

    crops_dir = combo_dir / "overlay_crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    crop_paths: dict[str, str] = {}
    for name, (left, top, right, bottom) in crop_regions.items():
        crop = cv2.cvtColor(gray[top:bottom, left:right], cv2.COLOR_GRAY2BGR)
        for x1, y1, x2, y2 in segments:
            cv2.line(
                crop,
                (int(round(x1 - left)), int(round(y1 - top))),
                (int(round(x2 - left)), int(round(y2 - top))),
                (0, 0, 255),
                1,
            )
        crop_path = crops_dir / f"{name}.png"
        cv2.imwrite(str(crop_path), crop)
        crop_paths[name] = str(crop_path)
    outputs["overlay_crops"] = crop_paths
    return outputs


# ---------------------------------------------------------------------------
# workbench driver
# ---------------------------------------------------------------------------


def _load_features(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("features") or [])


def evaluate_combo(
    rgb: np.ndarray,
    gray: np.ndarray,
    combo: ComboSpec,
    *,
    dark_distance: np.ndarray,
    skeleton: np.ndarray,
    text_mask: np.ndarray | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    height = rgb.shape[0]
    features = extract_line_candidates(
        rgb,
        min_length=combo.min_line_length,
        gray_threshold=combo.gray_threshold,
        max_line_gap=combo.max_line_gap,
        use_canny=combo.use_canny,
    )
    segments = feature_image_segments(features, height=height)
    lengths = segment_lengths(segments)
    line_mask = rasterize_segments(segments, shape=gray.shape)

    metrics: dict[str, Any] = {
        "candidate_count": len(features),
        "length_stats": length_stats(lengths),
        "orientation_bins": orientation_bins(segments),
        "short_fragment_ratio_lt180": short_fragment_ratio(lengths),
        "parallel_duplicate_ratio": parallel_duplicate_ratio(segments),
        "precision_proxy": precision_proxy(line_mask, dark_distance),
        "recall_proxy": recall_proxy(line_mask, skeleton),
        "merge_potential": merge_potential(segments),
        "closure_dry_run": closure_dry_run(segments),
    }
    if text_mask is not None:
        metrics["text_region_false_line_density"] = text_region_false_line_density(line_mask, text_mask)
    else:
        metrics["text_region_false_line_density"] = None
    return features, metrics


def _select_recommendation(
    combo_reports: list[dict[str, Any]], baseline_key: str
) -> tuple[str | None, str]:
    rule = (
        "eligible = precision_proxy >= baseline - 0.002 and "
        "text_region_false_line_density <= baseline * 1.10 + 0.002; "
        "rank eligible by recall_proxy descending. Advisory only; human overlay review decides."
    )
    baseline = next((item for item in combo_reports if item["key"] == baseline_key), None)
    if baseline is None:
        return None, rule
    base_precision = baseline["metrics"]["precision_proxy"]
    base_text = baseline["metrics"]["text_region_false_line_density"]
    eligible = []
    for item in combo_reports:
        metrics = item["metrics"]
        if metrics["precision_proxy"] < base_precision - 0.002:
            continue
        if base_text is not None and metrics["text_region_false_line_density"] is not None:
            if metrics["text_region_false_line_density"] > base_text * 1.10 + 0.002:
                continue
        eligible.append(item)
    if not eligible:
        return None, rule
    best = max(eligible, key=lambda item: item["metrics"]["recall_proxy"])
    return best["key"], rule


def run_param_workbench(
    *,
    output_root: Path,
    map_id: str,
    source_raster: Path | None = None,
    text_candidates: Path | None = None,
    grid: list[ComboSpec] | None = None,
    crop_size: tuple[int, int] = (1200, 900),
    overlay_max_dim: int = 1800,
    reset: bool = False,
    resume: bool = False,
) -> dict[str, Any]:
    output_root = Path(output_root)
    map_key = map_id.lower()
    workbench_dir = output_root / WORKBENCH_DIR_NAME
    report_path = workbench_dir / REPORT_NAME
    if report_path.exists() and not reset:
        raise FileExistsError(
            f"{report_path} already exists; pass reset=True/--reset to rerun the workbench"
        )

    if source_raster is None:
        source_raster = output_root / "00_INPUT_FREEZE" / f"{map_key}_source_frozen.tif"
    source_raster = Path(source_raster)
    if not source_raster.is_file():
        raise FileNotFoundError(source_raster)

    if text_candidates is None:
        candidate_path = output_root / "05_TEXT_WORKFLOW" / f"{map_key}_review_text_candidates.geojson"
        text_candidates = candidate_path if candidate_path.is_file() else None

    expected_baseline: dict[str, Any] | None = None
    generation_report = output_root / "04_LINE_WORKFLOW" / "LINE_CANDIDATE_GENERATION_REPORT.json"
    if generation_report.is_file():
        payload = json.loads(generation_report.read_text(encoding="utf-8"))
        if int(payload.get("min_line_length", -1)) == BASELINE_COMBO[0]:
            expected_baseline = {
                "source": str(generation_report),
                "feature_count": int(payload.get("feature_count", -1)),
            }

    combos = list(grid) if grid is not None else default_grid()
    workbench_dir.mkdir(parents=True, exist_ok=True)

    rgb = load_rgb(source_raster)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape

    text_features: list[dict[str, Any]] = []
    if text_candidates is not None:
        text_features = _load_features(Path(text_candidates))
    text_mask = text_region_mask(text_features, shape=gray.shape) if text_features else None

    reference_mask = dark_mask(gray, gray_threshold=BASELINE_COMBO[2])
    crop_regions = compute_crop_regions(reference_mask, text_features, crop_size=crop_size)
    del reference_mask

    combo_reports: list[dict[str, Any]] = []
    baseline_actual_count: int | None = None
    thresholds = sorted({combo.gray_threshold for combo in combos})
    for threshold in thresholds:
        threshold_combos = [item for item in combos if item.gray_threshold == threshold]
        pending = [
            item
            for item in threshold_combos
            if not (resume and (workbench_dir / f"combo_{item.key}" / "stats.json").is_file())
        ]
        resumed = [item for item in threshold_combos if item not in pending]
        for combo in resumed:
            stats = json.loads(
                (workbench_dir / f"combo_{combo.key}" / "stats.json").read_text(encoding="utf-8")
            )
            stats["resumed_from_previous_run"] = True
            combo_reports.append(stats)
            if (
                combo.min_line_length,
                combo.max_line_gap,
                combo.gray_threshold,
                combo.use_canny,
            ) == BASELINE_COMBO:
                baseline_actual_count = stats["metrics"]["candidate_count"]
            print(f"[param_workbench] {combo.key}: resumed from existing stats.json", flush=True)
        if not pending:
            continue
        threshold_mask = dark_mask(gray, gray_threshold=threshold)
        dark_distance = distance_to_nonzero(threshold_mask)
        skeleton = skeletonize(threshold_mask)
        del threshold_mask
        for combo in pending:
            started = time.perf_counter()
            features, metrics = evaluate_combo(
                rgb,
                gray,
                combo,
                dark_distance=dark_distance,
                skeleton=skeleton,
                text_mask=text_mask,
            )
            combo_dir = workbench_dir / f"combo_{combo.key}"
            combo_dir.mkdir(parents=True, exist_ok=True)
            write_geojson(combo_dir / "candidates.geojson", features)
            segments = feature_image_segments(features, height=height)
            overlay_outputs = render_overlays(
                gray,
                segments,
                combo_dir=combo_dir,
                crop_regions=crop_regions,
                overlay_max_dim=overlay_max_dim,
            )
            duration = round(time.perf_counter() - started, 2)
            stats = {
                "combo": combo.as_dict(),
                "key": combo.key,
                "metrics": metrics,
                "outputs": {
                    "candidates_geojson": str(combo_dir / "candidates.geojson"),
                    **overlay_outputs,
                },
                "duration_seconds": duration,
            }
            (combo_dir / "stats.json").write_text(
                json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            combo_reports.append(stats)
            if (
                combo.min_line_length,
                combo.max_line_gap,
                combo.gray_threshold,
                combo.use_canny,
            ) == BASELINE_COMBO:
                baseline_actual_count = metrics["candidate_count"]
            print(
                f"[param_workbench] {combo.key}: count={metrics['candidate_count']} "
                f"precision={metrics['precision_proxy']} recall={metrics['recall_proxy']} "
                f"dup={metrics['parallel_duplicate_ratio']} ({duration}s)",
                flush=True,
            )
        del dark_distance, skeleton

    baseline_key = ComboSpec(*BASELINE_COMBO).key
    recommended_key, selection_rule = _select_recommendation(combo_reports, baseline_key)

    baseline_check: dict[str, Any] = {
        "combo": ComboSpec(*BASELINE_COMBO).as_dict(),
        "actual_count": baseline_actual_count,
        "expected_count": expected_baseline["feature_count"] if expected_baseline else None,
        "expected_source": expected_baseline["source"] if expected_baseline else None,
        "match": (
            expected_baseline is not None
            and baseline_actual_count is not None
            and baseline_actual_count == expected_baseline["feature_count"]
        ),
    }

    report = {
        "program": "param_workbench",
        "map_id": map_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "output_root": str(output_root),
        "workbench_dir": str(workbench_dir),
        "source_raster": str(source_raster),
        "image_size_px": [int(width), int(height)],
        "text_candidates": str(text_candidates) if text_candidates else None,
        "crop_regions": {name: list(region) for name, region in crop_regions.items()},
        "grid_size": len(combos),
        "baseline_reproduction": baseline_check,
        "selection_rule": selection_rule,
        "recommended_key": recommended_key,
        "production_defaults_changed": False,
        "writes_checked_yes": False,
        "combos": combo_reports,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_summary(workbench_dir / SUMMARY_NAME, report)
    return report


def _write_summary(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# Param Workbench Summary",
        "",
        f"- map: `{report['map_id']}`",
        f"- source: `{report['source_raster']}`",
        f"- grid size: {report['grid_size']}",
        f"- baseline reproduction: actual={report['baseline_reproduction']['actual_count']} "
        f"expected={report['baseline_reproduction']['expected_count']} "
        f"match={report['baseline_reproduction']['match']}",
        f"- recommended (advisory): `{report['recommended_key']}`",
        f"- selection rule: {report['selection_rule']}",
        "",
        "Production defaults are unchanged. Any default change requires human overlay review.",
        "",
        "| combo | count | precision | recall | dup | short<180 | text_dens | merge_red | rect4 | rect3 | sec |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    ordered = sorted(
        report["combos"], key=lambda item: item["metrics"]["recall_proxy"], reverse=True
    )
    for item in ordered:
        metrics = item["metrics"]
        marker = " **<- recommended**" if item["key"] == report["recommended_key"] else ""
        baseline_marker = " (baseline)" if item["key"] == ComboSpec(*BASELINE_COMBO).key else ""
        lines.append(
            f"| {item['key']}{baseline_marker}{marker} "
            f"| {metrics['candidate_count']} "
            f"| {metrics['precision_proxy']} "
            f"| {metrics['recall_proxy']} "
            f"| {metrics['parallel_duplicate_ratio']} "
            f"| {metrics['short_fragment_ratio_lt180']} "
            f"| {metrics['text_region_false_line_density']} "
            f"| {metrics['merge_potential']['reduction_ratio']} "
            f"| {metrics['closure_dry_run']['closable_rectangles_4side']} "
            f"| {metrics['closure_dry_run']['closable_rectangles_3side']} "
            f"| {item['duration_seconds']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Line extraction parameter workbench")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run the parameter grid")
    run_parser.add_argument("--output-root", required=True)
    run_parser.add_argument("--map-id", required=True)
    run_parser.add_argument("--source-raster", default=None)
    run_parser.add_argument("--text-candidates", default=None)
    run_parser.add_argument("--reset", action="store_true")
    run_parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip combos whose stats.json already exists (crash recovery).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "run":
        report = run_param_workbench(
            output_root=Path(args.output_root),
            map_id=args.map_id,
            source_raster=Path(args.source_raster) if args.source_raster else None,
            text_candidates=Path(args.text_candidates) if args.text_candidates else None,
            reset=bool(args.reset),
            resume=bool(args.resume),
        )
        print(json.dumps(report["baseline_reproduction"], ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
