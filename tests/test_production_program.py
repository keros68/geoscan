from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from geoscan.production_program import (
    ProgramConfig,
    build_arg_parser,
    default_line_target_file,
    default_text_target_file,
    run_production_program,
)

def _write_test_raster(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (16, 10), "white")
    image.save(path, dpi=(300, 300))


def _write_line_test_raster(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (220, 140), "white")
    draw = ImageDraw.Draw(image)
    draw.line((20, 30, 200, 30), fill="black", width=2)
    draw.line((30, 110, 190, 110), fill="black", width=2)
    draw.line((60, 20, 60, 120), fill="black", width=2)
    image.save(path, dpi=(300, 300))


def _write_sample_text_candidates(path: Path) -> None:
    payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "candidate_id": "title_001",
                    "ocr_text": "蛋白石矿床7勘探线剖面图",
                    "category": "title_text",
                },
                "geometry": {"type": "Point", "coordinates": [120.0, 88.0]},
            },
            {
                "type": "Feature",
                "properties": {
                    "candidate_id": "blank_001",
                    "ocr_text": "",
                    "category": "sample_table_text",
                },
                "geometry": {"type": "Point", "coordinates": [40.0, 20.0]},
            },
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _generated_text_candidate_report(path: Path, *, feature_count: int) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_sample_text_candidates(path)
    return {
        "mode": "auto",
        "ok": True,
        "output_geojson": str(path),
        "review_csv": str(path.with_suffix(".csv")),
        "feature_count": feature_count,
        "fallback_used": False,
        "writes_checked_yes": False,
    }


def test_default_text_target_file_uses_current_map_id():
    assert default_text_target_file("T01_0006") == "T06TXT.WT"
    assert default_text_target_file("T01_0128") == "T28TXT.WT"
    assert default_text_target_file("CUSTOM") == "TEXTTXT.WT"


def test_default_line_target_file_uses_current_map_id():
    assert default_line_target_file("T01_0006") == "T06LINE.WL"
    assert default_line_target_file("T01_0128") == "T28LINE.WL"
    assert default_line_target_file("CUSTOM") == "LINE.WL"


def test_program_cli_safe_defaults():
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "run",
            "--project-root",
            "C:/maps",
            "--source-raster",
            "C:/maps/scans/t01_0006.tif",
            "--map-id",
            "T01_0006",
        ]
    )

    assert args.ai_provider == "none"
    assert args.conversion_mode == "prepare"
    assert args.computer_use_allowed is False


def test_program_creates_fresh_run_package_without_ai_or_computer_use(tmp_path):
    source_raster = tmp_path / "source" / "t01_0099.tif"
    text_candidates = tmp_path / "source" / "review_text.geojson"
    output_root = tmp_path / "T01_0099_P"
    _write_test_raster(source_raster)
    _write_sample_text_candidates(text_candidates)

    report = run_production_program(
        ProgramConfig(
            project_root=tmp_path,
            source_raster=source_raster,
            map_id="T01_0099",
            output_root=output_root,
            text_candidates=text_candidates,
            target_text_file="T99TXT.WT",
            conversion_mode="none",
            export_dxf=False,
        )
    )

    assert report["program"] == "mapgis_accuracy_workflow"
    assert report["fresh_run_acceptance"]["input_freeze_created"] is True
    assert report["fresh_run_acceptance"]["old_candidate_inputs_used"] is False
    assert report["fresh_run_acceptance"]["old_mapgis_ready_used"] is False
    assert report["ai"]["required_steps"] == 0
    assert report["ai"]["provider"] == "none"
    assert report["computer_use"]["allowed"] is False
    assert report["conversion"]["mode"] == "none"
    assert report["conversion"]["status"] == "not_requested"
    assert report["text"]["source_text_count"] == 2
    assert report["text"]["placeholder_text_count"] == 1

    frozen_manifest = output_root / "00_INPUT_FREEZE" / "INPUT_MANIFEST.json"
    pixel_unit_raster = output_root / "00_INPUT_FREEZE" / "t01_0099_mapgis_pixel_units.tif"
    raster_alignment_report = output_root / "00_INPUT_FREEZE" / "RASTER_ALIGNMENT_REPORT.json"
    load_ready_raster = output_root / "MAPGIS_LOAD_READY" / "t01_0099_mapgis_pixel_units.tif"
    load_ready_readme = output_root / "MAPGIS_LOAD_READY" / "README_MAPGIS_LOAD.md"
    run_report = output_root / "PROGRAM_RUN_REPORT.json"
    readme = output_root / "WORKFLOW_PROGRAM_README.md"
    conversion_list = output_root / "07_TEXT_SECTION_W60" / "CONVERSION_LIST.txt"

    assert frozen_manifest.exists()
    assert pixel_unit_raster.exists()
    assert raster_alignment_report.exists()
    assert load_ready_raster.exists()
    assert load_ready_readme.exists()
    assert run_report.exists()
    assert readme.exists()
    assert conversion_list.exists()
    with Image.open(pixel_unit_raster) as image:
        assert image.size == (16, 10)
        assert image.info["dpi"] == (25.4, 25.4)

    saved_report = json.loads(run_report.read_text(encoding="utf-8"))
    assert saved_report["output_root"] == str(output_root)
    assert saved_report["raster_alignment"]["target_dpi"] == [25.4, 25.4]
    assert saved_report["raster_alignment"]["pixel_unit_extent"] == [0.0, 0.0, 16.0, 10.0]
    assert "do not use the original 300 dpi TIFF" in saved_report["raster_alignment"]["mapgis_import_note"]
    assert saved_report["mapgis_load_ready"]["load_folder"] == str(output_root / "MAPGIS_LOAD_READY")
    assert saved_report["mapgis_load_ready"]["raster"]["destination"] == str(load_ready_raster)
    assert "mapgis_pixel_units.tif" in load_ready_readme.read_text(encoding="utf-8")
    assert "python -m geoscan.production_program run" in readme.read_text(
        encoding="utf-8"
    )


def test_program_runs_ai_visual_review_when_provider_is_configured(tmp_path, monkeypatch):
    source_raster = tmp_path / "source" / "t01_0098.tif"
    output_root = tmp_path / "T01_0098_P"
    _write_test_raster(source_raster)
    calls: list[dict[str, object]] = []

    def fake_analyze_map_image_with_ai(config, *, image_path, output_root, map_id):
        calls.append(
            {
                "provider": config.provider,
                "model": config.model,
                "image_path": Path(image_path),
                "output_root": Path(output_root),
                "map_id": map_id,
            }
        )
        ai_dir = Path(output_root) / "AI_VISUAL_REVIEW"
        ai_dir.mkdir(parents=True, exist_ok=True)
        analysis_path = ai_dir / "ai_visual_analysis.json"
        analysis_path.write_text(
            json.dumps({"review_only": True, "map_id": map_id}, ensure_ascii=False),
            encoding="utf-8",
        )
        return {
            "ok": True,
            "review_only": True,
            "analysis_path": str(analysis_path),
            "writes_coordinates": False,
            "writes_checked_yes": False,
        }

    monkeypatch.setattr(
        "geoscan.production_program.analyze_map_image_with_ai",
        fake_analyze_map_image_with_ai,
    )

    report = run_production_program(
        ProgramConfig(
            project_root=tmp_path,
            source_raster=source_raster,
            map_id="T01_0098",
            output_root=output_root,
            ai_provider="openai-compatible",
            ai_base_url="https://api.siliconflow.cn/v1",
            ai_api_key="sk-test-secret",
            ai_model="Qwen/Qwen3-VL-32B-Instruct",
            conversion_mode="none",
        )
    )

    assert len(calls) == 1
    assert calls[0]["provider"] == "openai-compatible"
    assert calls[0]["model"] == "Qwen/Qwen3-VL-32B-Instruct"
    assert Path(calls[0]["image_path"]).name == "t01_0098_source_frozen.tif"
    assert report["ai"]["required_steps"] == 0
    assert report["ai"]["visual_review"]["ok"] is True
    assert report["ai"]["visual_review"]["writes_coordinates"] is False
    assert report["ai"]["visual_review"]["writes_checked_yes"] is False
    assert (output_root / "AI_VISUAL_REVIEW" / "ai_visual_analysis.json").exists()


def test_program_records_ai_visual_review_failure_without_aborting_fresh_run(tmp_path, monkeypatch):
    source_raster = tmp_path / "source" / "t01_0097.tif"
    output_root = tmp_path / "T01_0097_P"
    _write_test_raster(source_raster)

    def fake_analyze_map_image_with_ai(*_args, **_kwargs):
        raise RuntimeError("AI API call failed: timeout")

    monkeypatch.setattr(
        "geoscan.production_program.analyze_map_image_with_ai",
        fake_analyze_map_image_with_ai,
    )

    report = run_production_program(
        ProgramConfig(
            project_root=tmp_path,
            source_raster=source_raster,
            map_id="T01_0097",
            output_root=output_root,
            ai_provider="openai-compatible",
            ai_base_url="https://api.siliconflow.cn/v1",
            ai_api_key="sk-test-secret",
            ai_model="Qwen/Qwen3-VL-32B-Instruct",
            conversion_mode="none",
        )
    )

    assert report["input"]["input_freeze_created"] is True
    assert report["ai"]["visual_review"]["ok"] is False
    assert "timeout" in report["ai"]["visual_review"]["error"]
    assert (output_root / "PROGRAM_RUN_REPORT.json").exists()
    assert (output_root / "AI_VISUAL_REVIEW" / "AI_VISUAL_REVIEW_REPORT.json").exists()


def test_program_auto_generates_text_candidates_when_none_are_supplied(tmp_path, monkeypatch):
    source_raster = tmp_path / "source" / "t01_0096.tif"
    output_root = tmp_path / "T01_0096_P"
    _write_test_raster(source_raster)
    calls: list[dict[str, object]] = []

    def fake_generate_review_text_candidates(*, source_raster, output_root, map_id, ocr_python=None):
        generated = Path(output_root) / "05_TEXT_WORKFLOW" / f"{map_id}_review_text_candidates.geojson"
        calls.append({"source_raster": Path(source_raster), "output_root": Path(output_root), "map_id": map_id})
        return _generated_text_candidate_report(generated, feature_count=2)

    monkeypatch.setattr(
        "geoscan.production_program.generate_review_text_candidates",
        fake_generate_review_text_candidates,
        raising=False,
    )

    report = run_production_program(
        ProgramConfig(
            project_root=tmp_path,
            source_raster=source_raster,
            map_id="T01_0096",
            output_root=output_root,
            conversion_mode="none",
            export_dxf=False,
        )
    )

    assert len(calls) == 1
    assert Path(calls[0]["source_raster"]).name == "t01_0096_source_frozen.tif"
    assert report["text_candidate_generation"]["mode"] == "auto"
    assert report["text_candidate_generation"]["feature_count"] == 2
    assert report["text"]["source_text_count"] == 2
    assert report["text"]["output_text_count"] == 2
    assert "05_TEXT_WORKFLOW" in report["text"]["source_geojson_input"]


def test_program_auto_generates_line_candidates_and_line_dxf(tmp_path):
    source_raster = tmp_path / "source" / "t01_0093.tif"
    output_root = tmp_path / "T01_0093_P"
    _write_line_test_raster(source_raster)

    report = run_production_program(
        ProgramConfig(
            project_root=tmp_path,
            source_raster=source_raster,
            map_id="T01_0093",
            output_root=output_root,
            conversion_mode="none",
            auto_generate_text_candidates=False,
        )
    )

    assert report["line_candidate_generation"]["feature_count"] > 0
    assert report["line"]["target_file"] == "T93LINE.WL"
    assert report["line"]["output_line_count"] > 0
    assert report["line"]["dxf_export"]["status"] == "written"
    assert Path(report["line"]["dxf_export"]["path"]).is_file()
    assert Path(report["line"]["dxf_export"]["path"]).stat().st_size > 0
    assert report["conversion"]["status"] == "not_requested"


def test_program_uses_supplied_text_candidates_without_auto_generation(tmp_path, monkeypatch):
    source_raster = tmp_path / "source" / "t01_0095.tif"
    text_candidates = tmp_path / "source" / "review_text.geojson"
    output_root = tmp_path / "T01_0095_P"
    _write_test_raster(source_raster)
    _write_sample_text_candidates(text_candidates)

    def fail_if_auto_generation_runs(**_kwargs):
        raise AssertionError("auto generation should not run when text candidates are supplied")

    monkeypatch.setattr(
        "geoscan.production_program.generate_review_text_candidates",
        fail_if_auto_generation_runs,
        raising=False,
    )

    report = run_production_program(
        ProgramConfig(
            project_root=tmp_path,
            source_raster=source_raster,
            map_id="T01_0095",
            output_root=output_root,
            text_candidates=text_candidates,
            conversion_mode="none",
            export_dxf=False,
        )
    )

    assert report["text_candidate_generation"]["mode"] == "provided"
    assert report["text_candidate_generation"]["feature_count"] == 2
    assert report["text"]["source_geojson_input"] == str(text_candidates.resolve())


def test_program_reports_prepare_blocked_when_dxf_export_is_disabled(tmp_path):
    source_raster = tmp_path / "source" / "t01_0094.tif"
    text_candidates = tmp_path / "source" / "review_text.geojson"
    output_root = tmp_path / "T01_0094_P"
    _write_test_raster(source_raster)
    _write_sample_text_candidates(text_candidates)

    report = run_production_program(
        ProgramConfig(
            project_root=tmp_path,
            source_raster=source_raster,
            map_id="T01_0094",
            output_root=output_root,
            text_candidates=text_candidates,
            conversion_mode="prepare",
            export_dxf=False,
        )
    )

    assert report["text"]["dxf_export"]["status"] == "skipped"
    assert report["conversion"]["status"] == "dxf_not_exported"
    assert report["conversion"]["ok"] is False


def test_program_refuses_non_short_output_root(tmp_path):
    source_raster = tmp_path / "source" / "t01_0099.tif"
    _write_test_raster(source_raster)

    with pytest.raises(ValueError, match="short output root"):
        run_production_program(
            ProgramConfig(
                project_root=tmp_path,
                source_raster=source_raster,
                map_id="T01_0099",
                output_root=tmp_path / "T01_0099_LONG_EXPERIMENT_NAME",
            )
        )


def test_program_allows_custom_parent_when_output_folder_keeps_short_map_name(tmp_path):
    source_raster = tmp_path / "source" / "t01_0099.tif"
    output_root = tmp_path / "custom_parent" / "T01_0099_P"
    _write_test_raster(source_raster)

    report = run_production_program(
        ProgramConfig(
            project_root=tmp_path,
            source_raster=source_raster,
            map_id="T01_0099",
            output_root=output_root,
            conversion_mode="none",
        )
    )

    assert report["output_root"] == str(output_root.resolve())


def test_main_exits_nonzero_when_conversion_failed(monkeypatch, tmp_path, capsys):
    from geoscan import production_program as pp

    monkeypatch.setattr(
        pp,
        "run_production_program",
        lambda config: {"conversion": {"mode": "cli", "ok": False, "status": "conversion_incomplete"}},
    )
    source = tmp_path / "t01_0001.tif"
    source.write_bytes(b"x")

    with pytest.raises(SystemExit) as excinfo:
        pp.main(
            [
                "run",
                "--project-root",
                str(tmp_path),
                "--source-raster",
                str(source),
                "--map-id",
                "T01_0001",
            ]
        )

    assert excinfo.value.code == 2
    assert "CONVERSION FAILED" in capsys.readouterr().err


