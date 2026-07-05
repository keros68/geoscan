#!/usr/bin/env python
"""Level scanned rasters and save RGB uncompressed TIFF files.

This module is intentionally standalone. It can be called from a GUI process
or used as a command line tool.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
from PIL import Image, ImageFile


Image.MAX_IMAGE_PIXELS = None

DEFAULT_EXTENSIONS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp")
ENHANCED_PREVIEW_DIRNAME = "ENHANCED_PREVIEW"


def _load_enhance_module():
    """Optional visual-enhance module from the main repo (mapgis_work package).

    The leveled main output is always faithful; enhancement only writes an
    extra viewing copy under ENHANCED_PREVIEW/. When this tool is copied out
    of the repo the module is unavailable and --enhance fails with a clear
    message instead of degrading silently.
    """
    try:
        from geoscan import raster_enhance

        return raster_enhance
    except ImportError:
        repo_root = Path(__file__).resolve().parents[1]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        try:
            from geoscan import raster_enhance

            return raster_enhance
        except ImportError:
            return None


@dataclass(frozen=True)
class LevelOptions:
    target_dpi: tuple[int, int] = (300, 300)
    recursive: bool = True
    extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    crop_blue_sheet: bool = True
    deskew: bool = True
    min_angle_deg: float = 0.02
    max_angle_deg: float = 3.0
    allow_truncated_images: bool = True
    create_preview: bool = True
    max_preview_items: int = 180
    # Preset name ("light"/"standard"/"strong") or "" = off. Writes an EXTRA
    # viewing copy under ENHANCED_PREVIEW/; the main output stays faithful.
    enhance_preset: str = ""


@dataclass(frozen=True)
class ImageJob:
    source_path: Path
    relative_source: Path
    output_path: Path
    relative_output: Path
    enhanced_path: Path | None = None


@dataclass(frozen=True)
class ProcessRecord:
    source_rel: str
    output_rel: str
    source_bytes: int
    output_bytes: int
    original_width: int
    original_height: int
    output_width: int
    output_height: int
    original_mode: str
    output_mode: str
    original_dpi: str
    output_dpi: str
    original_compression: str
    output_compression: str
    crop_box: str
    crop_note: str
    angle_deg: str
    method: str
    fill_rgb: str
    enhanced_rel: str = ""
    enhance_preset: str = ""


def normalize_extensions(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        item = value.strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = "." + item
        result.append(item)
    return tuple(dict.fromkeys(result))


def collect_image_files(source_root: Path, options: LevelOptions) -> list[Path]:
    iterator = source_root.rglob("*") if options.recursive else source_root.glob("*")
    paths = [
        path
        for path in iterator
        if path.is_file() and path.suffix.lower() in options.extensions
    ]
    return sorted(paths, key=lambda item: str(item.relative_to(source_root)).lower())


def enhanced_preview_path(output_root: Path, relative_output: Path) -> Path:
    return (
        output_root
        / ENHANCED_PREVIEW_DIRNAME
        / relative_output.parent
        / f"{relative_output.stem}_enhanced.tif"
    )


def build_jobs(
    source_root: Path, output_root: Path, paths: list[Path], *, enhance: bool = False
) -> list[ImageJob]:
    """Build output paths and avoid collisions such as a.jpg and a.tif."""
    used: set[Path] = set()
    jobs: list[ImageJob] = []
    for source_path in paths:
        relative_source = source_path.relative_to(source_root)
        relative_output = relative_source.with_suffix(".tif")
        key = Path(str(relative_output).lower())
        if key in used:
            suffix = source_path.suffix.lower().lstrip(".")
            relative_output = relative_output.with_name(
                f"{relative_output.stem}__from_{suffix}.tif"
            )
            key = Path(str(relative_output).lower())
        used.add(key)
        jobs.append(
            ImageJob(
                source_path=source_path,
                relative_source=relative_source,
                output_path=output_root / relative_output,
                relative_output=relative_output,
                enhanced_path=enhanced_preview_path(output_root, relative_output) if enhance else None,
            )
        )
    return jobs


# Geometry helpers live in the package so the production pipeline and this
# CLI share one implementation. When run from the C:\maps repo the
# import resolves directly; otherwise we add the repo root to sys.path.
try:
    from geoscan.raster_level import (
        detect_blue_sheet_crop,
        detect_level_angle,
        median_corner_fill,
    )
except ImportError:
    _repo_root = Path(__file__).resolve().parents[1]
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from geoscan.raster_level import (
        detect_blue_sheet_crop,
        detect_level_angle,
        median_corner_fill,
    )



def process_image(job: ImageJob, options: LevelOptions) -> ProcessRecord:
    ImageFile.LOAD_TRUNCATED_IMAGES = options.allow_truncated_images

    source = job.source_path
    image0 = Image.open(source)
    original_size = image0.size
    original_mode = image0.mode
    original_dpi = image0.info.get("dpi")
    original_compression = image0.info.get("compression")

    image = image0.convert("RGB")
    if options.crop_blue_sheet:
        crop_box, crop_note = detect_blue_sheet_crop(image)
    else:
        crop_box, crop_note = (0, 0, image.size[0], image.size[1]), "disabled"
    working = image.crop(crop_box)

    angle, method = detect_level_angle(
        working,
        deskew=options.deskew,
        min_angle_deg=options.min_angle_deg,
        max_angle_deg=options.max_angle_deg,
    )
    if angle:
        fill = median_corner_fill(working)
        result = working.rotate(
            angle,
            resample=Image.Resampling.BICUBIC,
            expand=True,
            fillcolor=fill,
        )
    else:
        fill = ""
        result = working

    job.output_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(job.output_path, format="TIFF", compression="raw", dpi=options.target_dpi)

    enhanced_rel = ""
    if options.enhance_preset and job.enhanced_path is not None:
        enhancer = _load_enhance_module()
        if enhancer is None:
            raise RuntimeError(
                "--enhance 需要主仓库的 geoscan.raster_enhance 模块；"
                "请在 GeoScan 仓库内运行本工具，或不加 --enhance。"
            )
        enhanced_rgb = enhancer.enhance_rgb_array(
            np.asarray(result.convert("RGB")),
            enhancer.ENHANCE_PRESETS[options.enhance_preset],
        )
        job.enhanced_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(enhanced_rgb).save(
            job.enhanced_path, format="TIFF", compression="raw", dpi=options.target_dpi
        )
        enhanced_rel = str(
            Path(ENHANCED_PREVIEW_DIRNAME) / job.relative_output.parent / job.enhanced_path.name
        )

    return ProcessRecord(
        source_rel=str(job.relative_source),
        output_rel=str(job.relative_output),
        source_bytes=source.stat().st_size,
        output_bytes=job.output_path.stat().st_size,
        original_width=original_size[0],
        original_height=original_size[1],
        output_width=result.size[0],
        output_height=result.size[1],
        original_mode=original_mode,
        output_mode=result.mode,
        original_dpi=str(original_dpi),
        output_dpi=str(options.target_dpi),
        original_compression=str(original_compression),
        output_compression="raw",
        crop_box=str(crop_box),
        crop_note=crop_note,
        angle_deg=f"{angle:.6f}",
        method=method,
        fill_rgb=str(fill),
        enhanced_rel=enhanced_rel,
        enhance_preset=options.enhance_preset if enhanced_rel else "",
    )


def write_csv(path: Path, records: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def make_preview(records: list[ProcessRecord], output_root: Path, preview_path: Path, max_items: int) -> None:
    if not records:
        return
    from PIL import ImageDraw

    selected = records[:max_items]
    cols = 5
    thumb_w = 240
    thumb_h = 180
    label_h = 42
    rows = math.ceil(len(selected) / cols)
    sheet = Image.new("RGB", (cols * thumb_w, rows * (thumb_h + label_h)), "white")
    draw = ImageDraw.Draw(sheet)

    for index, record in enumerate(selected):
        path = output_root / record.output_rel
        try:
            thumb = Image.open(path).convert("RGB")
            thumb.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
            x = (index % cols) * thumb_w
            y = (index // cols) * (thumb_h + label_h)
            sheet.paste(thumb, (x + (thumb_w - thumb.width) // 2, y + (thumb_h - thumb.height) // 2))
            label = record.output_rel if len(record.output_rel) <= 34 else "..." + record.output_rel[-31:]
            draw.text((x + 4, y + thumb_h + 4), label, fill=(0, 0, 0))
        except Exception as exc:  # pragma: no cover - preview is non-critical
            x = (index % cols) * thumb_w
            y = (index // cols) * (thumb_h + label_h)
            draw.text((x + 4, y + 24), f"preview error: {exc}", fill=(255, 0, 0))
    sheet.save(preview_path, quality=92)


def verify_tiff_outputs(output_root: Path) -> list[tuple[str, str, str, str]]:
    issues: list[tuple[str, str, str, str]] = []
    for path in sorted(output_root.rglob("*.tif"), key=lambda item: str(item).lower()):
        try:
            image = Image.open(path)
            dpi = image.info.get("dpi")
            compression = image.info.get("compression")
            if image.mode != "RGB" or dpi != (300.0, 300.0) or compression != "raw":
                issues.append((str(path.relative_to(output_root)), image.mode, str(dpi), str(compression)))
        except Exception as exc:
            issues.append((str(path.relative_to(output_root)), "OPEN_ERROR", repr(exc), ""))
    return issues


def run_batch(source_root: Path, output_root: Path, options: LevelOptions) -> tuple[list[ProcessRecord], list[dict[str, str]]]:
    paths = collect_image_files(source_root, options)
    jobs = build_jobs(source_root, output_root, paths, enhance=bool(options.enhance_preset))
    records: list[ProcessRecord] = []
    errors: list[dict[str, str]] = []

    for index, job in enumerate(jobs, 1):
        try:
            record = process_image(job, options)
            records.append(record)
            print(
                f"[{index}/{len(jobs)}] {job.relative_source} -> {job.relative_output} "
                f"angle={record.angle_deg} size={record.output_width}x{record.output_height}"
            )
        except Exception as exc:
            errors.append(
                {
                    "source_rel": str(job.relative_source),
                    "error": repr(exc),
                    "traceback": traceback.format_exc(limit=4),
                }
            )
            print(f"ERROR [{index}/{len(jobs)}] {job.relative_source}: {exc}", file=sys.stderr)

    log_records = [record.__dict__ for record in records]
    if log_records:
        write_csv(output_root / "conversion_log.csv", log_records, list(log_records[0].keys()))
    write_csv(output_root / "conversion_errors.csv", errors, ["source_rel", "error", "traceback"])

    if options.create_preview:
        make_preview(records, output_root, output_root / "preview_contact_sheet.jpg", options.max_preview_items)

    return records, errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Level scanned images and save RGB 300dpi uncompressed TIFF files."
    )
    parser.add_argument("source", type=Path, help="Input image file or input folder.")
    parser.add_argument("output", type=Path, help="Output TIFF file or output folder.")
    parser.add_argument(
        "--ext",
        default="jpg,jpeg,png,tif,tiff,bmp",
        help="Comma-separated extensions for folder mode. Default: jpg,jpeg,png,tif,tiff,bmp",
    )
    parser.add_argument("--no-recursive", action="store_true", help="Do not scan subfolders.")
    parser.add_argument("--no-deskew", action="store_true", help="Only convert to RGB TIFF; do not rotate.")
    parser.add_argument("--no-blue-crop", action="store_true", help="Disable conservative blue-sheet crop.")
    parser.add_argument("--strict-images", action="store_true", help="Do not tolerate truncated JPEG input.")
    parser.add_argument("--no-preview", action="store_true", help="Do not create preview_contact_sheet.jpg.")
    parser.add_argument("--verify", action="store_true", help="Verify output TIFF mode, DPI, and compression.")
    parser.add_argument(
        "--enhance",
        action="store_true",
        help="Also write a visually enhanced viewing copy (sharpen/clarity) under "
        "ENHANCED_PREVIEW/. The main leveled TIFF is never modified.",
    )
    parser.add_argument(
        "--enhance-strength",
        choices=("light", "standard", "strong"),
        default="standard",
        help="Enhancement preset used with --enhance. Default: standard.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.source.resolve()
    output = args.output.resolve()
    options = LevelOptions(
        recursive=not args.no_recursive,
        extensions=normalize_extensions(args.ext.split(",")),
        crop_blue_sheet=not args.no_blue_crop,
        deskew=not args.no_deskew,
        allow_truncated_images=not args.strict_images,
        create_preview=not args.no_preview,
        enhance_preset=args.enhance_strength if args.enhance else "",
    )

    if not source.exists():
        print(f"Source does not exist: {source}", file=sys.stderr)
        return 2

    if source.is_file():
        output_path = output if output.suffix.lower() in {".tif", ".tiff"} else output / (source.stem + ".tif")
        job = ImageJob(
            source,
            Path(source.name),
            output_path,
            Path(output_path.name),
            enhanced_path=(
                enhanced_preview_path(output_path.parent, Path(output_path.name))
                if options.enhance_preset
                else None
            ),
        )
        try:
            record = process_image(job, options)
        except Exception as exc:
            print(f"ERROR {source}: {exc}", file=sys.stderr)
            return 1
        print(
            f"ok {record.source_rel} -> {record.output_rel} "
            f"angle={record.angle_deg} size={record.output_width}x{record.output_height}"
        )
        if args.verify:
            issues = verify_tiff_outputs(output_path.parent)
            if issues:
                print(f"verify issues: {issues}", file=sys.stderr)
                return 1
        return 0

    output.mkdir(parents=True, exist_ok=True)
    records, errors = run_batch(source, output, options)
    print(f"completed={len(records)} errors={len(errors)} output={output}")

    if args.verify:
        issues = verify_tiff_outputs(output)
        print(f"verified_tifs={len(list(output.rglob('*.tif')))} issues={len(issues)}")
        if issues:
            for issue in issues[:50]:
                print(f"VERIFY ISSUE {issue}", file=sys.stderr)
            return 1
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
