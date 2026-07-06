from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import cv2

from geoscan.candidates import write_geojson
from geoscan.extract_lines import extract_line_candidates
from geoscan.line_connectivity import (
    apply_connectivity_overrides,
    bridge_line_candidates,
    close_nearly_closed_polylines,
    regularize_small_boxes,
    resolve_connectivity_profile,
)
from geoscan.raster import load_rgb

VALID_LINE_ENGINES = {"hough", "trace"}


def generate_review_line_candidates(
    *,
    source_raster: Path,
    output_root: Path,
    map_id: str,
    min_line_length: int = 130,
    engine: str = "hough",
    connectivity: str = "conservative",
    bridge_gap_px: float | None = None,
    close_gap_px: float | None = None,
) -> dict[str, Any]:
    """Generate review-only WL line candidates from the frozen input image.

    ``engine="hough"`` (production default) keeps the deterministic straight-line
    HoughLinesP extraction byte-identical to previous runs. ``engine="trace"``
    uses the centerline-tracing vectorizer (straight + curve + loop candidates);
    it stays opt-in until side-by-side overlay evidence justifies switching.

    ``connectivity`` picks a ConnectivityProfile ("conservative" reproduces the
    historical behavior exactly). "standard"/"aggressive" let the engines jump
    larger breaks and add a deterministic ink-evidence bridging pass that
    reconnects endpoints where the line visibly continues on the raster.

    ``bridge_gap_px`` / ``close_gap_px`` are optional per-run numeric overrides
    on top of the level (None = level default, 0 = off).
    """
    if engine not in VALID_LINE_ENGINES:
        raise ValueError(f"engine must be one of {sorted(VALID_LINE_ENGINES)}")
    profile = apply_connectivity_overrides(
        resolve_connectivity_profile(connectivity),
        bridge_gap_px=bridge_gap_px,
        close_gap_px=close_gap_px,
    )
    line_dir = Path(output_root) / "04_LINE_WORKFLOW"
    output_geojson = line_dir / f"{map_id}_review_line_candidates.geojson"
    report_path = line_dir / "LINE_CANDIDATE_GENERATION_REPORT.json"

    rgb = load_rgb(Path(source_raster))
    engine_parameters: dict[str, Any]
    if engine == "trace":
        from geoscan.trace_lines import (
            TraceConfig,
            extract_traced_line_candidates,
        )

        trace_config = TraceConfig(close_kernel_px=profile.trace_close_kernel_px)
        features = extract_traced_line_candidates(rgb, config=trace_config)
        engine_parameters = asdict(trace_config)
        note = (
            "Review-only WL line candidates generated from deterministic "
            "centerline tracing (straight + curve + loop)."
        )
    else:
        features = extract_line_candidates(
            rgb,
            min_length=min_line_length,
            max_line_gap=profile.hough_max_line_gap,
        )
        engine_parameters = {
            "min_line_length": int(min_line_length),
            "max_line_gap": int(profile.hough_max_line_gap),
        }
        note = "Review-only WL line candidates generated from deterministic Hough line extraction."

    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # One lazily-built dark-proximity mask per (window, threshold) actually
    # sampled, shared by the box and bridge passes (identical results, far
    # fewer raster reads).
    dark_masks: dict[tuple[int, int], Any] = {}
    # Raster-evidence box regularization first: broken legend-box strokes are
    # replaced by one clean rectangle each and parked in a sidecar file, so
    # the closure/bridge passes below never waste work on those fragments.
    features, box_rectangles, box_superseded, box_report = regularize_small_boxes(
        features, gray, profile=profile, dark_masks=dark_masks
    )
    if box_superseded:
        write_geojson(
            line_dir / f"{map_id}_small_box_replaced_lines.geojson", box_superseded
        )
    # Same-feature ring gaps first, then cross-feature bridging; the two
    # passes are disjoint by construction (see close_nearly_closed_polylines).
    closure_features, closure_report = close_nearly_closed_polylines(
        features, profile=profile
    )
    bridge_features, bridge_report = bridge_line_candidates(
        features, gray, profile=profile, dark_masks=dark_masks
    )
    features = features + box_rectangles + closure_features + bridge_features
    write_geojson(output_geojson, features)

    report = {
        "mode": "auto",
        "ok": True,
        "engine": engine,
        "engine_parameters": engine_parameters,
        "connectivity": profile.name,
        "connectivity_parameters": asdict(profile),
        "bridge": bridge_report,
        "closure": closure_report,
        "small_box": box_report,
        "source_raster": str(source_raster),
        "output_geojson": str(output_geojson),
        "feature_count": len(features),
        "min_line_length": int(min_line_length),
        "writes_checked_yes": False,
        "note": note,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
