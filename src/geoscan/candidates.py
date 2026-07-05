from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .classification import classify_object


def feature(
    *,
    geometry: dict[str, Any],
    target: str,
    cad_layer: str,
    feature_name: str,
    source: str,
    confidence: float,
    note: str,
    mapgis_no: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    properties = {
        "target": target,
        "cad_layer": cad_layer,
        "feature": feature_name,
        "source": source,
        "confidence": round(float(confidence), 4),
        "checked": "no",
        "note": note,
        "mapgis_no": int(mapgis_no),
    }
    if extra:
        properties.update(extra)
    properties.update(
        {
            key: properties.get(key, value)
            for key, value in classify_object(
                target=target,
                cad_layer=cad_layer,
                feature_name=feature_name,
                text_value=str(properties.get("label_text") or properties.get("ocr_text") or ""),
                symbol_name=str(properties.get("symbol_name") or ""),
                note=note,
            ).items()
        }
    )
    return {"type": "Feature", "properties": properties, "geometry": geometry}


def feature_collection(features: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": list(features)}


def write_geojson(path: Path, features: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(feature_collection(features), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
