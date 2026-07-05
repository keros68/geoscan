from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from .candidates import feature_collection, write_geojson
from .extract_areas import extract_color_area_candidates
from .extract_lines import extract_line_candidates
from .extract_text import extract_text_region_candidates
from .raster import load_rgb


def extract_candidates(
    rgb: np.ndarray,
    *,
    min_line_length: int = 80,
    min_area: int = 250,
    text_crop_dir: Path | None = None,
) -> dict:
    features = []
    features.extend(extract_line_candidates(rgb, min_length=min_line_length))
    features.extend(extract_color_area_candidates(rgb, min_area=min_area))
    if text_crop_dir is not None:
        features.extend(extract_text_region_candidates(rgb, crop_dir=text_crop_dir))
    return feature_collection(features)


def run(
    input_path: Path,
    output_path: Path,
    *,
    min_line_length: int,
    min_area: int,
    text_crop_dir: Path | None = None,
) -> dict:
    rgb = load_rgb(input_path)
    payload = extract_candidates(
        rgb,
        min_line_length=min_line_length,
        min_area=min_area,
        text_crop_dir=text_crop_dir,
    )
    write_geojson(output_path, payload["features"])
    return {
        "input": str(input_path),
        "output": str(output_path),
        "features": len(payload["features"]),
        "line_features": sum(1 for item in payload["features"] if item["properties"]["target"] == "WL"),
        "area_features": sum(1 for item in payload["features"] if item["properties"]["target"] == "WP"),
        "text_features": sum(1 for item in payload["features"] if item["properties"]["target"] == "WT"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--min-line-length", type=int, default=80)
    parser.add_argument("--min-area", type=int, default=250)
    parser.add_argument("--text-crop-dir", type=Path, default=None)
    args = parser.parse_args()
    report = run(
        args.input,
        args.output,
        min_line_length=args.min_line_length,
        min_area=args.min_area,
        text_crop_dir=args.text_crop_dir,
    )
    for key, value in report.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
