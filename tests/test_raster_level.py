"""Input leveling (jpg/png/bmp -> deskewed RGB TIFF) integrated into the pipeline.

Colleagues' source scans are often raw jpg; the pipeline now levels them itself
(previously leveling was a separate out-of-band tool). The original is always
frozen unchanged; vectorization reads the leveled raster.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from geoscan.production_program import (
    ProgramConfig,
    _copy_input_freeze,
    run_production_program,
)
from geoscan.raster_level import (
    LevelParams,
    detect_level_angle,
    level_to_rgb_tiff,
    needs_leveling,
)


def _framed_scan(width: int = 900, height: int = 700, tilt: bool = False) -> np.ndarray:
    """White page with a dark rectangular map frame (deskew evidence)."""
    rgb = np.full((height, width, 3), 245, dtype=np.uint8)
    top, bottom, left, right = 60, height - 60, 60, width - 60
    rgb[top : top + 4, left:right] = 20
    rgb[bottom - 4 : bottom, left:right] = 20
    rgb[top:bottom, left : left + 4] = 20
    rgb[top:bottom, right - 4 : right] = 20
    if tilt:
        img = Image.fromarray(rgb).rotate(-1.2, resample=Image.Resampling.BICUBIC,
                                          expand=False, fillcolor=(245, 245, 245))
        rgb = np.asarray(img)
    return rgb


def test_needs_leveling_modes(tmp_path: Path) -> None:
    jpg = tmp_path / "scan.jpg"
    tif = tmp_path / "scan.tif"
    assert needs_leveling(jpg, "auto") is True
    assert needs_leveling(tif, "auto") is False  # already the expected container
    assert needs_leveling(tif, "force") is True
    assert needs_leveling(jpg, "off") is False
    with pytest.raises(ValueError, match="auto|force|off"):
        needs_leveling(jpg, "bogus")


def test_level_to_rgb_tiff_writes_rgb_300dpi_and_keeps_source(tmp_path: Path) -> None:
    source = tmp_path / "scan.jpg"
    Image.fromarray(_framed_scan()).save(source, quality=95)
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "leveled.tif"

    record = level_to_rgb_tiff(source, target, LevelParams())

    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha  # source untouched
    with Image.open(target) as image:
        assert image.mode == "RGB"
        assert image.info.get("dpi") == (300.0, 300.0)
    assert record["output_mode"] == "RGB"


def test_deskew_uses_frame_and_skips_when_straight(tmp_path: Path) -> None:
    straight = Image.fromarray(_framed_scan(tilt=False))
    angle, method = detect_level_angle(straight)
    assert abs(angle) < 0.05  # nothing to rotate on an already-straight frame

    tilted = Image.fromarray(_framed_scan(tilt=True))
    angle2, method2 = detect_level_angle(tilted)
    assert abs(angle2) > 0.2  # detects the ~1.2 deg tilt from the frame
    assert "frame" in method2


def test_copy_input_freeze_levels_jpg_and_freezes_original(tmp_path: Path) -> None:
    source = tmp_path / "t01_0001.jpg"
    Image.fromarray(_framed_scan()).save(source, quality=95)
    output_root = tmp_path / "T01_0001_P"

    report = _copy_input_freeze(
        source_raster=source, output_root=output_root, map_id="T01_0001", level_input="auto"
    )

    frozen = Path(report["frozen_raster"])
    working = Path(report["working_raster"])
    assert frozen.suffix == ".jpg" and working.suffix == ".tif"
    assert working != frozen
    assert report["leveling"] is not None
    assert report["mode"] == "RGB"
    # Report dimensions reflect the leveled (working) raster, not the jpg.
    with Image.open(working) as image:
        assert (report["width"], report["height"]) == image.size


def test_copy_input_freeze_passes_tiff_through_in_auto(tmp_path: Path) -> None:
    source = tmp_path / "t01_0002.tif"
    Image.fromarray(_framed_scan()).save(source, format="TIFF", dpi=(300, 300), compression="raw")
    output_root = tmp_path / "T01_0002_P"

    report = _copy_input_freeze(
        source_raster=source, output_root=output_root, map_id="T01_0002", level_input="auto"
    )

    assert report["leveling"] is None
    assert Path(report["working_raster"]) == Path(report["frozen_raster"])


def test_copy_input_freeze_force_levels_even_tiff(tmp_path: Path) -> None:
    source = tmp_path / "t01_0003.tif"
    Image.fromarray(_framed_scan()).save(source, format="TIFF", dpi=(300, 300), compression="raw")
    output_root = tmp_path / "T01_0003_P"

    report = _copy_input_freeze(
        source_raster=source, output_root=output_root, map_id="T01_0003", level_input="force"
    )

    assert report["leveling"] is not None
    assert Path(report["working_raster"]).name.endswith("_source_leveled.tif")


def test_run_program_rejects_bad_level_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="level_input"):
        run_production_program(
            ProgramConfig(
                project_root=tmp_path,
                source_raster=tmp_path / "x.tif",
                map_id="T01_9999",
                level_input="bogus",
            )
        )


def test_cli_and_config_defaults_level_off() -> None:
    """Leveling is opt-in: default off so already-processed images are never
    re-leveled during a vectorization run. The user selects auto/force per run."""
    from geoscan import batch_runner, production_program

    assert ProgramConfig(
        project_root=Path("."), source_raster=Path("x.tif"), map_id="T01_0001"
    ).level_input == "off"
    assert batch_runner.BatchConfig(
        project_root=Path("."), source_rasters=()
    ).level_input == "off"
    assert production_program.build_arg_parser().parse_args(
        ["run", "--source-raster", "x.jpg", "--map-id", "T01_0001"]
    ).level_input == "off"
    assert batch_runner.build_arg_parser().parse_args(
        ["run", "--source-dir", "scans"]
    ).level_input == "off"

    args = production_program.build_arg_parser().parse_args(
        ["run", "--source-raster", "x.jpg", "--map-id", "T01_0001", "--level-input", "force"]
    )
    assert args.level_input == "force"

    args = batch_runner.build_arg_parser().parse_args(
        ["run", "--source-dir", "scans", "--level-input", "off"]
    )
    assert args.level_input == "off"


def test_gui_routes_level_input_into_both_configs() -> None:
    from geoscan.production_gui import (
        GuiFormState,
        build_batch_config_from_gui,
        build_program_config_from_gui,
    )

    state = GuiFormState(
        project_root=Path("C:/maps"),
        source_raster=Path("C:/maps/x.jpg"),
        map_id="T01_0001",
        output_parent=Path("C:/maps"),
        level_input="force",
    )
    assert build_program_config_from_gui(state).level_input == "force"
    assert (
        build_batch_config_from_gui(state, source_rasters=(Path("E:/x.jpg"),)).level_input
        == "force"
    )
