from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

import cv2
import numpy as np


Point: TypeAlias = tuple[float, float]
Segment: TypeAlias = tuple[Point, Point]
Region: TypeAlias = tuple[str, tuple[int, int, int, int]]


@dataclass(frozen=True)
class SmallBoxCandidate:
    bbox_px: tuple[int, int, int, int]
    region_name: str
    confidence: float
    side_coverage: tuple[float, float, float, float]
    fill_fraction: float


def _clamp_region(region: tuple[int, int, int, int], *, width: int, height: int) -> tuple[int, int, int, int]:
    left, top, right, bottom = region
    left = max(0, min(width, int(left)))
    right = max(0, min(width, int(right)))
    top = max(0, min(height, int(top)))
    bottom = max(0, min(height, int(bottom)))
    if right < left:
        left, right = right, left
    if bottom < top:
        top, bottom = bottom, top
    return left, top, right, bottom


def _side_coverage(dark_patch: np.ndarray) -> tuple[float, float, float, float]:
    height, width = dark_patch.shape[:2]
    if width <= 0 or height <= 0:
        return 0.0, 0.0, 0.0, 0.0
    band = max(1, min(3, int(round(min(width, height) * 0.15))))
    top = float(np.mean(np.any(dark_patch[:band, :] > 0, axis=0)))
    bottom = float(np.mean(np.any(dark_patch[-band:, :] > 0, axis=0)))
    left = float(np.mean(np.any(dark_patch[:, :band] > 0, axis=1)))
    right = float(np.mean(np.any(dark_patch[:, -band:] > 0, axis=1)))
    return top, bottom, left, right


def _bbox_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    if intersection == 0:
        return 0.0
    area_a = max(0, a[2] - a[0]) * max(0, a[3] - a[1])
    area_b = max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union else 0.0


def _dedupe_boxes(candidates: list[SmallBoxCandidate]) -> list[SmallBoxCandidate]:
    kept: list[SmallBoxCandidate] = []
    for candidate in sorted(candidates, key=lambda item: (-item.confidence, item.bbox_px)):
        if any(_bbox_iou(candidate.bbox_px, existing.bbox_px) >= 0.55 for existing in kept):
            continue
        kept.append(candidate)
    return sorted(kept, key=lambda item: (item.bbox_px[1], item.bbox_px[0], item.bbox_px[2], item.bbox_px[3]))


def detect_small_axis_aligned_boxes(
    rgb: np.ndarray,
    *,
    regions: list[Region] | None = None,
    threshold: int = 190,
    min_width_px: int = 8,
    min_height_px: int = 8,
    max_width_px: int = 48,
    max_height_px: int = 48,
    min_side_coverage: float = 0.72,
    min_present_sides: int = 4,
    max_fill_fraction: float = 0.78,
) -> list[SmallBoxCandidate]:
    """Detect small bordered legend/title-block boxes without lowering global line thresholds."""
    if min_present_sides < 3 or min_present_sides > 4:
        raise ValueError("min_present_sides must be 3 or 4")
    height, width = rgb.shape[:2]
    if regions is None:
        regions = [("full_image", (0, 0, width, height))]

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    _, dark = cv2.threshold(gray, threshold, 255, cv2.THRESH_BINARY_INV)
    candidates: list[SmallBoxCandidate] = []
    for region_name, raw_region in regions:
        left, top, right, bottom = _clamp_region(raw_region, width=width, height=height)
        if right <= left or bottom <= top:
            continue
        roi = dark[top:bottom, left:right]
        contours, _hierarchy = cv2.findContours(roi, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, box_width, box_height = cv2.boundingRect(contour)
            if box_width < min_width_px or box_height < min_height_px:
                continue
            if box_width > max_width_px or box_height > max_height_px:
                continue
            aspect = box_width / max(float(box_height), 1.0)
            if aspect < 0.45 or aspect > 2.2:
                continue
            patch = roi[y : y + box_height, x : x + box_width]
            fill_fraction = float(np.count_nonzero(patch) / max(patch.size, 1))
            if fill_fraction > max_fill_fraction:
                continue
            side_coverage = _side_coverage(patch)
            present_sides = sum(value >= min_side_coverage for value in side_coverage)
            if present_sides < min_present_sides:
                continue
            confidence = min(0.99, max(0.5, sum(side_coverage) / 4.0 * (1.0 - min(fill_fraction, 0.5))))
            candidates.append(
                SmallBoxCandidate(
                    bbox_px=(left + x, top + y, left + x + box_width, top + y + box_height),
                    region_name=region_name,
                    confidence=round(confidence, 6),
                    side_coverage=tuple(round(value, 6) for value in side_coverage),  # type: ignore[arg-type]
                    fill_fraction=round(fill_fraction, 6),
                )
            )
    return _dedupe_boxes(candidates)


def small_box_segments_to_map_segments(
    boxes: list[SmallBoxCandidate],
    *,
    image_height: int,
    coordinate_scale: float,
) -> list[Segment]:
    segments: list[Segment] = []
    for box in boxes:
        left, top, right, bottom = box.bbox_px
        x1 = round(left * coordinate_scale, 6)
        x2 = round(right * coordinate_scale, 6)
        y1 = round((image_height - bottom) * coordinate_scale, 6)
        y2 = round((image_height - top) * coordinate_scale, 6)
        segments.extend(
            [
                ((x1, y1), (x2, y1)),
                ((x1, y2), (x2, y2)),
                ((x1, y1), (x1, y2)),
                ((x2, y1), (x2, y2)),
            ]
        )
    return segments
