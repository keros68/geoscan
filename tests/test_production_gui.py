from __future__ import annotations

from pathlib import Path

import pytest

from geoscan.production_gui import (
    GuiFormState,
    ProductionGui,
    build_ai_config_from_gui,
    build_batch_config_from_gui,
    build_program_config_from_gui,
    completion_message_for_report,
    default_map_id_from_image,
    default_output_root_from_parent,
    friendly_error_message,
    redact_api_key,
    run_notice_for_state,
)


def test_gui_derives_map_id_from_common_image_name():
    assert default_map_id_from_image(Path("C:/maps/source/t01_0006.tif")) == "T01_0006"
    assert default_map_id_from_image(Path("C:/maps/source/T01_0128.JPG")) == "T01_0128"
    # Non-convention names must still auto-fill (previously returned "" -> empty field, no output).
    assert default_map_id_from_image(Path("C:/maps/source/12345.png")) == "12345"
    assert default_map_id_from_image(Path("C:/maps/source/custom.png")) == "CUSTOM"
    assert default_map_id_from_image(Path("C:/maps/source/scan_t01_0007_final.tif")) == "T01_0007"


def test_gui_default_output_root_uses_selected_parent_and_short_map_folder():
    assert default_output_root_from_parent(Path("D:/runs"), "T01_0006") == Path("D:/runs/T01_0006_P")


def test_gui_config_keeps_ai_key_out_of_plain_report_fields(tmp_path):
    state = GuiFormState(
        project_root=tmp_path,
        source_raster=tmp_path / "t01_0006.tif",
        map_id="T01_0006",
        output_parent=tmp_path / "out",
        ai_provider="openai-compatible",
        ai_base_url="https://example.test/v1",
        ai_api_key="sk-test-secret",
        ai_model="qwen-vl-max",
        conversion_mode="none",
    )

    config = build_program_config_from_gui(state)

    assert config.output_root == tmp_path / "out" / "T01_0006_P"
    assert config.ai_provider == "openai-compatible"
    assert config.ai_base_url == "https://example.test/v1"
    assert config.ai_api_key == "sk-test-secret"
    assert config.ai_model == "qwen-vl-max"
    assert redact_api_key(config.ai_api_key) == "sk-t...cret"


def test_gui_config_supplies_default_line_and_text_targets_without_override(tmp_path):
    state = GuiFormState(
        project_root=tmp_path,
        source_raster=tmp_path / "t01_0006.tif",
        map_id="T01_0006",
        output_parent=tmp_path / "out",
        conversion_mode="cli",
    )

    config = build_program_config_from_gui(state)

    assert config.target_line_file == "T06LINE.WL"
    assert config.target_text_file == "T06TXT.WT"


def test_gui_config_passes_optional_area_settings(tmp_path):
    state = GuiFormState(
        project_root=tmp_path,
        source_raster=tmp_path / "t01_0006.tif",
        map_id="T01_0006",
        output_parent=tmp_path / "out",
        conversion_mode="none",
        include_areas=True,
    )

    config = build_program_config_from_gui(state)

    assert config.include_areas is True
    assert config.target_area_file == "T06AREA.WP"


def test_gui_builds_ai_vision_config_from_form_state(tmp_path):
    state = GuiFormState(
        project_root=tmp_path,
        source_raster=tmp_path / "t01_0011.jpg",
        map_id="T01_0011",
        output_parent=tmp_path,
        ai_provider="qwen",
        ai_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        ai_api_key="sk-test-secret",
        ai_model="qwen-vl-max",
    )

    config = build_ai_config_from_gui(state)

    assert config.provider == "qwen"
    assert config.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert config.api_key == "sk-test-secret"
    assert config.model == "qwen-vl-max"
    assert config.timeout_seconds == 300


def test_gui_can_override_ai_connection_timeout_for_fast_checks(tmp_path):
    state = GuiFormState(
        project_root=tmp_path,
        source_raster=tmp_path / "t01_0011.jpg",
        map_id="T01_0011",
        output_parent=tmp_path,
        ai_provider="openai-compatible",
        ai_base_url="https://api.siliconflow.cn/v1",
        ai_api_key="sk-test-secret",
        ai_model="Qwen/Qwen3-VL-32B-Instruct",
        wait_timeout_seconds=300,
    )

    config = build_ai_config_from_gui(state, timeout_seconds=30)

    assert config.timeout_seconds == 30


def test_gui_notice_explains_no_text_candidates_means_freeze_only(tmp_path):
    notice = run_notice_for_state(
        GuiFormState(
            project_root=tmp_path,
            source_raster=tmp_path / "t01_0011.jpg",
            map_id="T01_0011",
            output_parent=tmp_path,
            conversion_mode="prepare",
        )
    )

    assert "未选择文字候选 GeoJSON" in notice
    assert "自动从输入图生成" in notice
    assert "文字占位包" in notice
    assert "不会生成 WT/WL" in notice


