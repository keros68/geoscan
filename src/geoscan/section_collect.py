from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ConversionEntry:
    target_file: str
    kind: str
    relative_path: str
    features: int


def parse_conversion_list(path: Path) -> list[ConversionEntry]:
    entries: list[ConversionEntry] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("T01_") or line.startswith("Raster:"):
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        target_file, kind, relative_path, features = parts
        entries.append(ConversionEntry(target_file, kind, relative_path, int(features)))
    return entries


def _source_stem(entry: ConversionEntry) -> str:
    source = Path(entry.relative_path.replace("\\", "/"))
    if source.suffix.lower() == ".shp":
        return source.parent.name
    return source.stem


def _candidate_roots(import_dir: Path, entry: ConversionEntry, output_dir: Path) -> list[Path]:
    roots = [import_dir / "grouped_exchange" / _source_stem(entry), import_dir]
    result: list[Path] = []
    for root in roots:
        if root.exists() and output_dir not in [root, *root.parents]:
            result.append(root)
    return result


def _candidate_files(import_dir: Path, entry: ConversionEntry, output_dir: Path) -> list[Path]:
    suffix = Path(entry.target_file).suffix.upper()
    candidates: list[Path] = []
    for root in _candidate_roots(import_dir, entry, output_dir):
        direct = root / entry.target_file
        if direct.exists():
            candidates.append(direct)
        for path in root.rglob(f"*{suffix}"):
            if output_dir in [path, *path.parents]:
                continue
            if path not in candidates:
                candidates.append(path)
    return sorted(candidates, key=lambda item: (len(item.parts), str(item)))


def _find_converted_file(
    import_dir: Path,
    entry: ConversionEntry,
    output_dir: Path,
    layer_output_to_target: dict[str, str],
) -> Path | None:
    for candidate in _candidate_files(import_dir, entry, output_dir):
        if candidate.name.upper() == entry.target_file.upper():
            return candidate
    for candidate in _candidate_files(import_dir, entry, output_dir):
        if layer_output_to_target.get(candidate.name) == entry.target_file:
            return candidate
    return None


def collect_converted_mapgis_files(
    *,
    import_dir: Path,
    conversion_list: Path,
    output_dir: Path,
    layer_output_to_target: dict[str, str],
) -> dict[str, Any]:
    entries = parse_conversion_list(conversion_list)
    output_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for entry in entries:
        source = _find_converted_file(import_dir, entry, output_dir, layer_output_to_target)
        if source is None:
            missing.append(entry.target_file)
            records.append(
                {
                    "target_file": entry.target_file,
                    "status": "missing",
                    "expected_source": entry.relative_path,
                    "features": entry.features,
                }
            )
            continue
        destination = output_dir / entry.target_file
        shutil.copy2(source, destination)
        records.append(
            {
                "target_file": entry.target_file,
                "status": "copied",
                "source": str(source),
                "destination": str(destination),
                "bytes": destination.stat().st_size,
                "features": entry.features,
            }
        )

    report = {
        "import_dir": str(import_dir),
        "conversion_list": str(conversion_list),
        "output_dir": str(output_dir),
        "copied": sum(1 for record in records if record["status"] == "copied"),
        "missing": missing,
        "records": records,
    }
    (output_dir / "COLLECT_REPORT.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "MISSING.txt").write_text("\n".join(missing) + ("\n" if missing else ""), encoding="utf-8")
    return report


def _find_section_batch_file(
    *,
    section_output_dir: Path,
    section_input: Path | None = None,
    target_file: str,
    layer_output_to_target: dict[str, str],
) -> Path | None:
    suffix = Path(target_file).suffix.upper()
    candidates = sorted(section_output_dir.rglob(f"*{suffix}")) if section_output_dir.is_dir() else []
    if section_input is not None:
        root_output = section_input.with_suffix(suffix)
        if root_output.is_file() and root_output not in candidates:
            candidates.append(root_output)
    for candidate in candidates:
        if candidate.name.upper() == target_file.upper():
            return candidate
    for candidate in candidates:
        if layer_output_to_target.get(candidate.name.upper()) == target_file:
            return candidate
    for candidate in candidates:
        if layer_output_to_target.get(candidate.name) == target_file:
            return candidate
    if len(candidates) == 1:
        return candidates[0]
    return None


def collect_section_batch_mapgis_files(
    *,
    conversion_list: Path,
    section_batch_manifest: Path,
    output_dir: Path,
    layer_output_to_target: dict[str, str],
) -> dict[str, Any]:
    entries = parse_conversion_list(conversion_list)
    records_by_target = {
        str(record["target_name"]): record
        for record in json.loads(section_batch_manifest.read_text(encoding="utf-8"))
    }
    output_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    missing: list[str] = []
    for entry in entries:
        if entry.kind != "dxf":
            missing.append(entry.target_file)
            records.append(
                {
                    "target_file": entry.target_file,
                    "status": "skipped",
                    "reason": "Only SECTION DXF outputs are collected by this pass.",
                    "features": entry.features,
                }
            )
            continue

        batch_record = records_by_target.get(entry.target_file)
        source = None
        candidate_batch_records = [batch_record] if batch_record is not None else list(records_by_target.values())
        for candidate_batch_record in candidate_batch_records:
            source = _find_section_batch_file(
                section_output_dir=Path(candidate_batch_record["section_output_dir"]),
                section_input=(
                    Path(candidate_batch_record["section_input"])
                    if "section_input" in candidate_batch_record
                    else None
                ),
                target_file=entry.target_file,
                layer_output_to_target=layer_output_to_target,
            )
            if source is not None:
                batch_record = candidate_batch_record
                break

        if source is None:
            missing.append(entry.target_file)
            records.append(
                {
                    "target_file": entry.target_file,
                    "status": "missing",
                    "features": entry.features,
                    "section_output_dir": None if batch_record is None else batch_record["section_output_dir"],
                }
            )
            continue

        destination = output_dir / entry.target_file
        shutil.copy2(source, destination)
        records.append(
            {
                "target_file": entry.target_file,
                "status": "copied",
                "source": str(source),
                "destination": str(destination),
                "bytes": destination.stat().st_size,
                "features": entry.features,
            }
        )

    report = {
        "conversion_list": str(conversion_list),
        "section_batch_manifest": str(section_batch_manifest),
        "output_dir": str(output_dir),
        "copied": sum(1 for record in records if record["status"] == "copied"),
        "missing": missing,
        "records": records,
    }
    (output_dir / "COLLECT_REPORT.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "MISSING.txt").write_text("\n".join(missing) + ("\n" if missing else ""), encoding="utf-8")
    return report
