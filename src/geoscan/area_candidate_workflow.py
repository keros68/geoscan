from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from geoscan.candidates import write_geojson
from geoscan.extract_areas import extract_color_area_candidates
from geoscan.raster import load_rgb


def generate_review_area_candidates(
    *,
    source_raster: Path,
    output_root: Path,
    map_id: str,
    min_area: int = 250,
) -> dict[str, Any]:
    """Generate optional review-only WP area candidates from colored raster fills."""

    area_dir = Path(output_root) / "05_AREA_WORKFLOW"
    output_geojson = area_dir / f"{map_id}_review_area_candidates.geojson"
    report_path = area_dir / "AREA_CANDIDATE_GENERATION_REPORT.json"

    rgb = load_rgb(Path(source_raster))
    features = extract_color_area_candidates(rgb, min_area=min_area)
    write_geojson(output_geojson, features)

    report = {
        "mode": "auto",
        "ok": True,
        "source_raster": str(source_raster),
        "output_geojson": str(output_geojson),
        "feature_count": len(features),
        "min_area": int(min_area),
        "writes_checked_yes": False,
        "optional": True,
        "note": (
            "Optional review-only WP area candidates from deterministic color-fill extraction; "
            "boundaries and geological meaning require manual MapGIS review."
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
