"""Conservative repaired-line-candidate stage.

Reads the immutable raw Hough candidate layer and writes a NEW file
``{map_id}_repaired_line_candidates.geojson`` next to it. The raw file is
never modified or overwritten.

Scope (deliberately conservative):

- orientation-aware collinear merging (small-gap merge always; full-span
  major-axis bridging only with strict, image-size-scaled thresholds);
- four-side-evidence rectangle closure only (``min_present_sides=4``);
- NO whole-map three-side closure, NO small-box detection — those need
  region-scoped passes with separately calibrated parameters;
- every output feature carries ``source`` / ``repair_method`` /
  ``confidence`` / ``needs_review``; nothing is marked ``checked=yes``;
- the repaired layer does NOT enter production export by default; exports
  must opt in explicitly (see production_program ``--line-export-source``).
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from .candidates import feature, write_geojson
from .line_repair import (
    Segment,
    _axis_item,
    close_axis_aligned_rectangles,
    find_axis_aligned_rectangle_closures,
    repair_major_axis_segments,
)

REPORT_NAME = "LINE_REPAIR_REPORT.json"


@dataclass(frozen=True)
class RepairStageConfig:
    axis_tolerance: float = 2.0
    small_gap_tolerance: float = 16.0
    min_major_segments: int = 3
    # Major-axis full-span bridging is only allowed for lines that plausibly
    # are the map frame / full-width table borders. Fractions of image size,
    # NOT the tiny absolute defaults from RegularizerConfig (those were tuned
    # on small pilot crops and would over-bridge a 10k-px production map).
    min_major_span_fraction: float = 0.55
    min_major_total_fraction: float = 0.6
    closure_corner_tolerance: float = 6.0
    closure_min_side: float = 40.0
    closure_min_side_coverage: float = 0.7
    merged_confidence: float = 0.6
    closure_confidence: float = 0.45


def _segment_key(segment: Segment) -> tuple[tuple[float, float], tuple[float, float]]:
    a = (round(segment[0][0], 3), round(segment[0][1], 3))
    b = (round(segment[1][0], 3), round(segment[1][1], 3))
    return (a, b) if a <= b else (b, a)


def _segment_length(segment: Segment) -> float:
    return math.hypot(segment[1][0] - segment[0][0], segment[1][1] - segment[0][1])


def _two_point_segment(item: dict[str, Any]) -> Segment | None:
    geometry = item.get("geometry") or {}
    if geometry.get("type") != "LineString":
        return None
    coordinates = geometry.get("coordinates") or []
    if len(coordinates) != 2:
        return None
    (x1, y1), (x2, y2) = coordinates
    return ((float(x1), float(y1)), (float(x2), float(y2)))


def _split_by_orientation(
    segments: list[Segment], *, axis_tolerance: float
) -> tuple[list[Segment], list[Segment], list[Segment]]:
    horizontal: list[Segment] = []
    vertical: list[Segment] = []
    other: list[Segment] = []
    for segment in segments:
        item = _axis_item(segment, axis_tolerance=axis_tolerance)
        if item is None:
            other.append(segment)
        elif item[0] == "h":
            horizontal.append(segment)
        else:
            vertical.append(segment)
    return horizontal, vertical, other


def _run_axis_merge(
    segments: list[Segment],
    *,
    config: RepairStageConfig,
    span_basis: float,
    enable_major: bool,
) -> list[Segment]:
    if not segments:
        return []
    min_span = config.min_major_span_fraction * span_basis
    return repair_major_axis_segments(
        segments,
        axis_tolerance=config.axis_tolerance,
        small_gap_tolerance=config.small_gap_tolerance,
        min_major_segments=config.min_major_segments if enable_major else 10**9,
        min_major_span=min_span if enable_major else float("inf"),
        min_major_total_length=(
            config.min_major_total_fraction * min_span if enable_major else float("inf")
        ),
    )


def _repaired_feature(
    segment: Segment, *, repair_method: str, confidence: float, note: str
) -> dict[str, Any]:
    return feature(
        geometry={
            "type": "LineString",
            "coordinates": [list(segment[0]), list(segment[1])],
        },
        target="WL",
        cad_layer="AUTO_REPAIR_LINE",
        feature_name="repaired_line",
        source="repair",
        confidence=confidence,
        note=note,
        mapgis_no=10,
        extra={
            "repair_method": repair_method,
            "needs_review": True,
            "length_px": round(_segment_length(segment), 2),
        },
    )


def _render_repair_overlay(
    *,
    source_raster: Path,
    raw_segments: list[Segment],
    repaired_features: list[dict[str, Any]],
    overlay_path: Path,
    max_dim: int = 1800,
) -> str | None:
    """Full-map QA overlay: raw in red, repaired result in green (drawn on top)."""
    try:
        from .raster import load_rgb

        rgb = load_rgb(source_raster)
    except (FileNotFoundError, OSError):
        return None
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    scale = min(1.0, max_dim / max(height, width))
    small = cv2.resize(
        gray, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation=cv2.INTER_AREA
    )
    canvas = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)

    def draw(segment: Segment, color: tuple[int, int, int]) -> None:
        (x1, y1), (x2, y2) = segment
        cv2.line(
            canvas,
            (int(round(x1 * scale)), int(round((height - y1) * scale))),
            (int(round(x2 * scale)), int(round((height - y2) * scale))),
            color,
            1,
        )

    for segment in raw_segments:
        draw(segment, (0, 0, 255))
    for item in repaired_features:
        segment = _two_point_segment(item)
        if segment is not None:
            draw(segment, (0, 200, 0))
    cv2.imwrite(str(overlay_path), canvas)
    return str(overlay_path)


def generate_repaired_line_candidates(
    *,
    output_root: Path,
    map_id: str,
    config: RepairStageConfig | None = None,
    image_width: float | None = None,
    image_height: float | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    """Produce the repaired candidate layer as a NEW file; raw stays untouched."""
    config = config or RepairStageConfig()
    output_root = Path(output_root)
    map_key = map_id.lower()
    line_dir = output_root / "04_LINE_WORKFLOW"
    raw_path = line_dir / f"{map_key}_review_line_candidates.geojson"
    repaired_path = line_dir / f"{map_key}_repaired_line_candidates.geojson"
    report_path = line_dir / REPORT_NAME
    if not raw_path.is_file():
        raise FileNotFoundError(raw_path)
    if repaired_path == raw_path:  # defensive; names differ by construction
        raise ValueError("repaired output must not overwrite the raw candidate layer")
    if repaired_path.exists() and not reset:
        raise FileExistsError(
            f"{repaired_path} already exists; pass reset=True/--reset to regenerate"
        )

    raw_bytes = raw_path.read_bytes()
    payload = json.loads(raw_bytes.decode("utf-8"))
    raw_features = list(payload.get("features") or [])

    segments: list[Segment] = []
    segment_features: list[dict[str, Any]] = []
    untouched_features: list[dict[str, Any]] = []
    for item in raw_features:
        segment = _two_point_segment(item)
        if segment is None:
            untouched_features.append(item)
        else:
            segments.append(segment)
            segment_features.append(item)

    if image_width is None or image_height is None:
        report_file = output_root / "PROGRAM_RUN_REPORT.json"
        if report_file.is_file():
            run_report = json.loads(report_file.read_text(encoding="utf-8"))
            image_width = image_width or float(run_report.get("input", {}).get("width") or 0)
            image_height = image_height or float(run_report.get("input", {}).get("height") or 0)
    if not image_width or not image_height:
        xs = [x for seg in segments for x, _y in seg]
        ys = [y for seg in segments for _x, y in seg]
        image_width = image_width or (max(xs) - min(xs) if xs else 0.0)
        image_height = image_height or (max(ys) - min(ys) if ys else 0.0)

    raw_keys = {_segment_key(segment) for segment in segments}
    raw_by_key: dict[Any, dict[str, Any]] = {}
    for segment, item in zip(segments, segment_features):
        raw_by_key.setdefault(_segment_key(segment), item)

    horizontal, vertical, other = _split_by_orientation(
        segments, axis_tolerance=config.axis_tolerance
    )

    # Small-gap-only pass first, to distinguish merge provenance.
    small_only = (
        _run_axis_merge(horizontal, config=config, span_basis=image_width, enable_major=False)
        + _run_axis_merge(vertical, config=config, span_basis=image_height, enable_major=False)
    )
    small_only_keys = {_segment_key(segment) for segment in small_only}

    merged = (
        _run_axis_merge(horizontal, config=config, span_basis=image_width, enable_major=True)
        + _run_axis_merge(vertical, config=config, span_basis=image_height, enable_major=True)
        + other
    )

    closure_kwargs = dict(
        axis_tolerance=config.axis_tolerance,
        corner_tolerance=config.closure_corner_tolerance,
        min_width=config.closure_min_side,
        min_height=config.closure_min_side,
        min_side_coverage=config.closure_min_side_coverage,
        min_present_sides=4,
    )
    closures = find_axis_aligned_rectangle_closures(merged, **closure_kwargs)
    closed = close_axis_aligned_rectangles(merged, closures=closures, **closure_kwargs)
    merged_keys = {_segment_key(segment) for segment in merged}

    counts = {
        "passthrough": 0,
        "small_gap_merged": 0,
        "major_axis_bridged": 0,
        "closure_regularized": 0,
        "untouched_non_two_point": len(untouched_features),
    }
    def _passthrough_clone(item: dict[str, Any], method: str) -> dict[str, Any]:
        # Shallow rebuild with a fresh properties dict: only the properties are
        # annotated, so the (potentially large) geometry is shared by reference
        # instead of a full JSON round trip per feature.
        properties = dict(item.get("properties") or {})
        properties["repair_method"] = method
        properties["needs_review"] = False
        return {**item, "properties": properties}

    output_features: list[dict[str, Any]] = []
    for item in untouched_features:
        output_features.append(_passthrough_clone(item, "passthrough_untouched"))

    for segment in closed:
        key = _segment_key(segment)
        if key in raw_keys:
            output_features.append(_passthrough_clone(raw_by_key[key], "passthrough"))
            counts["passthrough"] += 1
        elif key in merged_keys:
            if key in small_only_keys:
                method = "axis_small_gap_merge"
                counts["small_gap_merged"] += 1
            else:
                method = "major_axis_bridge"
                counts["major_axis_bridged"] += 1
            output_features.append(
                _repaired_feature(
                    segment,
                    repair_method=method,
                    confidence=config.merged_confidence,
                    note="共线合并候选；需人工复核是否为同一条真实图线。",
                )
            )
        else:
            output_features.append(
                _repaired_feature(
                    segment,
                    repair_method="rectangle_closure_regularize",
                    confidence=config.closure_confidence,
                    note="四边证据矩形规整候选；需人工复核矩形闭合是否成立。",
                )
            )
            counts["closure_regularized"] += 1

    for index, item in enumerate(output_features, start=1):
        item.setdefault("properties", {})["candidate_id"] = f"RL_{index:04d}"

    write_geojson(repaired_path, output_features)
    assert raw_path.read_bytes() == raw_bytes, "raw candidate layer must stay untouched"

    overlay = _render_repair_overlay(
        source_raster=output_root / "00_INPUT_FREEZE" / f"{map_key}_source_frozen.tif",
        raw_segments=segments,
        repaired_features=output_features,
        overlay_path=line_dir / "LINE_REPAIR_OVERLAY.png",
    )

    report = {
        "program": "line_repair_stage",
        "map_id": map_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "raw_geojson": str(raw_path),
        "repaired_geojson": str(repaired_path),
        "raw_preserved": True,
        "qa_overlay": overlay,
        "raw_feature_count": len(raw_features),
        "repaired_feature_count": len(output_features),
        "image_size_used": [image_width, image_height],
        "config": asdict(config),
        "effective_min_major_span": {
            "horizontal_px": round(config.min_major_span_fraction * image_width, 1),
            "vertical_px": round(config.min_major_span_fraction * image_height, 1),
        },
        "counts": counts,
        "closures": [
            {
                "bbox": list(closure["bbox"]),
                "side_coverages": closure["side_coverages"],
                "synthesized_sides": list(closure["synthesized_sides"]),
            }
            for closure in closures
        ],
        "three_side_closure_enabled": False,
        "small_box_pass_enabled": False,
        "writes_checked_yes": False,
        "production_export_default": "raw",
        "note": (
            "Repaired layer is a review product. It enters DXF/native WL export "
            "only via an explicit --line-export-source repaired selection."
        ),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Conservative repaired line candidate stage")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="generate repaired candidates")
    run_parser.add_argument("--output-root", required=True)
    run_parser.add_argument("--map-id", required=True)
    run_parser.add_argument("--reset", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "run":
        report = generate_repaired_line_candidates(
            output_root=Path(args.output_root),
            map_id=args.map_id,
            reset=bool(args.reset),
        )
        print(json.dumps({"counts": report["counts"], "closures": len(report["closures"])}, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
