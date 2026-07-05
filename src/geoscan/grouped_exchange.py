from __future__ import annotations

from pathlib import Path


def clean_grouped_source_dir(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    removed: list[Path] = []
    patterns = ["*.geojson", "manifest.json", "classification_summary.md"]
    for pattern in patterns:
        for path in output_dir.glob(pattern):
            if path.is_file():
                path.unlink()
                removed.append(path)
    return removed


def safe_target_stem(target_file: str) -> str:
    return str(target_file).replace(".", "_")


def target_exchange_kind(target_file: str) -> str:
    suffix = Path(str(target_file)).suffix.upper()
    if suffix in {".WT", ".WL"}:
        return "dxf"
    if suffix == ".WP":
        return "shp"
    raise ValueError(f"Unsupported target file type: {target_file}")


def grouped_exchange_path(output_dir: Path, target_file: str) -> Path:
    stem = safe_target_stem(target_file)
    kind = target_exchange_kind(target_file)
    if kind == "dxf":
        return output_dir / f"{stem}.dxf"
    return output_dir / stem / f"{stem}.shp"