def test_main_returns_normally_when_conversion_not_requested(monkeypatch, tmp_path):
    from geoscan import production_program as pp

    monkeypatch.setattr(
        pp,
        "run_production_program",
        lambda config: {"conversion": {"mode": "none", "ok": None, "status": "not_requested"}},
    )
    source = tmp_path / "t01_0001.tif"
    source.write_bytes(b"x")

    pp.main(
        [
            "run",
            "--project-root",
            str(tmp_path),
            "--source-raster",
            str(source),
            "--map-id",
            "T01_0001",
        ]
    )


def test_load_ready_marks_incomplete_when_cli_conversion_failed(tmp_path):
    from geoscan.production_program import (
        _staging_ready_dir,
        _write_mapgis_load_ready,
    )

    output_root = tmp_path / "T01_0001_P"
    _staging_ready_dir(output_root).mkdir(parents=True)
    raster = tmp_path / "t01_0001_mapgis_pixel_units.tif"
    raster.write_bytes(b"tif-bytes")

    report = _write_mapgis_load_ready(
        output_root=output_root,
        map_id="T01_0001",
        raster_alignment={"pixel_unit_raster": str(raster)},
        conversion_report={"mode": "cli", "ok": False, "status": "conversion_incomplete"},
    )

    assert report["complete"] is False
    load_dir = output_root / "MAPGIS_LOAD_READY"
    assert (load_dir / "INCOMPLETE_DO_NOT_USE.txt").is_file()
    assert "INCOMPLETE" in (load_dir / "README_MAPGIS_LOAD.md").read_text(encoding="utf-8")


def test_load_ready_complete_requires_nonempty_converted_files(tmp_path):
    from geoscan.production_program import (
        _staging_ready_dir,
        _write_mapgis_load_ready,
    )

    output_root = tmp_path / "T01_0001_P"
    ready_dir = _staging_ready_dir(output_root)
    ready_dir.mkdir(parents=True)
    raster = tmp_path / "t01_0001_mapgis_pixel_units.tif"
    raster.write_bytes(b"tif-bytes")
    (ready_dir / "T01LINE.WL").write_bytes(b"wl-bytes")
    (ready_dir / "T01TXT.WT").write_bytes(b"")

    report = _write_mapgis_load_ready(
        output_root=output_root,
        map_id="T01_0001",
        raster_alignment={"pixel_unit_raster": str(raster)},
        conversion_report={"mode": "cli", "ok": True, "status": "converted"},
    )

    assert report["skipped_empty_files"] == [str(ready_dir / "T01TXT.WT")]
    assert report["complete"] is False
    assert not (output_root / "MAPGIS_LOAD_READY" / "T01TXT.WT").exists()

    (ready_dir / "T01TXT.WT").write_bytes(b"wt-bytes")
    report = _write_mapgis_load_ready(
        output_root=output_root,
        map_id="T01_0001",
        raster_alignment={"pixel_unit_raster": str(raster)},
        conversion_report={"mode": "cli", "ok": True, "status": "converted"},
    )

    assert report["complete"] is True
    assert not (output_root / "MAPGIS_LOAD_READY" / "INCOMPLETE_DO_NOT_USE.txt").exists()


