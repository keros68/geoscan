"""Visual enhancement: human-viewing backdrop (pipeline opt-in) + level-tool --enhance.

OCR A/B on real maps (t01_0001/0003/0013, 2026-07-04, docs/OCR_ENHANCE_AB_20260704.md)
showed enhancement REDUCES rapidocr detections, so OCR/line/text stages never read
an enhanced raster. Enhancement is used ONLY for a human-viewing backdrop
(*_mapgis_pixel_units_enhanced.tif), generated from the pixel-unit raster so its
geometry matches the vectors 1:1 — for easier manual editing in MapGIS.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from geoscan.raster_enhance import (
    ENHANCE_PRESETS,
    enhance_image_file,
    enhance_rgb_array,
)

LEVEL_TOOL = (
    Path(__file__).resolve().parents[1]
    / "jpg_rgb_tiff_level_tool"
    / "raster_level_rgb_tiff.py"
)


def _washed_out_map(width: int = 640, height: int = 480) -> np.ndarray:
    """Low-contrast synthetic scan: pale lines on a gray-beige page."""
    rgb = np.full((height, width, 3), 208, dtype=np.uint8)
    rgb[:, :, 2] = 190  # slight yellow cast, like old paper
    rgb[100:104, 40:600] = 150  # faint horizontal line
    rgb[40:440, 300:303] = 155  # faint vertical line
    return rgb


def test_presets_and_shape_preserved() -> None:
    rgb = _washed_out_map()
    for name, options in ENHANCE_PRESETS.items():
        out = enhance_rgb_array(rgb, options)
        assert out.shape == rgb.shape, name
        assert out.dtype == np.uint8, name
    assert set(ENHANCE_PRESETS) == {"light", "standard", "strong"}


def test_enhancement_is_deterministic() -> None:
    rgb = _washed_out_map()
    first = enhance_rgb_array(rgb, ENHANCE_PRESETS["standard"])
    second = enhance_rgb_array(rgb, ENHANCE_PRESETS["standard"])
    assert np.array_equal(first, second)


def test_enhancement_increases_line_contrast() -> None:
    rgb = _washed_out_map()
    out = enhance_rgb_array(rgb, ENHANCE_PRESETS["standard"])

    def line_contrast(image: np.ndarray) -> float:
        gray = image.mean(axis=2)
        line = gray[101:103, 100:500].mean()
        background = gray[150:200, 100:500].mean()
        return background - line

    assert line_contrast(out) > line_contrast(rgb) * 1.15


def test_enhance_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="RGB"):
        enhance_rgb_array(np.zeros((10, 10), dtype=np.uint8), ENHANCE_PRESETS["standard"])
    with pytest.raises(ValueError, match="preset"):
        enhance_image_file(Path("x.tif"), Path("y.tif"), preset="extreme")


def test_enhance_image_file_writes_copy_and_never_touches_source(tmp_path: Path) -> None:
    source = tmp_path / "map.tif"
    Image.fromarray(_washed_out_map(), mode="RGB").save(
        source, format="TIFF", dpi=(300, 300), compression="raw"
    )
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    target = tmp_path / "map_enhanced.tif"

    report = enhance_image_file(source, target, preset="standard", dpi=(25.4, 25.4))

    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_sha
    assert report["purpose"] == "human_viewing_backdrop_only"
    assert report["geometry_changed"] is False
    assert report["preset"] == "standard"
    with Image.open(target) as image:
        assert image.size == (640, 480)
        assert image.mode == "RGB"
        assert image.info.get("dpi") == (25.4, 25.4)


def test_ocr_and_vectorization_never_enhance() -> None:
    """A/B evidence: enhancement reduces rapidocr detections, so OCR/line/text
    stages must read the plain raster. Enhancement is only allowed for the
    human-viewing backdrop (production_program, from the pixel-unit raster)."""
    for module_name in (
        "text_candidate_workflow",
        "extract_lines",
        "trace_lines",
        "line_candidate_workflow",
    ):
        import geoscan

        source = (
            Path(geoscan.__file__).parent / f"{module_name}.py"
        ).read_text(encoding="utf-8")
        assert "raster_enhance" not in source, module_name


def test_enhanced_backdrop_comes_from_pixel_unit_raster() -> None:
    """The backdrop must overlay the vectors 1:1 -> generated from the pixel-unit
    raster at pixel-unit dpi, so its geometry matches the vectors exactly."""
    import geoscan

    source = (
        Path(geoscan.__file__).parent / "production_program.py"
    ).read_text(encoding="utf-8")
    assert "enhance_image_file(" in source
    assert "pixel_unit_raster," in source
    assert "dpi=(PIXEL_UNIT_DPI, PIXEL_UNIT_DPI)" in source


def _load_level_tool():
    import sys

    spec = importlib.util.spec_from_file_location("raster_level_rgb_tiff", LEVEL_TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    # Register before exec: dataclasses resolves the module's postponed
    # annotations via sys.modules[cls.__module__].
    sys.modules["raster_level_rgb_tiff"] = module
    spec.loader.exec_module(module)
    return module


def test_level_tool_enhance_writes_preview_and_keeps_main_output_faithful(tmp_path: Path) -> None:
    tool = _load_level_tool()
    source_root = tmp_path / "scans"
    source_root.mkdir()
    Image.fromarray(_washed_out_map(), mode="RGB").save(source_root / "t01_0001.jpg", quality=95)

    plain_root = tmp_path / "out_plain"
    tool.run_batch(source_root, plain_root, tool.LevelOptions(create_preview=False))

    enhanced_root = tmp_path / "out_enhanced"
    records, errors = tool.run_batch(
        source_root,
        enhanced_root,
        tool.LevelOptions(create_preview=False, enhance_preset="standard"),
    )

    assert not errors
    # Main leveled output is byte-identical with or without --enhance.
    assert (plain_root / "t01_0001.tif").read_bytes() == (enhanced_root / "t01_0001.tif").read_bytes()
    preview = enhanced_root / "ENHANCED_PREVIEW" / "t01_0001_enhanced.tif"
    assert preview.is_file()
    assert records[0].enhanced_rel == str(Path("ENHANCED_PREVIEW") / "t01_0001_enhanced.tif")
    assert records[0].enhance_preset == "standard"
    with Image.open(preview) as image:
        assert image.mode == "RGB"
        assert image.info.get("dpi") == (300.0, 300.0)


def test_level_tool_without_enhance_writes_no_preview_folder(tmp_path: Path) -> None:
    tool = _load_level_tool()
    source_root = tmp_path / "scans"
    source_root.mkdir()
    Image.fromarray(_washed_out_map(), mode="RGB").save(source_root / "t01_0002.jpg", quality=95)

    output_root = tmp_path / "out"
    records, errors = tool.run_batch(
        source_root, output_root, tool.LevelOptions(create_preview=False)
    )

    assert not errors
    assert not (output_root / "ENHANCED_PREVIEW").exists()
    assert records[0].enhanced_rel == ""


def test_level_tool_cli_flags() -> None:
    tool = _load_level_tool()
    args = tool.parse_args(["in", "out", "--enhance", "--enhance-strength", "light"])
    assert args.enhance is True
    assert args.enhance_strength == "light"
    args = tool.parse_args(["in", "out"])
    assert args.enhance is False


# ---- pipeline enhanced-backdrop wiring ----


def test_enhanced_preview_default_is_standard_everywhere() -> None:
    from geoscan.batch_runner import BatchConfig
    from geoscan.production_gui import GuiFormState
    from geoscan.production_program import ProgramConfig

    assert ProgramConfig(
        project_root=Path("."), source_raster=Path("x.jpg"), map_id="T01_0001"
    ).enhanced_preview == "standard"
    assert BatchConfig(project_root=Path("."), source_rasters=()).enhanced_preview == "standard"
    assert GuiFormState(
        project_root=Path("."),
        source_raster=Path("x.jpg"),
        map_id="T01_0001",
        output_parent=Path("."),
    ).enhanced_preview == "standard"


def test_run_program_rejects_bad_enhanced_preview(tmp_path: Path) -> None:
    from geoscan.production_program import (
        ProgramConfig,
        run_production_program,
    )

    with pytest.raises(ValueError, match="enhanced_preview"):
        run_production_program(
            ProgramConfig(
                project_root=tmp_path,
                source_raster=tmp_path / "x.tif",
                map_id="T01_9999",
                enhanced_preview="extreme",
            )
        )


def test_cli_and_gui_route_enhanced_preview() -> None:
    from geoscan import batch_runner, production_program
    from geoscan.production_gui import (
        GuiFormState,
        build_batch_config_from_gui,
        build_program_config_from_gui,
    )

    args = production_program.build_arg_parser().parse_args(
        ["run", "--source-raster", "x.jpg", "--map-id", "T01_0001", "--enhanced-preview", "light"]
    )
    assert args.enhanced_preview == "light"
    args = batch_runner.build_arg_parser().parse_args(
        ["run", "--source-dir", "scans", "--enhanced-preview", "none"]
    )
    assert args.enhanced_preview == "none"

    state = GuiFormState(
        project_root=Path("C:/maps"),
        source_raster=Path("C:/maps/x.jpg"),
        map_id="T01_0001",
        output_parent=Path("C:/maps"),
        enhanced_preview="strong",
    )
    assert build_program_config_from_gui(state).enhanced_preview == "strong"
    assert (
        build_batch_config_from_gui(state, source_rasters=(Path("E:/x.jpg"),)).enhanced_preview
        == "strong"
    )


def test_load_ready_ships_enhanced_backdrop_when_present(tmp_path: Path) -> None:
    from geoscan.production_program import _write_mapgis_load_ready

    output_root = tmp_path / "T01_9999_P"
    freeze = output_root / "00_INPUT_FREEZE"
    freeze.mkdir(parents=True)
    raster = freeze / "t01_9999_mapgis_pixel_units.tif"
    enhanced = freeze / "t01_9999_mapgis_pixel_units_enhanced.tif"
    for path in (raster, enhanced):
        Image.fromarray(_washed_out_map(64, 48)).save(
            path, format="TIFF", dpi=(25.4, 25.4), compression="raw"
        )

    report = _write_mapgis_load_ready(
        output_root=output_root,
        map_id="T01_9999",
        raster_alignment={
            "pixel_unit_raster": str(raster),
            "enhanced_preview": {"target": str(enhanced), "preset": "standard"},
        },
        conversion_report={"mode": "none", "ok": None, "status": "not_requested"},
    )

    assert report["enhanced_backdrop"] is not None
    copied = Path(report["enhanced_backdrop"]["destination"])
    assert copied.is_file() and copied.parent.name == "MAPGIS_LOAD_READY"
    readme = Path(report["readme"]).read_text(encoding="utf-8")
    assert "overlay 1:1" in readme


def test_load_ready_no_enhanced_backdrop_when_absent(tmp_path: Path) -> None:
    from geoscan.production_program import _write_mapgis_load_ready

    output_root = tmp_path / "T01_9998_P"
    freeze = output_root / "00_INPUT_FREEZE"
    freeze.mkdir(parents=True)
    raster = freeze / "t01_9998_mapgis_pixel_units.tif"
    Image.fromarray(_washed_out_map(64, 48)).save(
        raster, format="TIFF", dpi=(25.4, 25.4), compression="raw"
    )

    report = _write_mapgis_load_ready(
        output_root=output_root,
        map_id="T01_9998",
        raster_alignment={"pixel_unit_raster": str(raster)},
        conversion_report={"mode": "none", "ok": None, "status": "not_requested"},
    )

    assert report["enhanced_backdrop"] is None
