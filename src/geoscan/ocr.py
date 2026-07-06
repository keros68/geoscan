from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .candidates import feature, feature_collection
from .raster import image_point_to_map_point, load_rgb, rgb_to_bgr


def merge_ocr_results(payload: dict[str, Any], results: dict[str, dict[str, Any]], *, engine_name: str) -> dict[str, Any]:
    updated_features = []
    for item in payload.get("features", []):
        updated_item = dict(item)
        properties = dict(item.get("properties", {}))
        crop_path = properties.get("crop_path")
        if properties.get("target") == "WT" and crop_path in results:
            result = results[str(crop_path)]
            properties["ocr_text"] = str(result.get("text", ""))
            properties["ocr_confidence"] = round(float(result.get("confidence", 0.0)), 4)
            properties["ocr_engine"] = str(result.get("engine", engine_name))
            properties["ocr_status"] = "recognized" if properties["ocr_text"] else "empty"
            if "error" in result:
                properties["ocr_error"] = str(result["error"])
        updated_item["properties"] = properties
        updated_features.append(updated_item)
    return feature_collection(updated_features)


def load_manual_corrections(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        rows = csv.DictReader(handle)
        return {
            str(row.get("crop_path", "")).strip(): {
                "review_text": str(row.get("review_text", "")).strip(),
                "review_note": str(row.get("review_note", "")).strip(),
            }
            for row in rows
            if str(row.get("crop_path", "")).strip()
        }


def apply_manual_ocr_corrections(payload: dict[str, Any], corrections: dict[str, dict[str, str]]) -> dict[str, Any]:
    updated_features = []
    for item in payload.get("features", []):
        updated_item = dict(item)
        properties = dict(item.get("properties", {}))
        crop_path = str(properties.get("crop_path", ""))
        correction = corrections.get(crop_path)
        if properties.get("target") == "WT" and correction:
            properties["ocr_raw_text"] = properties.get("ocr_raw_text", properties.get("ocr_text", ""))
            properties["review_text"] = str(correction.get("review_text", ""))
            properties["review_status"] = "manual_corrected" if properties["review_text"] else "manual_rejected"
            properties["review_note"] = str(correction.get("review_note", ""))
        updated_item["properties"] = properties
        updated_features.append(updated_item)
    return feature_collection(updated_features)


def _box_bounds(box: Any, *, image_width: int, image_height: int, padding: int) -> tuple[int, int, int, int]:
    points = np.asarray(box, dtype=float)
    left = max(0, int(np.floor(points[:, 0].min())) - padding)
    top = max(0, int(np.floor(points[:, 1].min())) - padding)
    right = min(image_width, int(np.ceil(points[:, 0].max())) + padding)
    bottom = min(image_height, int(np.ceil(points[:, 1].max())) + padding)
    return left, top, right, bottom


def direct_ocr_features_from_output(
    raw: Any,
    *,
    rgb: np.ndarray,
    crop_dir: Path,
    min_confidence: float = 0.5,
    padding: int = 3,
    max_candidates: int = 500,
) -> list[dict[str, Any]]:
    image_height, image_width = rgb.shape[:2]
    raw_boxes = getattr(raw, "boxes", None)
    boxes = list(raw_boxes) if raw_boxes is not None else []
    raw_texts = getattr(raw, "txts", None)
    texts = list(raw_texts) if raw_texts is not None else []
    raw_scores = getattr(raw, "scores", None)
    scores = list(raw_scores) if raw_scores is not None else []

    crop_dir.mkdir(parents=True, exist_ok=True)
    for old_crop in crop_dir.glob("direct_ocr_*.png"):
        old_crop.unlink()

    candidates = []
    for source_index, box in enumerate(boxes):
        if source_index >= len(texts) or source_index >= len(scores):
            continue
        text = str(texts[source_index]).strip()
        score = float(scores[source_index])
        if not text or score < min_confidence:
            continue

        left, top, right, bottom = _box_bounds(box, image_width=image_width, image_height=image_height, padding=padding)
        if right <= left or bottom <= top:
            continue

        crop_name = f"direct_ocr_{len(candidates) + 1:04d}.png"
        Image.fromarray(rgb[top:bottom, left:right]).save(crop_dir / crop_name)
        center_x = left + (right - left) / 2
        center_y = top + (bottom - top) / 2
        candidates.append(
            feature(
                geometry={
                    "type": "Point",
                    "coordinates": image_point_to_map_point(center_x, center_y, height=image_height),
                },
                target="WT",
                cad_layer="T04_AUTO_OCR_TEXT_DIRECT",
                feature_name="auto_ocr_text_direct",
                source="rapidocr_full_image",
                confidence=score,
                note="RapidOCR整图直接文字候选；需人工对照裁图确认。",
                mapgis_no=300,
                extra={
                    "crop_path": crop_name,
                    "ocr_text": text,
                    "ocr_confidence": round(score, 4),
                    "ocr_engine": "rapidocr",
                    "ocr_status": "recognized",
                    "bbox_left_px": left,
                    "bbox_top_px": top,
                    "bbox_right_px": right,
                    "bbox_bottom_px": bottom,
                    "crop_width_px": right - left,
                    "crop_height_px": bottom - top,
                },
            )
        )
        if len(candidates) >= max_candidates:
            break
    return candidates


def write_ocr_review_csv(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "crop_path",
                "ocr_text",
                "review_text",
                "ocr_confidence",
                "ocr_engine",
                "checked",
                "review_status",
                "review_note",
                "note",
            ],
        )
        writer.writeheader()
        for item in payload.get("features", []):
            properties = item.get("properties", {})
            if properties.get("target") == "WT" and properties.get("crop_path"):
                writer.writerow(
                    {
                        "crop_path": properties.get("crop_path", ""),
                        "ocr_text": properties.get("ocr_text", ""),
                        "review_text": properties.get("review_text", ""),
                        "ocr_confidence": properties.get("ocr_confidence", ""),
                        "ocr_engine": properties.get("ocr_engine", ""),
                        "checked": properties.get("checked", ""),
                        "review_status": properties.get("review_status", ""),
                        "review_note": properties.get("review_note", ""),
                        "note": properties.get("note", ""),
                    }
                )


