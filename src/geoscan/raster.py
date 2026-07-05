from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def image_point_to_map_point(x: float, y: float, *, height: int) -> list[float]:
    return [round(float(x), 6), round(float(height) - float(y), 6)]


def load_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"))


def rgb_to_bgr(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
