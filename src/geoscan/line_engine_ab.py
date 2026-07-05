"""Hough vs trace line-engine A/B comparison (analysis-only).

Reads the frozen raster of an existing production run, executes both line
engines on it, and writes candidates + metrics + overlay evidence into
``11_LINE_ENGINE_AB/`` inside the map's output folder. It never touches
``04_LINE_WORKFLOW`` or any production artifact, and it never changes engine
defaults — the output exists so a human can decide whether ``trace`` should
replace ``hough``.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .extract_lines import extract_line_candidates
from .param_workbench import (
    dark_mask,
    distance_to_nonzero,
    length_stats,
    precision_proxy,
    recall_proxy,
    skeletonize,
)
from .raster import load_rgb
from .trace_lines import TraceConfig, extract_traced_line_candidates

AB_DIR_NAME = "11_LINE_ENGINE_AB"
REPORT_NAME = "LINE_ENGINE_AB_REPORT.json"
SUMMARY_NAME = "LINE_ENGINE_AB_SUMMARY.md"
PRODUCTION_MIN_LINE_LENGTH = 130

_KIND_COLORS_BGR = {
    "straight": (0, 200, 0),
    "curve": (0, 140, 255),
    "loop": (255, 0, 200),
    "hough": (0, 0, 255),
}


def _feature_pixel_paths(
    features: list[dict[str, Any]], *, height: int
) -> list[tuple[str, list[tuple[float, float]]]]:
    """LineString features (map coords) -> (kind, [(x, y), ...]) image paths."""
    paths: list[tuple[str, list[tuple[float, float]]]] = []
    for item in features:
        geometry = item.get("geometry") or {}
        if geometry.get("type") != "LineString":
            continue
        coordinates = geometry.get("coordinates") or []
        if len(coordinates) < 2:
            continue
        properties = item.get("properties") or {}
        kind = str(properties.get("trace_kind") or "hough")
        paths.append(
            (kind, [(float(x), float(height) - float(y)) for x, y in coordinates])
        )
    return paths


def _rasterize_paths(
    paths: list[tuple[str, list[tuple[float, float]]]], *, shape: tuple[int, int]
) -> np.ndarray:
    canvas = np.zeros(shape, dtype=np.uint8)
    for _kind, points in paths:
        array = np.asarray(
            [[int(round(x)), int(round(y))] for x, y in points], dtype=np.int32
        ).reshape(-1, 1, 2)
        cv2.polylines(canvas, [array], False, 255, 1)
    return canvas


def _path_length(points: list[tuple[float, float]]) -> float:
    return float(
        sum(
            np.hypot(x2 - x1, y2 - y1)
            for (x1, y1), (x2, y2) in zip(points[:-1], points[1:])
        )
    )


def _render_overlay(
    gray: np.ndarray,
    paths: list[tuple[str, list[tuple[float, float]]]],
    *,
    output_path: Path,
    max_dim: int = 2200,
) -> str:
    height, width = gray.shape
    scale = min(1.0, max_dim / max(height, width))
    small = cv2.resize(
        gray,
        (max(1, int(width * scale)), max(1, int(height * scale))),
        interpolation=cv2.INTER_AREA,
    )
    canvas = cv2.cvtColor(small, cv2.COLOR_GRAY2BGR)
    for kind, points in paths:
        color = _KIND_COLORS_BGR.get(kind, (255, 255, 0))
        array = np.asarray(
            [[int(round(x * scale)), int(round(y * scale))] for x, y in points],
            dtype=np.int32,
        ).reshape(-1, 1, 2)
        cv2.polylines(canvas, [array], False, color, 1)
    cv2.imwrite(str(output_path), canvas)
    return str(output_path)


def _render_crop_pair(
    gray: np.ndarray,
    engine_paths: dict[str, list[tuple[str, list[tuple[float, float]]]]],
    *,
    center: tuple[int, int],
    crop_half: int,
    output_dir: Path,
    tag: str,
) -> dict[str, str]:
    height, width = gray.shape
    cx, cy = center
    left = max(0, min(width - 2 * crop_half, cx - crop_half))
    top = max(0, min(height - 2 * crop_half, cy - crop_half))
    right = min(width, left + 2 * crop_half)
    bottom = min(height, top + 2 * crop_half)
    outputs: dict[str, str] = {}
    for engine, paths in engine_paths.items():
        base = cv2.cvtColor(gray[top:bottom, left:right], cv2.COLOR_GRAY2BGR)
        for kind, points in paths:
            color = _KIND_COLORS_BGR.get(kind, (255, 255, 0))
            array = np.asarray(
                [[int(round(x - left)), int(round(y - top))] for x, y in points],
                dtype=np.int32,
            ).reshape(-1, 1, 2)
            cv2.polylines(base, [array], False, color, 1)
        output_path = output_dir / f"crop_{tag}_{engine}.png"
        cv2.imwrite(str(output_path), base)
        outputs[engine] = str(output_path)
    return outputs


def _curve_density_center(
    paths: list[tuple[str, list[tuple[float, float]]]],
    *,
    shape: tuple[int, int],
    cells: int = 4,
) -> tuple[int, int] | None:
    height, width = shape
    counter: Counter[tuple[int, int]] = Counter()
    for kind, points in paths:
        if kind not in {"curve", "loop"}:
            continue
        for x, y in points:
            cell = (
                min(cells - 1, int(y / height * cells)),
                min(cells - 1, int(x / width * cells)),
            )
            counter[cell] += 1
    if not counter:
        return None
    (row, col), _count = counter.most_common(1)[0]
    return (
        int((col + 0.5) * width / cells),
        int((row + 0.5) * height / cells),
    )


def run_line_engine_ab(
    *,
    output_root: Path,
    map_id: str,
    reset: bool = False,
    max_overlay_dim: int = 2200,
    crop_half: int = 700,
) -> dict[str, Any]:
    output_root = Path(output_root)
    map_key = map_id.lower()
    frozen = output_root / "00_INPUT_FREEZE" / f"{map_key}_source_frozen.tif"
    if not frozen.is_file():
        raise FileNotFoundError(frozen)
    ab_dir = output_root / AB_DIR_NAME
    report_path = ab_dir / REPORT_NAME
    if report_path.exists() and not reset:
        raise FileExistsError(f"{report_path} exists; pass reset=True/--reset to rerun")
    ab_dir.mkdir(parents=True, exist_ok=True)

    rgb = load_rgb(frozen)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    height = rgb.shape[0]

    engines: dict[str, dict[str, Any]] = {}
    engine_paths: dict[str, list[tuple[str, list[tuple[float, float]]]]] = {}

    start = time.perf_counter()
    hough_features = extract_line_candidates(rgb, min_length=PRODUCTION_MIN_LINE_LENGTH)
    hough_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    trace_config = TraceConfig()
    trace_features = extract_traced_line_candidates(rgb, config=trace_config)
    trace_elapsed = time.perf_counter() - start

    ink = dark_mask(gray, gray_threshold=190)
    ink_distance = distance_to_nonzero(ink)
    ink_skeleton = skeletonize(ink)

    for engine, features, elapsed, parameters in (
        (
            "hough",
            hough_features,
            hough_elapsed,
            {"min_line_length": PRODUCTION_MIN_LINE_LENGTH},
        ),
        ("trace", trace_features, trace_elapsed, trace_config.__dict__),
    ):
        paths = _feature_pixel_paths(features, height=height)
        engine_paths[engine] = paths
        candidates_path = ab_dir / f"{engine}_candidates.geojson"
        candidates_path.write_text(
            json.dumps(
                {"type": "FeatureCollection", "features": features},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        line_mask = _rasterize_paths(paths, shape=gray.shape)
        lengths = [_path_length(points) for _kind, points in paths]
        overlay = _render_overlay(
            gray,
            paths,
            output_path=ab_dir / f"overlay_{engine}.png",
            max_dim=max_overlay_dim,
        )
        engines[engine] = {
            "candidate_count": len(features),
            "kind_counts": dict(Counter(kind for kind, _pts in paths)),
            "elapsed_seconds": round(elapsed, 1),
            "parameters": parameters,
            "precision_proxy": precision_proxy(line_mask, ink_distance),
            "recall_proxy": recall_proxy(line_mask, ink_skeleton),
            "length_stats_px": length_stats(lengths),
            "candidates_geojson": str(candidates_path),
            "overlay_png": overlay,
        }

    crops: list[dict[str, Any]] = []
    curve_center = _curve_density_center(engine_paths["trace"], shape=gray.shape)
    if curve_center is not None:
        crops.append(
            {
                "tag": "curve_dense",
                "center_px": list(curve_center),
                "images": _render_crop_pair(
                    gray,
                    engine_paths,
                    center=curve_center,
                    crop_half=crop_half,
                    output_dir=ab_dir,
                    tag="curve_dense",
                ),
            }
        )

    report = {
        "program": "line_engine_ab",
        "map_id": map_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_raster": str(frozen),
        "image_size": [int(rgb.shape[1]), int(rgb.shape[0])],
        "engines": engines,
        "crops": crops,
        "production_default_engine": "hough",
        "default_change_requires_human_review": True,
        "writes_production_artifacts": False,
        "note": (
            "Analysis-only A/B evidence. The production default stays hough until "
            "a human reviews these overlays and approves switching."
        ),
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary_lines = [
        f"# Line Engine A/B — {map_id}",
        "",
        f"Source: `{frozen}`",
        "",
        "| metric | hough | trace |",
        "|---|---|---|",
    ]
    for key, label in (
        ("candidate_count", "candidates"),
        ("precision_proxy", "precision proxy"),
        ("recall_proxy", "recall proxy"),
        ("elapsed_seconds", "elapsed (s)"),
    ):
        summary_lines.append(
            f"| {label} | {engines['hough'][key]} | {engines['trace'][key]} |"
        )
    summary_lines += [
        f"| kinds | {engines['hough']['kind_counts']} | {engines['trace']['kind_counts']} |",
        "",
        "Overlay colors: hough=red; trace straight=green, curve=orange, loop=magenta.",
        "",
        "Decision: production default remains `hough` until a human reviews",
        f"`overlay_hough.png` vs `overlay_trace.png` (and crops) in `{AB_DIR_NAME}/`.",
        "",
    ]
    (ab_dir / SUMMARY_NAME).write_text("\n".join(summary_lines), encoding="utf-8")
    return report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hough vs trace line engine A/B analysis")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run", help="run the A/B comparison")
    run_parser.add_argument("--output-root", required=True)
    run_parser.add_argument("--map-id", required=True)
    run_parser.add_argument("--reset", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "run":
        report = run_line_engine_ab(
            output_root=Path(args.output_root),
            map_id=args.map_id,
            reset=bool(args.reset),
        )
        printable = {
            engine: {
                key: value
                for key, value in stats.items()
                if key in {"candidate_count", "kind_counts", "precision_proxy", "recall_proxy", "elapsed_seconds"}
            }
            for engine, stats in report["engines"].items()
        }
        print(json.dumps(printable, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
