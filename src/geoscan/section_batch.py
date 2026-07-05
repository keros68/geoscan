from __future__ import annotations

import json
import shutil
from pathlib import Path


def short_section_stem(name: str, used: set[str]) -> str:
    base = "".join(char for char in Path(name).stem.upper() if char.isalnum())
    if not base:
        base = "DXF"
    candidate = base[:8]
    index = 1
    while candidate in used:
        suffix = str(index)
        candidate = f"{base[: 8 - len(suffix)]}{suffix}"
        index += 1
    used.add(candidate)
    return candidate


def load_dxf_entries(source_dir: Path) -> list[tuple[str, Path]]:
    manifest_path = source_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = []
        for target_name, record in manifest.items():
            if record.get("kind") != "dxf":
                continue
            path = Path(record["path"])
            if not path.exists():
                raise FileNotFoundError(path)
            entries.append((target_name, path))
        return sorted(entries)
    return [(path.stem, path) for path in sorted(source_dir.glob("*.dxf"))]


def prepare_section_batch_input(source_dir: Path, output_dir: Path) -> list[dict[str, str]]:
    entries = load_dxf_entries(source_dir)
    if not entries:
        raise RuntimeError(f"No DXF inputs found in {source_dir}")
    output_dir.mkdir(parents=True, exist_ok=False)

    used: set[str] = set()
    records = []
    for target_name, source_path in entries:
        alias = short_section_stem(source_path.name, used)
        destination = output_dir / f"{alias}.DXF"
        shutil.copy2(source_path, destination)
        records.append(
            {
                "target_name": target_name,
                "source": str(source_path),
                "section_input": str(destination),
                "section_output_dir": str(destination.with_suffix("")),
            }
        )

    (output_dir / "section_batch_manifest.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return records