def _load_rapidocr() -> tuple[Any, str]:
    try:
        from rapidocr import RapidOCR

        return RapidOCR(), "rapidocr"
    except ImportError:
        try:
            from rapidocr_onnxruntime import RapidOCR

            return RapidOCR(), "rapidocr_onnxruntime"
        except ImportError as exc:
            raise RuntimeError(
                "未找到 OCR 引擎。请在 OCR 环境中安装 rapidocr 和 onnxruntime。"
            ) from exc


def _parse_ocr_lines(raw: Any) -> list[tuple[str, float]]:
    if raw is None:
        return []

    if hasattr(raw, "txts"):
        texts = list(getattr(raw, "txts") or [])
        scores = list(getattr(raw, "scores", []) or [])
        return [(str(text), float(scores[index]) if index < len(scores) else 0.0) for index, text in enumerate(texts)]

    if hasattr(raw, "to_json"):
        data = raw.to_json()
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return []
        return _parse_ocr_lines(data)

    if isinstance(raw, dict):
        texts = raw.get("txts") or raw.get("texts") or raw.get("rec_texts")
        scores = raw.get("scores") or raw.get("rec_scores") or []
        if texts:
            return [
                (str(text), float(scores[index]) if index < len(scores) else 0.0)
                for index, text in enumerate(texts)
            ]
        if "text" in raw:
            return [(str(raw.get("text", "")), float(raw.get("confidence", raw.get("score", 0.0)) or 0.0))]
        return []

    if isinstance(raw, tuple):
        return _parse_ocr_lines(raw[0] if raw else None)

    if isinstance(raw, list):
        lines: list[tuple[str, float]] = []
        for entry in raw:
            if isinstance(entry, str):
                lines.append((entry, 0.0))
            elif isinstance(entry, dict):
                lines.extend(_parse_ocr_lines(entry))
            elif isinstance(entry, (list, tuple)):
                if len(entry) >= 3 and isinstance(entry[1], str):
                    lines.append((entry[1], float(entry[2] or 0.0)))
                elif len(entry) >= 2 and isinstance(entry[1], (list, tuple)) and len(entry[1]) >= 2:
                    lines.append((str(entry[1][0]), float(entry[1][1] or 0.0)))
                else:
                    lines.extend(_parse_ocr_lines(entry))
        return lines

    return []


