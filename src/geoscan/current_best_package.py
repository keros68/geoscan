from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, Iterable


MPJ_REFERENCE_PATTERN = re.compile(
    r"[A-Za-z0-9_]+\.(?:WL|WT|WP|MPJ|JPG|JPEG|TIF|TIFF)",
    flags=re.IGNORECASE,
)


def extract_mpj_references(data: bytes) -> list[str]:
    text = "".join(chr(item) if 32 <= item <= 126 else " " for item in data)
    references: list[str] = []
    seen: set[str] = set()
    for match in MPJ_REFERENCE_PATTERN.finditer(text):
        reference = match.group(0)
        key = reference.lower()
        if key in seen:
            continue
        seen.add(key)
        references.append(reference)
    return references


def _safe_relative_path(value: str) -> Path:
    path = Path(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Only relative package paths are allowed: {value}")
    return path


def _assert_safe_output(source_root: Path, output_root: Path) -> None:
    source = source_root.resolve()
    output = output_root.resolve()
    if output == source:
        raise ValueError("Output root must not be the source package")
    if source in output.parents:
        raise ValueError("Output root must not be inside the source package")


def _copy_file(source_root: Path, output_root: Path, relative_path: Path) -> str:
    source = source_root / relative_path
    target = output_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return str(relative_path).replace("/", "\\")


def _copy_extra(source_root: Path, output_root: Path, relative_path: Path) -> list[str]:
    source = source_root / relative_path
    target = output_root / relative_path
    if source.is_dir():
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        return [
            str(item.relative_to(output_root)).replace("/", "\\")
            for item in sorted(target.rglob("*"))
            if item.is_file()
        ]
    return [_copy_file(source_root, output_root, relative_path)]


def _write_readme(path: Path, manifest: dict[str, Any]) -> None:
    lines = [
        f"# {manifest['package_label']}",
        "",
        "Open this MapGIS project for the current review pass:",
        "",
        f"- {manifest['mpj']}",
        "",
        "This folder is intentionally minimal. It contains only the project file,",
        "the files referenced by that project, and selected QA evidence files.",
        "",
        "Boundary:",
        "",
        "- No geological interpretation was added by this packaging step.",
        "- Source scripts and full experiment history remain under mapgis_work.",
        "- OCR/text candidates remain review content unless separately confirmed.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def build_current_best_package(
    *,
    source_root: Path,
    output_root: Path,
    mpj_name: str,
    package_label: str,
    extra_relative_paths: Iterable[str] = (),
) -> dict[str, Any]:
    source_root = Path(source_root)
    output_root = Path(output_root)
    _assert_safe_output(source_root, output_root)

    mpj_relative = _safe_relative_path(mpj_name)
    mpj_path = source_root / mpj_relative
    if not mpj_path.exists():
        raise FileNotFoundError(mpj_path)

    references = extract_mpj_references(mpj_path.read_bytes())
    missing_references = [item for item in references if not (source_root / _safe_relative_path(item)).exists()]
    if missing_references:
        raise FileNotFoundError(f"missing MPJ references: {missing_references}")

    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)

    copied_files: list[str] = []
    copied_files.append(_copy_file(source_root, output_root, mpj_relative))
    for reference in references:
        copied_files.append(_copy_file(source_root, output_root, _safe_relative_path(reference)))

    copied_extra_files: list[str] = []
    missing_extra_files: list[str] = []
    for extra in extra_relative_paths:
        relative_extra = _safe_relative_path(extra)
        if not (source_root / relative_extra).exists():
            missing_extra_files.append(str(relative_extra).replace("/", "\\"))
            continue
        copied_extra_files.extend(_copy_extra(source_root, output_root, relative_extra))

    manifest: dict[str, Any] = {
        "package_label": package_label,
        "source_root": str(source_root),
        "output_root": str(output_root),
        "mpj": str(output_root / mpj_relative),
        "mpj_name": str(mpj_relative).replace("/", "\\"),
        "required_references": references,
        "required_reference_count": len(references),
        "missing_references": missing_references,
        "copied_files": sorted(set(copied_files)),
        "copied_extra_files": sorted(set(copied_extra_files)),
        "missing_extra_files": missing_extra_files,
        "boundary": {
            "packaging_only": True,
            "geological_content_modified": False,
            "source_experiments_copied": False,
        },
    }
    _write_readme(output_root / "README_CURRENT_BEST.txt", manifest)
    (output_root / "CURRENT_BEST_MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest
