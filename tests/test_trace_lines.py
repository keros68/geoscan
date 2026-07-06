"""zhang_suen_thin bbox-cropping perf change must be output-identical (synthetic only)."""

from __future__ import annotations

import numpy as np
import pytest

from geoscan.trace_lines import zhang_suen_thin


def _neighbor_planes_reference(img: np.ndarray) -> tuple[np.ndarray, ...]:
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


def _zhang_suen_thin_reference(mask: np.ndarray, *, max_iterations: int = 64) -> np.ndarray:
    """Pre-crop implementation of zhang_suen_thin, kept verbatim as an oracle.

    This is a frozen copy of the original (whole-image, uncropped) algorithm
    that lived in src/geoscan/trace_lines.py before the bbox-cropping
    optimization. Do not "fix" it to match the new source - it is the ground
    truth the new implementation is checked against.
    """
    img = mask.astype(bool).copy()
    for _ in range(int(max_iterations)):
        changed = False
        for step in (0, 1):
            p2, p3, p4, p5, p6, p7, p8, p9 = _neighbor_planes_reference(img)
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


def _assert_matches_reference(mask: np.ndarray) -> None:
    expected = _zhang_suen_thin_reference(mask)
    actual = zhang_suen_thin(mask)
    assert actual.dtype == expected.dtype
    assert actual.shape == expected.shape
    assert np.array_equal(actual, expected)


def test_empty_image() -> None:
    mask = np.zeros((60, 80), dtype=bool)
    _assert_matches_reference(mask)


def test_single_pixel() -> None:
    mask = np.zeros((60, 80), dtype=bool)
    mask[30, 40] = True
    _assert_matches_reference(mask)


def test_single_pixel_at_corner() -> None:
    mask = np.zeros((60, 80), dtype=bool)
    mask[0, 0] = True
    _assert_matches_reference(mask)


def test_random_blobs() -> None:
    rng = np.random.default_rng(12345)
    mask = rng.random((100, 120)) < 0.35
    _assert_matches_reference(mask)


def test_random_sparse_blobs() -> None:
    rng = np.random.default_rng(999)
    mask = np.zeros((150, 150), dtype=bool)
    for _ in range(15):
        cy, cx = rng.integers(10, 140, size=2)
        r = rng.integers(2, 8)
        yy, xx = np.ogrid[:150, :150]
        mask |= (yy - cy) ** 2 + (xx - cx) ** 2 <= r * r
    _assert_matches_reference(mask)


def test_thick_straight_horizontal_line() -> None:
    mask = np.zeros((60, 200), dtype=bool)
    mask[25:35, 20:180] = True
    _assert_matches_reference(mask)


def test_thick_straight_vertical_line() -> None:
    mask = np.zeros((200, 60), dtype=bool)
    mask[20:180, 25:35] = True
    _assert_matches_reference(mask)


def test_thick_diagonal_line() -> None:
    mask = np.zeros((120, 120), dtype=bool)
    for i in range(100):
        y = 10 + i
        x = 10 + i
        mask[y - 3 : y + 4, x - 3 : x + 4] = True
    _assert_matches_reference(mask)


def test_shape_touching_top_left_border() -> None:
    mask = np.zeros((80, 100), dtype=bool)
    mask[0:20, 0:30] = True
    _assert_matches_reference(mask)


def test_shape_touching_bottom_right_border() -> None:
    mask = np.zeros((80, 100), dtype=bool)
    mask[60:80, 70:100] = True
    _assert_matches_reference(mask)


def test_shape_touching_all_four_borders() -> None:
    mask = np.zeros((80, 100), dtype=bool)
    mask[:, 40:60] = True  # spans full height
    mask[30:50, :] = True  # spans full width
    _assert_matches_reference(mask)


def test_full_frame_border_rectangle() -> None:
    mask = np.zeros((70, 90), dtype=bool)
    mask[0, :] = True
    mask[-1, :] = True
    mask[:, 0] = True
    mask[:, -1] = True
    _assert_matches_reference(mask)


def test_full_frame_filled_rectangle() -> None:
    mask = np.ones((50, 50), dtype=bool)
    _assert_matches_reference(mask)


def test_thick_loop_shape() -> None:
    mask = np.zeros((100, 100), dtype=bool)
    yy, xx = np.ogrid[:100, :100]
    dist = np.sqrt((yy - 50) ** 2 + (xx - 50) ** 2)
    mask |= (dist >= 25) & (dist <= 32)
    _assert_matches_reference(mask)


@pytest.mark.parametrize("max_iterations", [0, 1, 3])
def test_low_iteration_cap_matches_reference(max_iterations: int) -> None:
    rng = np.random.default_rng(42)
    mask = rng.random((90, 90)) < 0.3
    expected = _zhang_suen_thin_reference(mask, max_iterations=max_iterations)
    actual = zhang_suen_thin(mask, max_iterations=max_iterations)
    assert np.array_equal(actual, expected)