def test_gui_notice_explains_ai_visual_review_when_no_text_candidates(tmp_path):
    notice = run_notice_for_state(
        GuiFormState(
            project_root=tmp_path,
            source_raster=tmp_path / "t01_0011.jpg",
            map_id="T01_0011",
            output_parent=tmp_path,
            ai_provider="openai-compatible",
            ai_base_url="https://api.siliconflow.cn/v1",
            ai_api_key="sk-test-secret",
            ai_model="Qwen/Qwen3-VL-32B-Instruct",
            conversion_mode="prepare",
        )
    )

    assert "未选择文字候选 GeoJSON" in notice
    assert "自动从输入图生成" in notice
    assert "AI_VISUAL_REVIEW" in notice
    assert "不会生成 WT/WL" in notice


def test_gui_completion_summary_warns_when_run_only_prepared_batch():
    kind, message = completion_message_for_report(
        {
            "output_root": "C:/maps/T01_0013_P",
            "line_candidate_generation": {
                "feature_count": 120,
                "output_geojson": "C:/maps/T01_0013_P/04_LINE_WORKFLOW/T01_0013_review_line_candidates.geojson",
            },
            "line": {
                "output_line_count": 120,
                "dxf_export": {
                    "status": "written",
                    "path": "C:/maps/T01_0013_P/06_LINE_SECTION_W60/grouped_exchange/T13LINE_WL.dxf",
                },
            },
            "text_candidate_generation": {
                "feature_count": 22,
                "output_geojson": "C:/maps/T01_0013_P/05_TEXT_WORKFLOW/T01_0013_review_text_candidates.geojson",
            },
            "text": {
                "output_text_count": 22,
                "dxf_export": {
                    "status": "written",
                    "path": "C:/maps/T01_0013_P/07_TEXT_SECTION_W60/grouped_exchange/T13TXT_WT.dxf",
                },
            },
            "conversion": {
                "mode": "prepare",
                "status": "prepared",
                "ok": True,
            },
        }
    )

    assert kind == "warning"
    assert "线候选: 120 个" in message
    assert "线 DXF" in message
    assert "T13LINE_WL.dxf" in message
    assert "T13TXT_WT.dxf" in message
    assert "未生成 WT/WL" in message
    assert "prepare" in message


def test_gui_completion_summary_mentions_optional_area_exchange():
    kind, message = completion_message_for_report(
        {
            "output_root": "C:/maps/T01_0013_P",
            "area_candidate_generation": {
                "feature_count": 3,
                "output_geojson": "C:/maps/T01_0013_P/05_AREA_WORKFLOW/T01_0013_review_area_candidates.geojson",
            },
            "area": {
                "output_area_count": 3,
                "shp_export": {
                    "status": "written",
                    "path": "C:/maps/T01_0013_P/07_AREA_SECTION_W60/grouped_exchange/T13AREA_WP/T13AREA_WP.shp",
                },
            },
            "conversion": {
                "mode": "none",
                "status": "not_requested",
                "ok": None,
            },
        }
    )

    assert kind == "ok"
    assert "区候选: 3 个" in message
    assert "区 Shapefile" in message
    assert "T13AREA_WP.shp" in message


def test_gui_completion_summary_warns_when_cli_conversion_failed():
    kind, message = completion_message_for_report(
        {
            "output_root": "C:/maps/T01_0013_P",
            "conversion": {
                "mode": "cli",
                "status": "conversion_incomplete",
                "ok": False,
                "pipeline": {
                    "convert": {
                        "error": "SECTION batch DXF directory dialog did not appear",
                    }
                },
            },
        }
    )

    assert kind == "warning"
    assert "转换未完成" in message
    assert "未生成 WT/WL" in message
    assert "SECTION batch DXF directory dialog did not appear" in message


def test_gui_completion_summary_prefers_nested_conversion_error_over_generic_note():
    kind, message = completion_message_for_report(
        {
            "output_root": "C:/maps/T01_0015_P",
            "conversion": {
                "mode": "cli",
                "status": "conversion_incomplete",
                "ok": False,
                "pipeline": {
                    "status": "convert_failed",
                    "convert": {
                        "status": "automation_failed",
                        "error": "RuntimeError: SECTION batch DXF directory dialog did not appear",
                    },
                },
                "note": "No Computer Use fallback is accepted; failed bridge conversion remains incomplete.",
            },
        }
    )

    assert kind == "warning"
    assert "SECTION batch DXF directory dialog did not appear" in message
    assert "No Computer Use fallback" not in message


