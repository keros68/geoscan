from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from geoscan.candidates import write_geojson
from geoscan.extract_lines import extract_line_candidates
from geoscan.raster import load_rgb

VALID_LINE_ENGINES = {"hough", "trace"}


def generate_review_line_candidates(
    *,
    source_raster: Path,
    output_root: Path,
    map_id: str,
    min_line_length: int = 130,
    engine: str = "hough",
) -> dict[str, Any]:
    """Generate review-only WL line candidates from the frozen input image.

    ``engine="hough"`` (production default) keeps the deterministic straight-line
    HoughLinesP extraction byte-identical to previous runs. ``engine="trace"``
    uses the centerline-tracing vectorizer (straight + curve + loop candidates);
    it stays opt-in until side-by-side overlay evidence justifies switching.
    """
    if engine not in VALID_LINE_ENGINES:
        raise ValueError(f"engine must be one of {sorted(VALID_LINE_ENGINES)}")
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

        trace_config = TraceConfig()
        features = extract_traced_line_candidates(rgb, config=trace_config)
        engine_parameters = asdict(trace_config)
        note = (
            "Review-only WL line candidates generated from deterministic "
            "centerline tracing (straight + curve + loop)."
        )
    else:
        features = extract_line_candidates(rgb, min_length=min_line_length)
        engine_parameters = {"min_line_length": int(min_line_length)}
        note = "Review-only WL line candidates generated from deterministic Hough line extraction."
    write_geojson(output_geojson, features)

    report = {
        "mode": "auto",
        "ok": True,
        "engine": engine,
        "engine_parameters": engine_parameters,
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
