"""Connectivity profiles + deterministic ink-evidence bridging (synthetic only)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from PIL import Image

from geoscan.ai_enhance import AiEnhanceThresholds, validate_bridge_op
from geoscan.line_candidate_workflow import generate_review_line_candidates
from geoscan.line_connectivity import (
    CONNECTIVITY_PROFILES,
    bridge_line_candidates,
    resolve_connectivity_profile,
)
from geoscan.trace_lines import TraceConfig, extract_traced_line_candidates

HEIGHT = 200
WIDTH = 400


def _line_feature(coords: list[list[float]]) -> dict:
    return {
        "type": "Feature",
        "properties": {},
        "geometry": {"type": "LineString", "coordinates": coords},
    }


def _white() -> np.ndarray:
    return np.full((HEIGHT, WIDTH), 255, dtype=np.uint8)


def _collinear_pair_with_gap() -> tuple[list[dict], np.ndarray]:
    """Two collinear horizontal candidates, 50 px apart; ink spans the gap.

    Ink lives at pixel row 100; candidates are in map coords (y flipped),
    so map_y = HEIGHT - 100 = 100 as well — but the regression test below
    uses an asymmetric row to catch mirrored sampling.
    """
    gray = _white()
    gray[99:102, 50:351] = 0
    features = [
        _line_feature([[50, 100], [180, 100]]),
        _line_feature([[230, 100], [350, 100]]),
    ]
    return features, gray


def test_conservative_profile_matches_historical_behavior() -> None:
    profile = CONNECTIVITY_PROFILES["conservative"]
    assert profile.hough_max_line_gap == 8
    assert profile.trace_close_kernel_px == 0
    assert profile.bridge_enabled is False
    assert profile.repair_small_gap_tolerance == 16.0
    assert profile.ai_max_gap_px == 60.0
    assert profile.ai_min_dark_coverage == 0.55


def test_resolve_profile_rejects_unknown_name() -> None:
    with pytest.raises(ValueError):
        resolve_connectivity_profile("maximum")


def test_bridge_accepts_collinear_gap_with_ink() -> None:
    features, gray = _collinear_pair_with_gap()
    profile = resolve_connectivity_profile("standard")
    bridges, report = bridge_line_candidates(features, gray, profile=profile)
    assert report["accepted"] == 1
    assert len(bridges) == 1
    bridge = bridges[0]
    coords = {tuple(point) for point in bridge["geometry"]["coordinates"]}
    assert coords == {(180.0, 100.0), (230.0, 100.0)}
    props = bridge["properties"]
    assert props["checked"] == "no"
    assert props["source"] == "auto_bridge"
    assert props["bridge_gap_px"] == pytest.approx(50.0)
    assert props["bridge_dark_coverage"] >= 0.9


def test_bridge_rejects_gap_without_ink() -> None:
    features, _ = _collinear_pair_with_gap()
    gray = _white()  # no ink anywhere: the "line" does not exist on the map
    profile = resolve_connectivity_profile("standard")
    bridges, report = bridge_line_candidates(features, gray, profile=profile)
    assert bridges == []
    assert report["rejected_no_ink"] == 1


def test_bridge_rejects_sideways_jump_between_parallel_lines() -> None:
    # Fully inked image: only the angle gate can reject.
    gray = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    features = [
        _line_feature([[50, 100], [180, 100]]),
        _line_feature([[200, 120], [330, 120]]),
    ]
    profile = resolve_connectivity_profile("standard")
    bridges, report = bridge_line_candidates(features, gray, profile=profile)
    assert bridges == []
    assert report["rejected_angle"] >= 1


def test_dark_coverage_mask_fast_path_matches_loop_exactly() -> None:
    """Property test: the precomputed dark-proximity mask path of
    dark_coverage_px must reproduce the per-sample window-slicing loop
    EXACTLY — including image borders (cv2.dilate border handling) and
    sample points outside the raster (clamped windows can still overlap)."""
    from geoscan.line_connectivity import dark_coverage_px, dark_proximity_mask

    rng = np.random.default_rng(20260706)
    height, width = 60, 80
    for image_index in range(20):
        # Values concentrated around the 140 threshold so single-pixel
        # differences flip individual samples; plus solid white/black patches.
        gray = rng.integers(132, 150, size=(height, width), dtype=np.uint8)
        gray[10:20, 0:30] = 255
        gray[30:34, 40:80] = 0
        segments = [
            ((0.0, 0.0), (width - 1.0, 0.0)),  # along the top border
            ((0.0, height - 1.0), (width - 1.0, height - 1.0)),  # bottom border
            ((-6.0, -4.0), (width + 5.0, height + 3.0)),  # diagonal, ends outside
            ((-1.0, 5.0), (-1.0, height - 5.0)),  # just outside: window still overlaps
            ((-30.0, -30.0), (-10.0, -40.0)),  # fully outside the raster
            ((10.5, 20.5), (30.5, 20.5)),  # .5 coords: rounding ties
            ((12.0, 12.0), (12.0, 12.0)),  # zero-length gap
        ]
        for _ in range(6):
            segments.append(
                (
                    (float(rng.uniform(-10, width + 10)), float(rng.uniform(-10, height + 10))),
                    (float(rng.uniform(-10, width + 10)), float(rng.uniform(-10, height + 10))),
                )
            )
        for window, threshold in ((2, 140), (3, 140), (2, 100)):
            mask = dark_proximity_mask(gray, window=window, threshold=threshold)
            for a, b in segments:
                slow = dark_coverage_px(gray, a, b, dark_threshold=threshold, window=window)
                fast = dark_coverage_px(
                    gray, a, b, dark_threshold=threshold, window=window, dark_mask=mask
                )
                assert fast == slow, (image_index, window, threshold, a, b)


def test_conservative_profile_never_bridges() -> None:
    features, gray = _collinear_pair_with_gap()
    profile = resolve_connectivity_profile("conservative")
    bridges, report = bridge_line_candidates(features, gray, profile=profile)
    assert bridges == []
    assert report["enabled"] is False


def test_trace_close_kernel_repairs_small_break() -> None:
    rgb = np.full((120, 300, 3), 255, dtype=np.uint8)
    rgb[59:62, 20:150] = 0
    rgb[59:62, 152:280] = 0  # 2 px scan break at x=150..151

    broken = extract_traced_line_candidates(rgb, config=TraceConfig())
    closed = extract_traced_line_candidates(
        rgb, config=TraceConfig(close_kernel_px=3)
    )
    assert len(broken) == 2
    assert len(closed) == 1


def test_ai_enhance_bridge_samples_pixel_rows_not_map_rows() -> None:
    """Regression: candidates are map coords (y flipped); ink evidence must be
    read at pixel row height - map_y, not at the mirrored map row."""
    gray = _white()
    gray[19:22, 50:351] = 0  # ink at pixel row 20 -> map_y = 180
    features_by_id = {
        "A": _line_feature([[50, 180], [180, 180]]),
        "B": _line_feature([[230, 180], [350, 180]]),
    }
    result = validate_bridge_op(
        {
            "op": "bridge_gap",
            "candidate_a": "A",
            "candidate_b": "B",
            "endpoint_a": "end",
            "endpoint_b": "start",
        },
        features_by_id=features_by_id,
        gray=gray,
        thresholds=AiEnhanceThresholds(),
    )
    assert result["accepted"] is True
    assert result["dark_coverage"] >= 0.9


def test_workflow_reports_connectivity_and_wires_hough_gap(tmp_path) -> None:
    rgb = np.full((200, 500, 3), 255, dtype=np.uint8)
    rgb[99:102, 60:460] = 0
    raster = tmp_path / "synthetic.tif"
    Image.fromarray(rgb).save(raster)

    report = generate_review_line_candidates(
        source_raster=raster,
        output_root=tmp_path / "out",
        map_id="T99_TEST",
        engine="hough",
        connectivity="standard",
    )
    assert report["connectivity"] == "standard"
    assert report["engine_parameters"]["max_line_gap"] == 15
    assert report["bridge"]["enabled"] is True
    payload = json.loads(
        (tmp_path / "out" / "04_LINE_WORKFLOW" / "T99_TEST_review_line_candidates.geojson")
        .read_text(encoding="utf-8")
    )
    assert payload["features"], "hough should extract the synthetic line"

    with pytest.raises(ValueError):
        generate_review_line_candidates(
            source_raster=raster,
            output_root=tmp_path / "out2",
            map_id="T99_TEST",
            engine="hough",
            connectivity="max",
        )


def test_close_nearly_closed_polyline_gets_closing_segment() -> None:
    from geoscan.line_connectivity import close_nearly_closed_polylines

    # Open square: perimeter ~154 px, corner gap 6 px (ratio ~0.04).
    ring = _line_feature(
        [[50, 50], [90, 50], [90, 90], [50, 90], [50, 56]]
    )
    profile = resolve_connectivity_profile("standard")
    closures, report = close_nearly_closed_polylines([ring], profile=profile)
    assert report["accepted"] == 1
    assert len(closures) == 1
    coords = closures[0]["geometry"]["coordinates"]
    assert coords == [[50.0, 56.0], [50.0, 50.0]]
    props = closures[0]["properties"]
    assert props["checked"] == "no"
    assert props["source"] == "auto_close"
    assert props["close_gap_px"] == pytest.approx(6.0)


def test_close_rejects_u_shape_and_conservative_level() -> None:
    from geoscan.line_connectivity import close_nearly_closed_polylines

    # U-shape: the open side is a real opening (~33% of path), not a worn corner.
    u_shape = _line_feature([[50, 50], [90, 50], [90, 90], [50, 90]])
    standard = resolve_connectivity_profile("standard")
    closures, report = close_nearly_closed_polylines([u_shape], profile=standard)
    assert closures == []

    ring = _line_feature([[50, 50], [90, 50], [90, 90], [50, 90], [50, 56]])
    conservative = resolve_connectivity_profile("conservative")
    closures, report = close_nearly_closed_polylines([ring], profile=conservative)
    assert closures == []
    assert report["enabled"] is False


def test_overrides_enable_and_disable_passes() -> None:
    from geoscan.line_connectivity import apply_connectivity_overrides

    features, gray = _collinear_pair_with_gap()

    # Conservative + explicit bridge gap: bridging turns ON.
    enabled = apply_connectivity_overrides(
        resolve_connectivity_profile("conservative"), bridge_gap_px=60.0
    )
    bridges, _ = bridge_line_candidates(features, gray, profile=enabled)
    assert len(bridges) == 1

    # Standard + 0: bridging turns OFF.
    disabled = apply_connectivity_overrides(
        resolve_connectivity_profile("standard"), bridge_gap_px=0.0
    )
    bridges, report = bridge_line_candidates(features, gray, profile=disabled)
    assert bridges == []
    assert report["enabled"] is False

    # Close override flows into the profile value.
    closed = apply_connectivity_overrides(
        resolve_connectivity_profile("conservative"), close_gap_px=15.0
    )
    assert closed.close_max_gap_px == 15.0


def test_text_interference_lines_split_out_of_main_export() -> None:
    from geoscan.text_interference import (
        TEXT_INTERFERENCE_LAYER,
        split_text_interference_lines,
    )

    image_height = 200.0
    # Text bbox in PIXEL coords: x 100..160, y 40..80 -> map y 120..160.
    text_feature = {
        "type": "Feature",
        "properties": {
            "bbox_left_px": 100,
            "bbox_top_px": 40,
            "bbox_right_px": 160,
            "bbox_bottom_px": 80,
        },
        "geometry": {"type": "Point", "coordinates": [130, 140]},
    }
    glyph_stroke = _line_feature([[110, 130], [150, 130], [150, 150]])
    real_line = _line_feature([[20, 140], [380, 140]])  # crosses the box

    kept, removed, report = split_text_interference_lines(
        [glyph_stroke, real_line], [text_feature], image_height=image_height
    )
    assert report["flagged_count"] == 1
    assert len(removed) == 1 and len(kept) == 1
    assert removed[0]["properties"]["cad_layer"] == TEXT_INTERFERENCE_LAYER
    assert removed[0]["properties"]["object_class"] == "text_interference"
    # The through-line keeps its endpoints outside the box -> stays in main.
    assert kept[0]["geometry"]["coordinates"][0] == [20, 140]


def test_small_box_regularization_replaces_broken_strokes() -> None:
    from geoscan.line_connectivity import regularize_small_boxes

    # Box at px (100,60)-(130,84), 2 px walls, right side only half present.
    gray = _white()
    gray[60:62, 100:131] = 0    # top
    gray[82:84, 100:131] = 0    # bottom
    gray[60:84, 100:102] = 0    # left
    gray[60:72, 128:131] = 0    # right (partial -> only 3 full sides)
    # Broken stroke fragments the tracer would have produced, in map coords
    # (map_y = HEIGHT - y_px; box spans map y 116..140).
    fragment = _line_feature([[101, 117], [101, 139], [129, 139]])
    outside_line = _line_feature([[200, 50], [350, 50]])

    profile = resolve_connectivity_profile("standard")
    kept, rectangles, superseded, report = regularize_small_boxes(
        [fragment, outside_line], gray, profile=profile
    )
    assert report["rectangles"] == 1
    assert len(rectangles) == 1
    ring = rectangles[0]["geometry"]["coordinates"]
    assert ring[0] == ring[-1] and len(ring) == 5  # closed rectangle
    assert rectangles[0]["properties"]["checked"] == "no"
    assert superseded == [fragment]
    assert kept == [outside_line]

    conservative = resolve_connectivity_profile("conservative")
    kept, rectangles, superseded, report = regularize_small_boxes(
        [fragment], gray, profile=conservative
    )
    assert rectangles == [] and superseded == [] and kept == [fragment]
    assert report["enabled"] is False


def test_w60_shape_to_wp_validates_without_launching(tmp_path) -> None:
    from geoscan.mapgis67_bridge import w60_shape_to_wp

    missing = w60_shape_to_wp(
        shp_path=tmp_path / "absent.shp", target_wp=tmp_path / "OUT.WP"
    )
    assert missing["ok"] is False
    assert missing["status"] == "missing_shp"

    shp = tmp_path / "area.shp"
    shp.write_bytes(b"stub shapefile bytes")
    dry = w60_shape_to_wp(
        shp_path=shp,
        target_wp=tmp_path / "OUT.WP",
        dry_run=True,
        report_path=tmp_path / "wp_report.json",
    )
    assert dry["status"] == "dry_run"
    assert dry["conversion_started"] is False
    assert (tmp_path / "wp_report.json").is_file()


def test_dxf_label_size_converts_mm_to_pixels() -> None:
    from geoscan.production_accuracy_workflow import _placeholder_feature

    placeholder, _ = _placeholder_feature(
        {
            "type": "Feature",
            "properties": {"ocr_text": "比例尺"},
            "geometry": {"type": "Point", "coordinates": [100, 100]},
        },
        index=1,
        map_id="T99_TEST",
        target_file="T99TXT.WT",
    )
    style = placeholder["properties"]["OGR_STYLE"]
    # 1.8 mm at 300 dpi = 1.8 * 300 / 25.4 = 21.26 px, not 1.8 px.
    assert "s:21.259843g" in style
