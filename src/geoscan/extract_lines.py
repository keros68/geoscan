from __future__ import annotations

import math

import cv2
import numpy as np

from .candidates import feature
from .raster import image_point_to_map_point


def _line_length(x1: int, y1: int, x2: int, y2: int) -> float:
    return math.hypot(x2 - x1, y2 - y1)


def extract_line_candidates(
    rgb: np.ndarray,
    *,
    min_length: int = 80,
    canny_low: int = 50,
    canny_high: int = 150,
    gray_threshold: int = 190,
    hough_threshold: int = 50,
    max_line_gap: int = 8,
    use_canny: bool = True,
) -> list[dict]:
    height = rgb.shape[0]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    _, dark = cv2.threshold(gray, gray_threshold, 255, cv2.THRESH_BINARY_INV)
    # use_canny=False feeds the binary mask to Hough directly, avoiding the
    # double-edge artifact where each thick stroke yields two parallel edges.
    edges = cv2.Canny(dark, canny_low, canny_high) if use_canny else dark
    raw_lines = cv2.HoughLinesP(
        edges,
        rho=1,
        theta=np.pi / 180,
        threshold=hough_threshold,
        minLineLength=min_length,
        maxLineGap=max_line_gap,
    )
    if raw_lines is None:
        return []

    candidates = []
    seen: set[tuple[int, int, int, int]] = set()
    # OpenCV 4.x returns (N, 1, 4); OpenCV 5.x returns (N, 4). Normalize.
    for raw in raw_lines.reshape(-1, 4):
        x1, y1, x2, y2 = [int(value) for value in raw]
        length = _line_length(x1, y1, x2, y2)
        if length < min_length:
            continue
        key = tuple(round(value / 2) * 2 for value in (x1, y1, x2, y2))
        rev_key = (key[2], key[3], key[0], key[1])
        if key in seen or rev_key in seen:
            continue
        seen.add(key)
        coordinates = [
            image_point_to_map_point(x1, y1, height=height),
            image_point_to_map_point(x2, y2, height=height),
        ]
        candidates.append(
            feature(
                geometry={"type": "LineString", "coordinates": coordinates},
                target="WL",
                cad_layer="T04_AUTO_LINE",
                feature_name="auto_straight_line",
                source="auto",
                confidence=min(0.99, max(0.5, length / max(rgb.shape[:2]))),
                note="自动直线候选；需人工复核是否为真实图线。",
                mapgis_no=10,
                extra={"length_px": round(length, 2)},
            )
        )
    return candidates
