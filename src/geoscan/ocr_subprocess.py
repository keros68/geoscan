"""Entry point executed inside the external OCR interpreter (e.g. mapgis-ocr env).

Usage: python -m geoscan.ocr_subprocess <args-json-path>

Reads the task description from the JSON file, runs full-image OCR, writes the
candidate GeoJSON/CSV to the requested paths, and stores the OCR report JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from geoscan.ocr import run_direct_ocr_on_raster


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: ocr_subprocess <args-json-path>", file=sys.stderr)
        return 2
    args = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    report = run_direct_ocr_on_raster(
        Path(args["source_raster"]),
        Path(args["output_geojson"]),
        crop_dir=Path(args["crop_dir"]),
        review_csv=Path(args["review_csv"]),
        min_confidence=float(args["min_confidence"]),
        max_candidates=int(args["max_candidates"]),
    )
    Path(args["report_path"]).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
