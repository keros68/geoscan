from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from geoscan.candidates import feature_collection
from geoscan.extract_text import extract_text_region_candidates
from geoscan.ocr import run_direct_ocr_on_raster, write_ocr_review_csv
from geoscan.raster import load_rgb

PROJECT_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
OCR_PYTHON_ENV_VAR = "MAPGIS_OCR_PYTHON"


def _package_import_root() -> Path:
    """Folder the external OCR interpreter should import ``geoscan`` from.

    In the PyInstaller build the package sources are bundled under
    ``_internal/py_src/`` — a dedicated subfolder, NOT ``_internal`` itself,
    because ``_internal`` also holds compiled numpy/cv2 for the frozen Python
    version and would poison the external interpreter's imports.
    """
    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "py_src"
        if (bundled / "geoscan").is_dir():
            return bundled
    return PROJECT_PACKAGE_ROOT
DEFAULT_OCR_PYTHON_CANDIDATES = (
    Path(r"D:\miniconda\envs\mapgis-ocr\python.exe"),
    Path(r"C:\miniconda\envs\mapgis-ocr\python.exe"),
)
OCR_SUBPROCESS_TIMEOUT_SECONDS = 600


def resolve_ocr_python(ocr_python: Path | None = None) -> Path | None:
    """Resolve an external interpreter that has rapidocr installed.

    Priority: explicit argument -> MAPGIS_OCR_PYTHON env var -> known conda paths.
    Returns None when nothing usable is found (caller falls back).
    """
    candidates: list[Path] = []
    if ocr_python is not None:
        candidates.append(Path(ocr_python))
    env_value = os.environ.get(OCR_PYTHON_ENV_VAR, "").strip()
    if env_value:
        candidates.append(Path(env_value))
    candidates.extend(DEFAULT_OCR_PYTHON_CANDIDATES)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _run_direct_ocr_via_subprocess(
    interpreter: Path,
    *,
    source_raster: Path,
    output_geojson: Path,
    crop_dir: Path,
    review_csv: Path,
    min_confidence: float,
    max_candidates: int,
    text_dir: Path,
) -> dict[str, Any]:
    args_path = text_dir / "_ocr_subprocess_args.json"
    report_path = text_dir / "_ocr_subprocess_report.json"
    args_path.write_text(
        json.dumps(
            {
                "source_raster": str(source_raster),
                "output_geojson": str(output_geojson),
                "crop_dir": str(crop_dir),
                "review_csv": str(review_csv),
                "min_confidence": min_confidence,
                "max_candidates": max_candidates,
                "report_path": str(report_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    import_root = _package_import_root()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(import_root) + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [str(interpreter), "-m", "geoscan.ocr_subprocess", str(args_path)],
        cwd=str(import_root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=OCR_SUBPROCESS_TIMEOUT_SECONDS,
    )
    result: dict[str, Any] = {
        "interpreter": str(interpreter),
        "returncode": completed.returncode,
        "stderr_tail": (completed.stderr or "")[-500:],
    }
    if completed.returncode == 0 and report_path.is_file():
        result["ocr_report"] = json.loads(report_path.read_text(encoding="utf-8"))
    return result


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _feature_count(path: Path) -> int:
    if not path.is_file():
        return 0
    payload = json.loads(path.read_text(encoding="utf-8"))
    return len(payload.get("features", []))


def _fallback_text_region_candidates(
    *,
    source_raster: Path,
    output_geojson: Path,
    crop_dir: Path,
    review_csv: Path,
) -> dict[str, Any]:
    rgb = load_rgb(source_raster)
    features = extract_text_region_candidates(
        rgb,
        crop_dir=crop_dir,
        max_candidates=800,
    )
    payload = feature_collection(features)
    _write_json(output_geojson, payload)
    write_ocr_review_csv(payload, review_csv)
    return {
        "engine": "local_text_region_detector",
        "feature_count": len(features),
        "crop_dir": str(crop_dir),
    }


def generate_review_text_candidates(
    *,
    source_raster: Path,
    output_root: Path,
    map_id: str,
    min_confidence: float = 0.45,
    max_candidates: int = 800,
    ocr_python: Path | None = None,
) -> dict[str, Any]:
    """Generate review-only text candidate GeoJSON for the production run.

    RapidOCR is preferred when available because it gives real text guesses.
    If the OCR engine is missing, fails, or returns no candidates, the workflow
    falls back to local text-region detection so later WT placeholders still
    preserve likely text positions.
    """

    text_dir = Path(output_root) / "05_TEXT_WORKFLOW"
    output_geojson = text_dir / f"{map_id}_review_text_candidates.geojson"
    review_csv = text_dir / f"{map_id}_review_text_candidates.csv"
    direct_crop_dir = text_dir / "direct_ocr_crops"
    fallback_crop_dir = text_dir / "region_crops"
    text_dir.mkdir(parents=True, exist_ok=True)

    direct_report: dict[str, Any] | None = None
    direct_error = ""
    try:
        direct_report = run_direct_ocr_on_raster(
            Path(source_raster),
            output_geojson,
            crop_dir=direct_crop_dir,
            review_csv=review_csv,
            min_confidence=min_confidence,
            max_candidates=max_candidates,
        )
    except Exception as exc:
        direct_error = f"{type(exc).__name__}: {exc}"

    direct_count = _feature_count(output_geojson)
    ocr_route = "in_process"
    subprocess_report: dict[str, Any] | None = None
    if direct_count == 0:
        interpreter = resolve_ocr_python(ocr_python)
        if interpreter is not None:
            try:
                subprocess_report = _run_direct_ocr_via_subprocess(
                    interpreter,
                    source_raster=Path(source_raster),
                    output_geojson=output_geojson,
                    crop_dir=direct_crop_dir,
                    review_csv=review_csv,
                    min_confidence=min_confidence,
                    max_candidates=max_candidates,
                    text_dir=text_dir,
                )
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                subprocess_report = {
                    "interpreter": str(interpreter),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            direct_count = _feature_count(output_geojson)
            if direct_count > 0:
                ocr_route = "external_interpreter_subprocess"

    if direct_count > 0:
        report = {
            "mode": "auto",
            "ok": True,
            "source_raster": str(source_raster),
            "output_geojson": str(output_geojson),
            "review_csv": str(review_csv),
            "feature_count": direct_count,
            "fallback_used": False,
            "ocr_route": ocr_route,
            "direct_ocr": direct_report,
            "direct_ocr_error": direct_error,
            "ocr_subprocess": subprocess_report,
            "writes_checked_yes": False,
            "note": "Review-only text candidates generated from full-image OCR.",
        }
        _write_json(text_dir / "TEXT_CANDIDATE_GENERATION_REPORT.json", report)
        return report

    fallback_report = _fallback_text_region_candidates(
        source_raster=Path(source_raster),
        output_geojson=output_geojson,
        crop_dir=fallback_crop_dir,
        review_csv=review_csv,
    )
    feature_count = _feature_count(output_geojson)
    report = {
        "mode": "auto",
        "ok": True,
        "source_raster": str(source_raster),
        "output_geojson": str(output_geojson),
        "review_csv": str(review_csv),
        "feature_count": feature_count,
        "fallback_used": True,
        "ocr_route": "fallback_local_detector",
        "direct_ocr": direct_report,
        "direct_ocr_error": direct_error,
        "ocr_subprocess": subprocess_report,
        "fallback": fallback_report,
        "writes_checked_yes": False,
        "note": "Review-only text candidates generated by local fallback detector after OCR was unavailable, failed, or empty.",
    }
    _write_json(text_dir / "TEXT_CANDIDATE_GENERATION_REPORT.json", report)
    return report