def test_load_ready_is_single_folder_with_wl_wt_and_dxf(tmp_path):
    """One deliverable folder: raster + WL/WT + DXF together. The verified-file
    staging area lives under 08_SECTION_W60, not as a top-level sibling."""
    from geoscan.production_program import (
        _staging_ready_dir,
        _write_mapgis_load_ready,
    )

    output_root = tmp_path / "T01_0001_P"
    ready_dir = _staging_ready_dir(output_root)
    ready_dir.mkdir(parents=True)
    (ready_dir / "T01LINE.WL").write_bytes(b"wl")
    (ready_dir / "T01TXT.WT").write_bytes(b"wt")
    raster = tmp_path / "t01_0001_mapgis_pixel_units.tif"
    raster.write_bytes(b"tif")
    line_dxf = output_root / "06_LINE_SECTION_W60" / "T01LINE.dxf"
    text_dxf = output_root / "07_TEXT_SECTION_W60" / "T01TXT.dxf"
    for path, data in ((line_dxf, b"line-dxf"), (text_dxf, b"text-dxf")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    report = _write_mapgis_load_ready(
        output_root=output_root,
        map_id="T01_0001",
        raster_alignment={"pixel_unit_raster": str(raster)},
        conversion_report={"mode": "cli", "ok": True, "status": "converted"},
        line_report={"dxf_export": {"path": str(line_dxf), "status": "written"}},
        text_report={"dxf_export": {"path": str(text_dxf), "status": "written"}},
    )

    load_dir = output_root / "MAPGIS_LOAD_READY"
    # WL/WT + DXF + raster all in the single deliverable folder.
    for name in ("T01LINE.WL", "T01TXT.WT", "T01LINE.dxf", "T01TXT.dxf"):
        assert (load_dir / name).is_file(), name
    assert {r["kind"] for r in report["dxf_files"]} == {"line_dxf", "text_dxf"}
    # Staging is nested, not a top-level MAPGIS_READY sibling.
    assert not (output_root / "MAPGIS_READY").exists()
    assert ready_dir.parent.name == "08_SECTION_W60"


def test_reset_output_backs_up_load_ready_without_rasters(tmp_path):
    from geoscan.production_program import _ensure_fresh_output_root

    output_root = tmp_path / "T01_0001_P"
    load_dir = output_root / "MAPGIS_LOAD_READY"
    load_dir.mkdir(parents=True)
    (load_dir / "T01LINE.WL").write_bytes(b"hand-edited")
    (load_dir / "t01_0001_mapgis_pixel_units.tif").write_bytes(b"big raster")

    backup_info = _ensure_fresh_output_root(output_root, reset_output=True)

    assert backup_info is not None
    backup_root = Path(backup_info["backup_root"])
    assert (backup_root / "MAPGIS_LOAD_READY" / "T01LINE.WL").read_bytes() == b"hand-edited"
    assert not list(backup_root.rglob("*.tif"))
    assert output_root.exists()
    assert not any(output_root.iterdir())


def test_reset_output_without_load_ready_creates_no_backup(tmp_path):
    from geoscan.production_program import _ensure_fresh_output_root

    output_root = tmp_path / "T01_0001_P"
    (output_root / "04_LINE_WORKFLOW").mkdir(parents=True)

    backup_info = _ensure_fresh_output_root(output_root, reset_output=True)

    assert backup_info is None
    assert not (tmp_path / "T01_0001_P_LAST_READY_BACKUP").exists()


def test_run_cancelled_at_stage_boundary(tmp_path):
    from geoscan.production_program import (
        ProgramConfig,
        RunCancelledError,
        run_production_program,
    )

    source = tmp_path / "t01_0001.tif"
    _write_test_raster(source)

    with pytest.raises(RunCancelledError, match="input_freeze"):
        run_production_program(
            ProgramConfig(
                project_root=tmp_path,
                source_raster=source,
                map_id="T01_0001",
                conversion_mode="none",
            ),
            should_stop=lambda: True,
        )

    output_root = tmp_path / "T01_0001_P"
    assert not (output_root / "PROGRAM_RUN_REPORT.json").exists()


def test_run_cancelled_after_first_stage(tmp_path):
    from geoscan.production_program import (
        ProgramConfig,
        RunCancelledError,
        run_production_program,
    )

    source = tmp_path / "t01_0001.tif"
    _write_test_raster(source)
    calls = {"count": 0}

    def stop_after_first_check() -> bool:
        calls["count"] += 1
        return calls["count"] > 1

    with pytest.raises(RunCancelledError, match="line_candidates"):
        run_production_program(
            ProgramConfig(
                project_root=tmp_path,
                source_raster=source,
                map_id="T01_0001",
                conversion_mode="none",
            ),
            should_stop=stop_after_first_check,
        )

    # The frozen input from the completed first stage exists; no run report.
    output_root = tmp_path / "T01_0001_P"
    assert (output_root / "00_INPUT_FREEZE").is_dir()
    assert not (output_root / "PROGRAM_RUN_REPORT.json").exists()