def test_gui_file_exists_error_tells_user_how_to_rerun():
    message = friendly_error_message(FileExistsError("Output root already contains files: E:/x/T01_0011_P"))

    assert "输出文件夹已经存在" in message
    assert "覆盖已有输出" in message
    assert "换一个 Map ID" in message


def test_gui_config_passes_line_engine_repair_and_ocr_python(tmp_path):
    state = GuiFormState(
        project_root=tmp_path,
        source_raster=tmp_path / "t01_0006.tif",
        map_id="T01_0006",
        output_parent=tmp_path / "out",
        conversion_mode="none",
        line_engine="trace",
        line_repair="conservative",
        line_export_source="repaired",
        ocr_python=tmp_path / "python.exe",
    )

    config = build_program_config_from_gui(state)

    assert config.line_engine == "trace"
    assert config.line_repair == "conservative"
    assert config.line_export_source == "repaired"
    assert config.ocr_python == tmp_path / "python.exe"


def test_gui_batch_config_reuses_form_options(tmp_path):
    state = GuiFormState(
        project_root=tmp_path,
        source_raster=tmp_path / "t01_0006.tif",
        map_id="T01_0006",
        output_parent=tmp_path,
        conversion_mode="none",
        line_engine="trace",
        ai_provider="openai-compatible",
        ai_base_url="https://example.test/v1",
        ai_api_key="sk-test-secret",
        ai_model="qwen-vl-max",
        include_areas=True,
    )
    rasters = (tmp_path / "t01_0001.tif", tmp_path / "t01_0002.tif")

    config = build_batch_config_from_gui(
        state, source_rasters=rasters, retry_incomplete=True, limit=5
    )

    assert config.project_root == tmp_path
    assert config.source_rasters == rasters
    assert config.line_engine == "trace"
    assert config.conversion_mode == "none"
    assert config.ai_api_key == "sk-test-secret"
    assert config.include_areas is True
    assert config.retry_incomplete is True
    assert config.limit == 5


def test_gui_window_initializes_without_name_error(monkeypatch):
    monkeypatch.delenv("MAPGIS_OCR_PYTHON", raising=False)
    try:
        app = ProductionGui()
    except Exception as exc:
        if exc.__class__.__name__ == "TclError":
            pytest.skip(f"Tk display is not available: {exc}")
        raise
    try:
        assert app.notebook is not None
        assert app.log_tab is not None
        assert app.batch_tab is not None
        assert app.batch_tree is not None
        assert app.line_engine_var.get() == "trace"
        assert app.line_repair_var.get() == "conservative"
        assert app.line_export_source_var.get() == "repaired"
        assert app.ocr_python_var.get() == ""
        assert app.conversion_mode_var.get() == "cli"
        assert app.ai_provider_var.get() == "none"
        assert app.ai_base_url_var.get() == "https://api.siliconflow.cn/v1/chat/completions"
        assert app.ai_model_var.get() == "Qwen/Qwen3-VL-32B-Instruct"
        assert app.ai_api_key_var.get() == ""
    finally:
        app.destroy()


def test_gui_has_menu_bar_and_full_tab_names(monkeypatch):
    monkeypatch.delenv("MAPGIS_OCR_PYTHON", raising=False)
    try:
        app = ProductionGui()
    except Exception as exc:
        if exc.__class__.__name__ == "TclError":
            pytest.skip(f"Tk display is not available: {exc}")
        raise
    try:
        # 专业外壳：常规菜单栏存在，且不再有线框图时期的假数据组件。
        assert str(app.cget("menu"))
        for fake_widget in ("stage_labels", "project_tree", "canvas_placeholder", "inspector_frame"):
            assert not hasattr(app, fake_widget)
        assert app.notebook.tab(app.log_tab, "text") == "运行日志"
        assert app.notebook.tab(app.batch_tab, "text") == "批量运行"
    finally:
        app.destroy()


def test_default_project_root_prefers_saved_setting(tmp_path, monkeypatch):
    from geoscan import production_gui

    saved = tmp_path / "work"
    saved.mkdir()
    assert production_gui.default_project_root({"project_root": str(saved)}) == saved

    monkeypatch.setattr(production_gui, "DEFAULT_PROJECT_ROOT", tmp_path / "missing")
    monkeypatch.setattr(production_gui.sys, "frozen", False, raising=False)
    assert (
        production_gui.default_project_root({"project_root": str(tmp_path / "nope")})
        == Path.cwd()
    )


def test_default_project_root_falls_back_to_dev_default(tmp_path, monkeypatch):
    from geoscan import production_gui

    dev_root = tmp_path / "dev_root"
    dev_root.mkdir()
    monkeypatch.setattr(production_gui, "DEFAULT_PROJECT_ROOT", dev_root)
    assert production_gui.default_project_root({}) == dev_root
