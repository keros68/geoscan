from __future__ import annotations

import cv2
import numpy as np

from .candidates import feature
from .raster import image_point_to_map_point, rgb_to_bgr


def _contour_to_ring(contour: np.ndarray, *, height: int) -> list[list[float]]:
    points = contour[:, 0, :]
    ring = [image_point_to_map_point(x, y, height=height) for x, y in points]
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring


def extract_color_area_candidates(
    rgb: np.ndarray,
    *,
    min_area: int = 250,
    hue_ranges: tuple[tuple[int, int], ...] = ((160, 179), (0, 12)),
) -> list[dict]:
    height = rgb.shape[0]
    hsv = cv2.cvtColor(rgb_to_bgr(rgb), cv2.COLOR_BGR2HSV)
    mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
    for low_h, high_h in hue_ranges:
        partial = cv2.inRange(
            hsv,
            np.array([low_h, 35, 50], dtype=np.uint8),
            np.array([high_h, 255, 255], dtype=np.uint8),
        )
        mask = cv2.bitwise_or(mask, partial)

    kernel = np.ones((3, 3), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _hierarchy = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        epsilon = max(1.0, 0.002 * cv2.arcLength(contour, True))
        simplified = cv2.approxPolyDP(contour, epsilon, True)
        ring = _contour_to_ring(simplified, height=height)
        if len(ring) < 4:
            continue
        candidates.append(
            feature(
                geometry={"type": "Polygon", "coordinates": [ring]},
                target="WP",
                cad_layer="T04_AUTO_COLOR_AREA",
                feature_name="auto_color_area",
                source="auto",
                confidence=0.75,
                note="自动颜色面区候选；需人工复核边界和地质含义。",
                mapgis_no=100,
                extra={"area_px": round(float(area), 2), "fill_color": "pink/red"},
            )
        )
    return candidates