def _recognize_crop(engine: Any, crop_path: Path, *, engine_name: str) -> dict[str, Any]:
    raw = engine(str(crop_path))
    lines = [(text.strip(), score) for text, score in _parse_ocr_lines(raw) if text.strip()]
    if not lines:
        return {"text": "", "confidence": 0.0, "engine": engine_name}
    text = " ".join(text for text, _score in lines)
    confidence = sum(score for _text, score in lines) / len(lines)
    return {"text": text, "confidence": confidence, "engine": engine_name}


def run_ocr_on_candidates(
    input_geojson: Path,
    crop_dir: Path,
    output_geojson: Path,
    *,
    review_csv: Path | None = None,
    corrections_csv: Path | None = None,
    engine: str = "rapidocr",
) -> dict[str, Any]:
    if engine != "rapidocr":
        raise ValueError("当前只接入 rapidocr；后续可在同一接口下增加 PaddleOCR。")

    payload = json.loads(input_geojson.read_text(encoding="utf-8"))
    ocr_engine, engine_name = _load_rapidocr()
    crop_names = sorted(
        {
            str(item.get("properties", {}).get("crop_path"))
            for item in payload.get("features", [])
            if item.get("properties", {}).get("target") == "WT" and item.get("properties", {}).get("crop_path")
        }
    )

    results: dict[str, dict[str, Any]] = {}
    for crop_name in crop_names:
        crop_path = crop_dir / crop_name
        if not crop_path.exists():
            results[crop_name] = {
                "text": "",
                "confidence": 0.0,
                "engine": engine_name,
                "error": f"crop not found: {crop_path}",
            }
            continue
        try:
            results[crop_name] = _recognize_crop(ocr_engine, crop_path, engine_name=engine_name)
        except Exception as exc:  # OCR engines can fail on individual damaged crops.
            results[crop_name] = {"text": "", "confidence": 0.0, "engine": engine_name, "error": str(exc)}

    updated = merge_ocr_results(payload, results, engine_name=engine_name)
    corrections = load_manual_corrections(corrections_csv) if corrections_csv is not None else {}
    if corrections:
        updated = apply_manual_ocr_corrections(updated, corrections)
    output_geojson.parent.mkdir(parents=True, exist_ok=True)
    output_geojson.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
    if review_csv is not None:
        write_ocr_review_csv(updated, review_csv)

    recognized = sum(1 for result in results.values() if result.get("text"))
    return {
        "input": str(input_geojson),
        "output": str(output_geojson),
        "review_csv": str(review_csv) if review_csv else "",
        "engine": engine_name,
        "text_crops": len(crop_names),
        "recognized_text_crops": recognized,
        "manual_corrections": len(corrections),
    }


def run_direct_ocr_on_raster(
    input_path: Path,
    output_geojson: Path,
    *,
    crop_dir: Path,
    review_csv: Path | None = None,
    min_confidence: float = 0.5,
    max_candidates: int = 500,
) -> dict[str, Any]:
    rgb = load_rgb(input_path)
    ocr_engine, engine_name = _load_rapidocr()
    # Reuse the already-decoded raster instead of letting the OCR engine re-read
    # input_path from disk. RapidOCR's LoadImage passes ndarray input through
    # unchanged (only str/Path/bytes/PIL.Image get RGB->BGR conversion), so it
    # must be handed BGR here to match what it would have decoded from the file.
    raw = ocr_engine(rgb_to_bgr(rgb))
    features = direct_ocr_features_from_output(
        raw,
        rgb=rgb,
        crop_dir=crop_dir,
        min_confidence=min_confidence,
        max_candidates=max_candidates,
    )
    payload = feature_collection(features)
    output_geojson.parent.mkdir(parents=True, exist_ok=True)
    output_geojson.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if review_csv is not None:
        write_ocr_review_csv(payload, review_csv)
    return {
        "input": str(input_path),
        "output": str(output_geojson),
        "review_csv": str(review_csv) if review_csv else "",
        "engine": engine_name,
        "text_features": len(features),
        "min_confidence": min_confidence,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_geojson", type=Path)
    parser.add_argument("crop_dir", type=Path)
    parser.add_argument("output_geojson", type=Path)
    parser.add_argument("--review-csv", type=Path, default=None)
    parser.add_argument("--corrections-csv", type=Path, default=None)
    parser.add_argument("--engine", default="rapidocr")
    args = parser.parse_args()

    report = run_ocr_on_candidates(
        args.input_geojson,
        args.crop_dir,
        args.output_geojson,
        review_csv=args.review_csv,
        corrections_csv=args.corrections_csv,
        engine=args.engine,
    )
    for key, value in report.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
