from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .candidates import feature
from .raster import image_point_to_map_point


def _expanded_box(
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    image_width: int,
    image_height: int,
    padding: int,
) -> tuple[int, int, int, int]:
    left = max(0, x - padding)
    top = max(0, y - padding)
    right = min(image_width, x + w + padding)
    bottom = min(image_height, y + h + padding)
    return left, top, right, bottom


def _is_text_like(
    w: int,
    h: int,
    area: float,
    *,
    min_width: int,
    min_height: int,
    min_area: int,
    max_width: int,
    max_height: int,
) -> bool:
    if w < min_width or h < min_height or area < min_area:
        return False
    if w > max_width or h > max_height:
        return False
    aspect = w / max(h, 1)
    if aspect < 0.15 or aspect > 18:
        return False
    # Very thin long components are usually frame/table/leader lines, not text.
    if h <= 4 or w <= 4:
        return False
    return True


def _vertical_overlap_ratio(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    top = max(a[1], b[1])
    bottom = min(a[3], b[3])
    overlap = max(0, bottom - top)
    min_height = max(1, min(a[3] - a[1], b[3] - b[1]))
    return overlap / min_height


def _merge_text_boxes(
    boxes: list[tuple[int, int, int, int, float]],
    *,
    max_horizontal_gap: int = 35,
    min_vertical_overlap: float = 0.35,
    max_group_width: int = 700,
) -> list[tuple[int, int, int, int, float, int]]:
    rows: list[list[tuple[int, int, int, int, float]]] = []
    for box in sorted(boxes, key=lambda item: (item[1] + item[3] / 2, item[0])):
        x, y, w, h, _area = box
        bounds = (x, y, x + w, y + h)
        for row in rows:
            row_left = min(item[0] for item in row)
            row_top = min(item[1] for item in row)
            row_right = max(item[0] + item[2] for item in row)
            row_bottom = max(item[1] + item[3] for item in row)
            if _vertical_overlap_ratio((row_left, row_top, row_right, row_bottom), bounds) >= min_vertical_overlap:
                row.append(box)
                break
        else:
            rows.append([box])

    groups: list[tuple[int, int, int, int, float, int]] = []
    for row in rows:
        current: tuple[int, int, int, int, float, int] | None = None
        for x, y, w, h, area in sorted(row, key=lambda item: item[0]):
            left, top, right, bottom = x, y, x + w, y + h
            if current is None:
                current = (left, top, right, bottom, area, 1)
                continue

            group_left, group_top, group_right, group_bottom, group_area, group_count = current
            gap = left - group_right
            merged_width = max(group_right, right) - min(group_left, left)

            if 0 <= gap <= max_horizontal_gap and merged_width <= max_group_width:
                current = (
                    min(group_left, left),
                    min(group_top, top),
                    max(group_right, right),
                    max(group_bottom, bottom),
                    group_area + area,
                    group_count + 1,
                )
            else:
                groups.append(current)
                current = (left, top, right, bottom, area, 1)
        if current is not None:
            groups.append(current)
    return sorted(groups, key=lambda item: (item[1], item[0]))


def extract_text_region_candidates(
    rgb: np.ndarray,
    *,
    crop_dir: Path,
    min_width: int = 12,
    min_height: int = 8,
    min_area: int = 45,
    padding: int = 4,
    max_candidates: int = 400,
    merge_nearby: bool = True,
    max_horizontal_gap: int = 35,
    min_vertical_overlap: float = 0.35,
    max_group_width: int = 700,
    max_text_width: int | None = None,
    max_text_height: int | None = None,
) -> list[dict]:
    image_height, image_width = rgb.shape[:2]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    _, dark = cv2.threshold(gray, 170, 255, cv2.THRESH_BINARY_INV)
    # Join nearby strokes into reviewable text-region crops.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 3))
    text_mask = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, kernel)
    contours, _hierarchy = cv2.findContours(text_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    crop_dir.mkdir(parents=True, exist_ok=True)
    for old_crop in crop_dir.glob("text_candidate_*.png"):
        old_crop.unlink()

    candidates = []
    boxes = []
    text_width_limit = max_text_width or max(250, int(image_width * 0.12))
    text_height_limit = max_text_height or max(120, int(image_height * 0.12))
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        if _is_text_like(
            w,
            h,
            area,
            min_width=min_width,
            min_height=min_height,
            min_area=min_area,
            max_width=text_width_limit,
            max_height=text_height_limit,
        ):
            boxes.append((x, y, w, h, area))

    if merge_nearby:
        crop_boxes = _merge_text_boxes(
            boxes,
            max_horizontal_gap=max_horizontal_gap,
            min_vertical_overlap=min_vertical_overlap,
            max_group_width=max_group_width,
        )
    else:
        crop_boxes = [(x, y, x + w, y + h, area, 1) for x, y, w, h, area in sorted(boxes, key=lambda item: (item[1], item[0]))]

    for index, (x, y, right_raw, bottom_raw, area, source_count) in enumerate(crop_boxes[:max_candidates], start=1):
        left, top, right, bottom = _expanded_box(
            x,
            y,
            right_raw - x,
            bottom_raw - y,
            image_width=image_width,
            image_height=image_height,
            padding=padding,
        )
        crop = rgb[top:bottom, left:right]
        crop_name = f"text_candidate_{index:04d}.png"
        Image.fromarray(crop).save(crop_dir / crop_name)

        center_x = left + (right - left) / 2
        center_y = top + (bottom - top) / 2
        candidates.append(
            feature(
                geometry={
                    "type": "Point",
                    "coordinates": image_point_to_map_point(center_x, center_y, height=image_height),
                },
                target="WT",
                cad_layer="T04_AUTO_TEXT_LINE_CROP" if source_count > 1 else "T04_AUTO_TEXT_CROP",
                feature_name="auto_text_line_region" if source_count > 1 else "auto_text_region",
                source="auto",
                confidence=0.45,
                note="自动合并文字区域候选；需人工看裁图确认文字内容。" if source_count > 1 else "自动文字区域候选；需人工看裁图确认文字内容。",
                mapgis_no=300,
                extra={
                    "crop_path": crop_name,
                    "ocr_text": "",
                    "crop_width_px": right - left,
                    "crop_height_px": bottom - top,
                    "area_px": round(float(area), 2),
                    "source_crop_count": source_count,
                    "bbox_left_px": left,
                    "bbox_top_px": top,
                    "bbox_right_px": right,
                    "bbox_bottom_px": bottom,
                },
            )
        )
    return candidates
