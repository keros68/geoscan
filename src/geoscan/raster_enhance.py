"""Visual enhancement for scanned-map backdrops (human eyes only).

The enhanced raster is a VIEWING aid for manual repair in MapGIS; every
vectorization stage keeps reading the untouched frozen raster. All operations
work on the LAB lightness channel with per-pixel / local-neighborhood filters
(illumination flatten, median denoise, CLAHE, unsharp mask) — output
dimensions and pixel geometry are identical to the input, so vector overlay
alignment is unaffected. No content is invented: nothing is drawn, erased, or
reinterpreted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image


Image.MAX_IMAGE_PIXELS = None


@dataclass(frozen=True)
class EnhanceOptions:
    flatten_background: bool = True
    background_sigma_px: float = 200.0
    denoise_median: bool = True
    clahe_clip: float = 2.0
    clahe_tile_px: int = 512
    unsharp_amount: float = 0.8
    unsharp_sigma_px: float = 1.5


ENHANCE_PRESETS: dict[str, EnhanceOptions] = {
    "light": EnhanceOptions(denoise_median=False, clahe_clip=1.4, unsharp_amount=0.5),
    "standard": EnhanceOptions(),
    "strong": EnhanceOptions(clahe_clip=3.0, unsharp_amount=1.2),
}


def _flatten_illumination(channel: np.ndarray, sigma_px: float) -> np.ndarray:
    """Divide out large-scale lighting/yellowing while keeping global brightness."""
    height, width = channel.shape
    # Estimate the background on a small proxy — a full-resolution blur with a
    # 200 px sigma on a 100+ MP scan is prohibitively slow.
    proxy_side = 512
    scale = proxy_side / max(height, width)
    if scale < 1.0:
        proxy = cv2.resize(
            channel,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
        proxy_sigma = max(3.0, sigma_px * scale)
    else:
        proxy = channel
        proxy_sigma = max(3.0, sigma_px)
    background = cv2.GaussianBlur(proxy, (0, 0), proxy_sigma)
    background = cv2.resize(background, (width, height), interpolation=cv2.INTER_LINEAR)
    background = np.maximum(background.astype(np.float32), 1.0)
    ratio = channel.astype(np.float32) / background
    return np.clip(ratio * float(np.median(background)), 0.0, 255.0).astype(np.uint8)


def enhance_rgb_array(rgb: np.ndarray, options: EnhanceOptions) -> np.ndarray:
    """Enhance an RGB uint8 array; shape and dtype are preserved exactly."""
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"expected HxWx3 uint8 RGB array, got {rgb.dtype} {rgb.shape}")
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    channel = lab[:, :, 0]
    height, width = channel.shape

    if options.flatten_background:
        channel = _flatten_illumination(channel, options.background_sigma_px)
    if options.denoise_median:
        channel = cv2.medianBlur(channel, 3)
    if options.clahe_clip > 0:
        tiles = (
            max(2, round(width / options.clahe_tile_px)),
            max(2, round(height / options.clahe_tile_px)),
        )
        clahe = cv2.createCLAHE(clipLimit=options.clahe_clip, tileGridSize=tiles)
        channel = clahe.apply(channel)
    if options.unsharp_amount > 0:
        blur = cv2.GaussianBlur(channel, (0, 0), options.unsharp_sigma_px)
        channel = cv2.addWeighted(
            channel, 1.0 + options.unsharp_amount, blur, -options.unsharp_amount, 0
        )

    lab[:, :, 0] = channel
    enhanced = cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)
    assert enhanced.shape == rgb.shape and enhanced.dtype == rgb.dtype
    return enhanced


def enhance_image_file(
    source: Path,
    target: Path,
    *,
    preset: str = "standard",
    dpi: tuple[float, float] | None = None,
    compression: str = "raw",
) -> dict[str, Any]:
    """Write an enhanced copy of ``source`` to ``target``; source is never modified.

    ``dpi`` defaults to the source's dpi (falling back to 300); pass the
    pixel-unit dpi when the copy must overlay MapGIS pixel-coordinate vectors.
    """
    if preset not in ENHANCE_PRESETS:
        raise ValueError(f"preset must be one of {sorted(ENHANCE_PRESETS)}, got {preset!r}")
    source = Path(source)
    target = Path(target)
    with Image.open(source) as image:
        source_dpi = image.info.get("dpi") or (300.0, 300.0)
        rgb = np.asarray(image.convert("RGB"))
    enhanced = enhance_rgb_array(rgb, ENHANCE_PRESETS[preset])
    output_dpi = dpi if dpi is not None else (float(source_dpi[0]), float(source_dpi[1]))
    target.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(enhanced).save(
        target, format="TIFF", dpi=output_dpi, compression=compression
    )
    return {
        "purpose": "human_viewing_backdrop_only",
        "source": str(source),
        "target": str(target),
        "preset": preset,
        "options": asdict(ENHANCE_PRESETS[preset]),
        "size_px": [int(rgb.shape[1]), int(rgb.shape[0])],
        "output_dpi": [float(output_dpi[0]), float(output_dpi[1])],
        "geometry_changed": False,
        "bytes": target.stat().st_size,
    }
