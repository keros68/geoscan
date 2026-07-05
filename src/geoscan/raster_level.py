"""Scan leveling: convert an arbitrary scan (jpg/png/bmp/tif) to an RGB TIFF
with a conservative deskew, so the vectorization pipeline gets the same kind of
input the standalone ``jpg_rgb_tiff_level_tool`` used to produce out-of-band.

Deskew evidence comes only from paper structure (map frame, table lines, long
grid lines) — never from geological content — and is skipped when no reliable
long straight edge is found. No colour/contrast changes are made: this is a
geometry + container-format step, not an enhancement.

This module is the single source of truth for the leveling algorithm; the
standalone CLI tool imports these functions so there is only one copy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageFile


Image.MAX_IMAGE_PIXELS = None

# Suffixes that are already the pipeline's expected container. "auto" leveling
# passes these through untouched (the user's RGB调平TIFF folder is already leveled).
TIFF_SUFFIXES = {".tif", ".tiff"}


@dataclass(frozen=True)
class LevelParams:
    target_dpi: tuple[int, int] = (300, 300)
    crop_blue_sheet: bool = True
    deskew: bool = True
    min_angle_deg: float = 0.02
    max_angle_deg: float = 3.0
    allow_truncated_images: bool = True


def proxy_gray(image: Image.Image, max_side: int | None = None, max_width: int = 3200) -> np.ndarray:
    width, height = image.size
    if max_side is not None:
        scale = min(1.0, max_side / max(width, height))
    else:
        scale = min(1.0, max_width / width)
    if scale == 1.0:
        proxy = image
    else:
        proxy = image.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            Image.Resampling.BILINEAR,
        )
    return np.asarray(proxy.convert("L"))


def detect_blue_sheet_crop(image: Image.Image) -> tuple[tuple[int, int, int, int], str]:
    """Crop photographed background around a blue sheet when detection is strong."""
    width, height = image.size
    scale = min(1.0, 2400 / max(width, height))
    proxy = image if scale == 1.0 else image.resize(
        (int(width * scale), int(height * scale)), Image.Resampling.BILINEAR
    )
    arr = np.asarray(proxy.convert("RGB"))
    red = arr[:, :, 0].astype(np.int16)
    green = arr[:, :, 1].astype(np.int16)
    blue = arr[:, :, 2].astype(np.int16)
    mask = (blue - red > 12) & (green - red > 3) & (blue > 110) & (green > 100)
    blue_fraction = float(mask.mean())
    if blue_fraction < 0.25:
        return (0, 0, width, height), "none"

    closed = cv2.morphologyEx(
        (mask.astype("uint8") * 255),
        cv2.MORPH_CLOSE,
        np.ones((9, 25), np.uint8),
    )
    row_fraction = (closed > 0).mean(axis=1)
    col_fraction = (closed > 0).mean(axis=0)
    ys = np.where(row_fraction > 0.55)[0]
    xs = np.where(col_fraction > 0.35)[0]
    if not (ys.size and xs.size):
        return (0, 0, width, height), "none"

    x1 = max(0, int(round(xs[0] / scale)))
    y1 = max(0, int(round(ys[0] / scale)))
    x2 = min(width, int(round((xs[-1] + 1) / scale)))
    y2 = min(height, int(round((ys[-1] + 1) / scale)))
    crop_width = x2 - x1
    crop_height = y2 - y1
    removed_fraction = (width * height - crop_width * crop_height) / max(1, width * height)

    if crop_width > width * 0.80 and crop_height > height * 0.45 and removed_fraction > 0.03:
        return (x1, y1, x2, y2), f"blue_sheet fraction={blue_fraction:.3f}"
    return (0, 0, width, height), "none"


def fit_frame_angle(image: Image.Image) -> tuple[float | None, str]:
    """Estimate skew from long top/bottom frame-like dark rows."""
    gray = proxy_gray(image, max_side=2200)
    if gray.size == 0:
        return None, "frame:no_gray"
    mask = gray < 90
    row_counts = mask.sum(axis=1)
    height, width = mask.shape
    if height < 100 or width < 100:
        return None, "frame:small"

    def fit_row(center_y: int, half: int = 8) -> tuple[int, float] | None:
        y1 = max(0, center_y - half)
        y2 = min(height, center_y + half + 1)
        roi = mask[y1:y2, :]
        xs: list[int] = []
        ys: list[float] = []
        for x in range(roi.shape[1]):
            found = np.where(roi[:, x])[0]
            if found.size:
                xs.append(x)
                ys.append(float(np.median(found) + y1))
        if not xs:
            return None

        xs_arr = np.asarray(xs)
        ys_arr = np.asarray(ys)
        blocks: list[tuple[int, int]] = []
        start = 0
        for idx in range(1, len(xs_arr)):
            if xs_arr[idx] != xs_arr[idx - 1] + 1:
                blocks.append((start, idx))
                start = idx
        blocks.append((start, len(xs_arr)))
        block = max(blocks, key=lambda pair: pair[1] - pair[0])
        xb = xs_arr[block[0] : block[1]]
        yb = ys_arr[block[0] : block[1]]
        if len(xb) < width * 0.38:
            return None

        coef = np.polyfit(xb, yb, 1)
        residual = yb - (coef[0] * xb + coef[1])
        keep = np.abs(residual) < 2.5
        if keep.sum() > width * 0.30:
            coef = np.polyfit(xb[keep], yb[keep], 1)
        return len(xb), math.degrees(math.atan(coef[0]))

    results: list[tuple[str, int, float]] = []
    zones = (
        ("top", (int(height * 0.03), int(height * 0.32))),
        ("bottom", (int(height * 0.68), int(height * 0.98))),
    )
    for zone_name, (low, high) in zones:
        candidates = [int(y) for y in np.argsort(row_counts)[::-1] if low < y < high][:35]
        seen: list[int] = []
        for candidate in candidates:
            if any(abs(candidate - old) < 4 for old in seen):
                continue
            seen.append(candidate)
            fitted = fit_row(candidate)
            if fitted and abs(fitted[1]) <= 3:
                results.append((zone_name, fitted[0], fitted[1]))
                break

    if not results:
        return None, "frame:no_line"
    total_length = sum(length for _, length, _ in results)
    if total_length < width * 0.45:
        detail = ";".join(f"{name}:{angle:.3f}/len{length}" for name, length, angle in results)
        return None, "frame:weak_" + detail
    angle = sum(length * angle for _, length, angle in results) / total_length
    detail = ";".join(f"{name}:{angle:.3f}/len{length}" for name, length, angle in results)
    return angle, "frame:" + detail


def hough_angle(image: Image.Image) -> tuple[float | None, str]:
    """Fallback skew estimate from long horizontal/vertical line segments."""
    gray = proxy_gray(image, max_width=3200)
    height, width = gray.shape
    if height < 80 or width < 80:
        return None, "hough:small"

    blur = cv2.GaussianBlur(gray, (0, 0), 7)
    contrast = cv2.subtract(blur, gray)
    contrast = cv2.normalize(contrast, None, 0, 255, cv2.NORM_MINMAX).astype("uint8")
    _, threshold = cv2.threshold(contrast, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    edges = cv2.Canny(threshold, 50, 150)
    min_len = max(90, int(min(width, height) * 0.12))
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 1800,
        threshold=90,
        minLineLength=min_len,
        maxLineGap=35,
    )
    if lines is None:
        return None, "hough:no_lines"

    try:
        segments = np.asarray(lines).reshape(-1, 4)
    except ValueError:
        return None, f"hough:unexpected_lines_shape_{tuple(np.asarray(lines).shape)}"

    horizontal: list[tuple[float, float]] = []
    vertical: list[tuple[float, float]] = []
    for x1, y1, x2, y2 in segments:
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length < min_len:
            continue
        angle = math.degrees(math.atan2(dy, dx))
        while angle <= -90:
            angle += 180
        while angle > 90:
            angle -= 180
        if abs(angle) <= 3:
            horizontal.append((length, angle))
        elif abs(abs(angle) - 90) <= 3:
            vertical_deviation = angle - 90 if angle > 0 else angle + 90
            vertical.append((length, vertical_deviation))

    horizontal = sorted(horizontal, reverse=True)[:30]
    vertical = sorted(vertical, reverse=True)[:30]
    candidates: list[tuple[str, float, float]] = []

    if horizontal:
        total = sum(length for length, _ in horizontal)
        if total >= width * 0.50:
            candidates.append(("h", total, sum(length * angle for length, angle in horizontal) / total))
    if vertical:
        total = sum(length for length, _ in vertical)
        if total >= height * 0.50:
            candidates.append(("v", total, sum(length * angle for length, angle in vertical) / total))

    if not candidates:
        return None, (
            f"hough:weak h={sum(length for length, _ in horizontal):.1f} "
            f"v={sum(length for length, _ in vertical):.1f}"
        )

    if len(candidates) == 2 and abs(candidates[0][2] - candidates[1][2]) <= 0.8:
        total = sum(candidate[1] for candidate in candidates)
        angle = sum(candidate[1] * candidate[2] for candidate in candidates) / total
        detail = ";".join(f"{axis}:{angle:.3f}/len{length:.0f}" for axis, length, angle in candidates)
        return angle, "hough:hv_" + detail

    axis, total, angle = max(candidates, key=lambda candidate: candidate[1])
    return angle, f"hough:{axis}_{angle:.3f}/len{total:.0f}"


def detect_level_angle(
    image: Image.Image,
    *,
    deskew: bool = True,
    min_angle_deg: float = 0.02,
    max_angle_deg: float = 3.0,
) -> tuple[float, str]:
    if not deskew:
        return 0.0, "deskew_disabled"

    angle, method = fit_frame_angle(image)
    if angle is None:
        angle, method = hough_angle(image)

    if angle is None:
        return 0.0, method + "|kept_original"
    if abs(angle) > max_angle_deg:
        return 0.0, method + "|rejected_large_angle"
    if abs(angle) < min_angle_deg:
        return 0.0, method + "|tiny_angle"
    return angle, method


def median_corner_fill(image: Image.Image) -> tuple[int, int, int]:
    width, height = image.size
    corner = max(8, min(80, width // 20, height // 20))
    corners = [
        image.crop((0, 0, corner, corner)),
        image.crop((width - corner, 0, width, corner)),
        image.crop((0, height - corner, corner, height)),
        image.crop((width - corner, height - corner, width, height)),
    ]
    samples = np.concatenate([np.asarray(item.convert("RGB")).reshape(-1, 3) for item in corners], axis=0)
    return tuple(int(value) for value in np.median(samples, axis=0))


def level_to_rgb_tiff(source: Path, target: Path, params: LevelParams | None = None) -> dict[str, Any]:
    """Level ``source`` into an RGB, ``target_dpi``, uncompressed TIFF at ``target``.

    Source is only read, never modified. Returns a record describing the crop,
    detected skew angle, and detection method for the run report.
    """
    params = params or LevelParams()
    ImageFile.LOAD_TRUNCATED_IMAGES = params.allow_truncated_images
    source = Path(source)
    target = Path(target)

    image0 = Image.open(source)
    original_size = image0.size
    original_mode = image0.mode
    image = image0.convert("RGB")

    if params.crop_blue_sheet:
        crop_box, crop_note = detect_blue_sheet_crop(image)
    else:
        crop_box, crop_note = (0, 0, image.size[0], image.size[1]), "disabled"
    working = image.crop(crop_box)

    angle, method = detect_level_angle(
        working,
        deskew=params.deskew,
        min_angle_deg=params.min_angle_deg,
        max_angle_deg=params.max_angle_deg,
    )
    if angle:
        fill = median_corner_fill(working)
        result = working.rotate(
            angle,
            resample=Image.Resampling.BICUBIC,
            expand=True,
            fillcolor=fill,
        )
    else:
        fill = ()
        result = working

    target.parent.mkdir(parents=True, exist_ok=True)
    result.save(target, format="TIFF", compression="raw", dpi=params.target_dpi)

    return {
        "source": str(source),
        "target": str(target),
        "original_size_px": [original_size[0], original_size[1]],
        "original_mode": original_mode,
        "output_size_px": [result.size[0], result.size[1]],
        "output_mode": result.mode,
        "output_dpi": [float(params.target_dpi[0]), float(params.target_dpi[1])],
        "crop_box": list(crop_box),
        "crop_note": crop_note,
        "angle_deg": round(float(angle), 6),
        "deskew_method": method,
        "fill_rgb": list(fill),
    }


def needs_leveling(source: Path, mode: str) -> bool:
    """Whether ``source`` should be leveled for the given mode.

    ``auto`` levels any non-TIFF input (jpg/png/bmp) and passes TIFFs through
    (the pipeline's expected input is already leveled); ``force`` levels every
    input; ``off`` never levels.
    """
    if mode == "off":
        return False
    if mode == "force":
        return True
    if mode == "auto":
        return Path(source).suffix.lower() not in TIFF_SUFFIXES
    raise ValueError(f"level_input must be one of auto|force|off, got {mode!r}")
