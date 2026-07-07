import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from geoscan.engine_host import (
    EngineHost,
    Protocol,
    STAGE_KEYS,
    StageTracker,
    _StdoutToLog,
    form_state_from_args,
    load_candidates,
    preflight,
    run_summary,
    stage_states_from_report,
    validate_form_state,
)


class _Sink:
    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass

    def payloads(self):
        return [json.loads(line) for line in self.lines if line.strip()]


def _make_proto():
    sink = _Sink()
    return Protocol(sink), sink


def test_protocol_reply_fail_event_shapes():
    proto, sink = _make_proto()
    proto.reply(3, {"a": 1})
    proto.fail(4, "bad")
    proto.event("log", level="info", message="你好")
    replies = sink.payloads()
    assert replies[0] == {"id": 3, "ok": True, "data": {"a": 1}}
    assert replies[1] == {"id": 4, "ok": False, "error": "bad"}
    assert replies[2] == {"event": "log", "data": {"level": "info", "message": "你好"}}
    # Chinese must be emitted as UTF-8 text, not \u escapes (readability in logs).
    assert "你好" in sink.lines[2]


def test_stdout_to_log_buffers_partial_lines():
    proto, sink = _make_proto()
    capture = _StdoutToLog(proto)
    capture.write("part1 ")
    capture.write("part2\nnext")
    events = sink.payloads()
    assert len(events) == 1
    assert events[0]["data"]["message"] == "part1 part2"
    capture.write("\n")
    assert sink.payloads()[1]["data"]["message"] == "next"


def test_form_state_from_args_defaults_and_types(tmp_path):
    state = form_state_from_args(
        {
            "source_raster": str(tmp_path / "t01_0004.tif"),
            "map_id": "T01_0004",
            "project_root": str(tmp_path),
            "output_parent": str(tmp_path),
            "line_bridge_gap_px": "80",
            "include_areas": True,
        }
    )
    assert state.map_id == "T01_0004"
    assert state.line_bridge_gap_px == 80.0
    assert state.include_areas is True
    assert state.conversion_mode == "cli"
    assert state.line_engine == "trace"
    assert state.line_connect == "standard"
    assert state.export_dxf is True


def test_validate_form_state_reports_first_error(tmp_path):
    raster = tmp_path / "t01_0004.tif"
    raster.write_bytes(b"fake")
    base = {
        "source_raster": str(raster),
        "map_id": "T01_0004",
        "project_root": str(tmp_path),
        "output_parent": str(tmp_path),
        "conversion_mode": "none",
    }
    assert validate_form_state(form_state_from_args(base)) is None

    missing = dict(base, source_raster=str(tmp_path / "nope.tif"))
    assert "输入图片" in validate_form_state(form_state_from_args(missing))

    bad_combo = dict(base, line_export_source="repaired", line_repair="off")
    assert "repaired" in validate_form_state(form_state_from_args(bad_combo))

    bad_gap = dict(base, line_bridge_gap_px=-5)
    assert "不能为负数" in validate_form_state(form_state_from_args(bad_gap))


def test_preflight_skips_unselected_output_tool_checks(monkeypatch):
    """No MapGIS/DXF output selected means no MapGIS/GDAL probes should run."""
    import geoscan.engine_host as engine_host
    import geoscan.env_probe as env_probe
    import geoscan.production_accuracy_workflow as accuracy

    def fail_probe(*_args, **_kwargs):
        raise AssertionError("unselected output category was probed")

    monkeypatch.setattr(engine_host, "read_machine_settings", lambda: {})
    monkeypatch.setattr(env_probe, "program_candidates", fail_probe)
    monkeypatch.setattr(env_probe, "dongle_process_running", fail_probe)
    monkeypatch.setattr(accuracy, "resolve_ogr2ogr", fail_probe)

    report = preflight(conversion_mode="none", export_dxf=False)
    checks = {check["key"]: check for check in report["checks"]}

    assert report["overall"] != "blocked"
    assert checks["section"]["state"] == "skip"
    assert checks["w60"]["state"] == "skip"
    assert checks["ogr2ogr"]["state"] == "skip"
    assert checks["dongle"]["state"] == "skip"


def test_preflight_uses_configured_dongle_process_name(monkeypatch, tmp_path):
    import geoscan.engine_host as engine_host
    import geoscan.env_probe as env_probe
    import geoscan.production_accuracy_workflow as accuracy

    monkeypatch.delenv("MAPGIS67_DONGLE_PROCESS_NAME", raising=False)
    section = tmp_path / "section.exe"
    w60 = tmp_path / "W60_Conv.exe"
    ogr = tmp_path / "ogr2ogr.exe"
    for path in (section, w60, ogr):
        path.write_bytes(b"x")
    seen = []

    monkeypatch.setattr(
        engine_host,
        "read_machine_settings",
        lambda: {"dongle_process_name": r"C:\mapgis67\dog\SimDog.exe"},
    )
    monkeypatch.setattr(
        env_probe,
        "program_candidates",
        lambda program: {"section": [section], "w60_conv": [w60]}[program],
    )
    monkeypatch.setattr(env_probe, "dongle_process_running", lambda process_name=None: seen.append(process_name) or False)
    monkeypatch.setattr(accuracy, "resolve_ogr2ogr", lambda: ogr)

    report = preflight(conversion_mode="cli", export_dxf=True)
    checks = {check["key"]: check for check in report["checks"]}

    assert seen == ["SimDog.exe"]
    assert checks["dongle"]["label"] == "MapGIS 密码狗 (SimDog.exe)"
    assert checks["dongle"]["state"] == "warn"


def test_stage_states_from_successful_report(tmp_path):
    load_folder = tmp_path / "MAPGIS_LOAD_READY"
    load_folder.mkdir()
    report = {
        "line_candidate_generation": {"ok": True, "feature_count": 12},
        "text_candidate_generation": {"ok": True, "feature_count": 3},
        "line": {"dxf_export": {"path": "line.dxf"}},
        "text": {"dxf_export": {"path": "text.dxf"}},
        "conversion": {"status": "converted", "ok": True, "mode": "cli"},
        "mapgis_load_ready": {"load_folder": str(load_folder)},
    }
    states = stage_states_from_report(report)
    assert states["00_INPUT_FREEZE"] == "completed"
    assert states["04_LINE_WORKFLOW"] == "completed"
    assert states["05_TEXT_WORKFLOW"] == "completed"
    assert states["DXF_EXPORT"] == "completed"
    assert states["08_SECTION_W60"] == "completed"
    assert states["MAPGIS_LOAD_READY"] == "completed"


def test_stage_states_prepare_is_skipped_not_failed():
    report = {
        "line_candidate_generation": {"ok": True},
        "text_candidate_generation": {"ok": True},
        "line": {"dxf_export": {"path": "line.dxf"}},
        "text": {"dxf_export": {"path": "text.dxf"}},
        "conversion": {"status": "prepared", "mode": "prepare"},
    }
    states = stage_states_from_report(report)
    assert states["08_SECTION_W60"] == "skipped"
    assert states["DXF_EXPORT"] == "completed"


def test_stage_states_prepare_failure_is_failed_not_skipped():
    # prepare was explicitly requested; a failed prepare is a real failure.
    # Every other surface (completion message, batch row, exit code) already
    # treats it that way — the rail must agree, for legacy reports (no
    # "outcome" key, derived) and stamped ones alike.
    for status in ("prepare_failed", "dxf_not_exported"):
        report = {
            "line_candidate_generation": {"ok": True},
            "text_candidate_generation": {"ok": True},
            "conversion": {"mode": "prepare", "status": status, "ok": False},
        }
        assert stage_states_from_report(report)["08_SECTION_W60"] == "failed", status

        report["conversion"]["outcome"] = "failed"
        assert stage_states_from_report(report)["08_SECTION_W60"] == "failed", status


def test_stage_states_no_exchange_package_is_skipped_not_failed():
    # Zero exportable candidates means nothing to convert — that is a skip,
    # not a bridge failure. Legacy reports carry no "outcome" key, so both
    # the stamped and the derived paths must agree.
    report = {
        "line_candidate_generation": {"ok": True, "feature_count": 0},
        "text_candidate_generation": {"ok": True, "feature_count": 0},
        "conversion": {"mode": "cli", "status": "no_exchange_package", "ok": None},
    }
    assert stage_states_from_report(report)["08_SECTION_W60"] == "skipped"

    report["conversion"]["outcome"] = "skipped"
    assert stage_states_from_report(report)["08_SECTION_W60"] == "skipped"


def test_stage_states_missing_text_dxf_is_a_failure():
    # Repo rule: line AND text DXF must always be produced — a missing DXF is
    # a bug, so half an export must never read as "completed".
    report = {
        "line_candidate_generation": {"ok": True},
        "text_candidate_generation": {"ok": True},
        "line": {"dxf_export": {"path": "line.dxf"}},
        "text": {},
        "conversion": {"status": "prepared", "mode": "prepare"},
    }
    assert stage_states_from_report(report)["DXF_EXPORT"] == "failed"


def test_stage_states_no_dxf_at_all_is_skipped():
    report = {
        "line_candidate_generation": {"ok": True},
        "text_candidate_generation": {"ok": True},
        "line": {},
        "text": {},
    }
    assert stage_states_from_report(report)["DXF_EXPORT"] == "skipped"


def test_stage_states_failed_conversion_blocks_load_ready(tmp_path):
    report = {
        "line_candidate_generation": {"ok": True},
        "text_candidate_generation": {"ok": True},
        "line": {"dxf_export": {"path": "line.dxf"}},
        "conversion": {"status": "failed", "ok": False, "mode": "cli"},
        "mapgis_load_ready": {"load_folder": str(tmp_path / "missing")},
    }
    states = stage_states_from_report(report)
    assert states["08_SECTION_W60"] == "failed"
    assert states["MAPGIS_LOAD_READY"] == "blocked"


def test_stage_tracker_scan_marks_dirs_and_finish_freezes(tmp_path):
    proto, sink = _make_proto()
    tracker = StageTracker(tmp_path, proto)
    (tmp_path / "00_INPUT_FREEZE").mkdir()
    tracker.scan()
    (tmp_path / "04_LINE_WORKFLOW").mkdir()
    tracker.scan()
    states = {
        event["data"]["stage"]: event["data"]["state"]
        for event in sink.payloads()
        if event.get("event") == "stage"
    }
    assert states["00_INPUT_FREEZE"] == "completed"
    assert states["04_LINE_WORKFLOW"] == "running"

    tracker.finish(None, cancelled=True)
    states = {
        event["data"]["stage"]: event["data"]["state"]
        for event in sink.payloads()
        if event.get("event") == "stage"
    }
    assert states["04_LINE_WORKFLOW"] == "cancelled"
    assert states["MAPGIS_LOAD_READY"] == "cancelled"

    # A scan racing in after finish() must never resurrect "running":
    # the done-check inside the lock freezes the rail at its final states.
    events_before = len(sink.lines)
    tracker.scan()
    assert len(sink.lines) == events_before


def test_load_candidates_flips_y_and_reads_text_bboxes(tmp_path):
    lines_path = tmp_path / "lines.geojson"
    texts_path = tmp_path / "texts.geojson"
    lines_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "LineString", "coordinates": [[10, 90], [20, 80]]},
                        "properties": {},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    texts_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": None,
                        "properties": {
                            "bbox_left_px": 5,
                            "bbox_top_px": 6,
                            "bbox_right_px": 30,
                            "bbox_bottom_px": 18,
                            "text": "Qn2",
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "PROGRAM_RUN_REPORT.json").write_text(
        json.dumps(
            {
                "line_candidate_generation": {"output_geojson": str(lines_path)},
                "text_candidate_generation": {"output_geojson": str(texts_path)},
            }
        ),
        encoding="utf-8",
    )
    result = load_candidates(tmp_path, image_height=100)
    # Map-space y=90 with height 100 -> image row 10 (y-down for the canvas).
    assert result["lines"] == [[[10.0, 10.0], [20.0, 20.0]]]
    # The report's own raster size must win over the caller-provided height.
    report_path = tmp_path / "PROGRAM_RUN_REPORT.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["raster_alignment"] = {"source_size_px": [200, 190]}
    report_path.write_text(json.dumps(report), encoding="utf-8")
    result = load_candidates(tmp_path, image_height=100)
    assert result["lines"] == [[[10.0, 100.0], [20.0, 110.0]]]
    result = load_candidates(tmp_path, image_height=100)
    assert result["texts"][0]["text"] == "Qn2"
    assert result["texts"][0]["top"] == 6.0
    assert result["dropped_lines"] == 0


def test_run_summary_without_report(tmp_path):
    assert run_summary(tmp_path) == {"has_report": False}


def test_list_history_finds_run_folders(tmp_path):
    from geoscan.engine_host import list_history

    run_dir = tmp_path / "T01_0004_P"
    run_dir.mkdir()
    (run_dir / "PROGRAM_RUN_REPORT.json").write_text(
        json.dumps({"map_id": "T01_0004", "line_candidate_generation": {"feature_count": 7}}),
        encoding="utf-8",
    )
    (tmp_path / "not_a_run").mkdir()
    rows = list_history(tmp_path)
    assert len(rows) == 1
    assert rows[0]["name"] == "T01_0004_P"
    assert rows[0]["line_candidates"] == 7


def test_count_checked_yes_reads_real_property(tmp_path):
    from geoscan.engine_host import _count_checked_yes

    lines_path = tmp_path / "lines.geojson"
    lines_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "geometry": None, "properties": {"checked": "no"}},
                    {"type": "Feature", "geometry": None, "properties": {"checked": "yes"}},
                    {"type": "Feature", "geometry": None, "properties": {}},
                ],
            }
        ),
        encoding="utf-8",
    )
    report = {"line_candidate_generation": {"output_geojson": str(lines_path)}}
    assert _count_checked_yes(report) == 1
    assert _count_checked_yes({}) == 0


def test_save_settings_without_save_key_never_touches_stored_key(monkeypatch, tmp_path):
    import geoscan.engine_host as engine_host

    key_calls = []
    monkeypatch.setattr(engine_host, "save_settings", lambda settings: tmp_path / "settings.json")
    monkeypatch.setattr(engine_host, "apply_settings_to_env", lambda settings, override: {})
    monkeypatch.setattr(engine_host, "save_encrypted_api_key", lambda value: key_calls.append(value))

    proto, sink = _make_proto()
    host = EngineHost(proto)
    # Tool-paths-only save (the console settings dialog): key must be untouched.
    host.handle({"id": 1, "cmd": "save_settings", "args": {"settings": {}}})
    assert sink.payloads()[0]["ok"] is True
    assert key_calls == []
    # Explicit save_key=True stores the key; explicit save_key=False clears it.
    host.handle({"id": 2, "cmd": "save_settings", "args": {"settings": {}, "save_key": True, "ai_api_key": "sk-x"}})
    host.handle({"id": 3, "cmd": "save_settings", "args": {"settings": {}, "save_key": False}})
    assert key_calls == ["sk-x", ""]


def test_run_single_releases_busy_lock_when_setup_fails(monkeypatch, tmp_path):
    import geoscan.engine_host as engine_host

    raster = tmp_path / "t01_0004.tif"
    raster.write_bytes(b"fake")
    form = {
        "source_raster": str(raster),
        "map_id": "T01_0004",
        "project_root": str(tmp_path),
        "output_parent": str(tmp_path),
        "conversion_mode": "none",
    }

    def boom(state):
        raise RuntimeError("config build exploded")

    monkeypatch.setattr(engine_host, "build_program_config_from_gui", boom)
    proto, sink = _make_proto()
    host = EngineHost(proto)
    host.handle({"id": 1, "cmd": "run_single", "args": {"form": form}})
    assert sink.payloads()[0]["ok"] is False
    # The lock must be free again or every later run is rejected forever.
    assert host._busy.acquire(blocking=False)
    host._busy.release()


def test_get_settings_never_returns_plaintext_key(monkeypatch):
    import geoscan.engine_host as engine_host

    monkeypatch.setattr(engine_host, "read_machine_settings", lambda: {})
    monkeypatch.setattr(engine_host, "load_encrypted_api_key", lambda: "sk-super-secret")
    proto, sink = _make_proto()
    host = EngineHost(proto)
    host.handle({"id": 1, "cmd": "get_settings"})
    reply = sink.payloads()[0]
    assert reply["ok"] is True
    assert "sk-super-secret" not in json.dumps(reply)
    assert reply["data"]["has_saved_key"] is True


def test_engine_host_handle_ping_and_unknown():
    proto, sink = _make_proto()
    host = EngineHost(proto)
    host.handle({"id": 1, "cmd": "ping"})
    host.handle({"id": 2, "cmd": "no_such_cmd"})
    payloads = sink.payloads()
    assert payloads[0]["ok"] is True
    assert payloads[0]["data"]["app"] == "geoscan"
    assert payloads[1]["ok"] is False


def test_engine_host_run_single_rejects_invalid_form():
    proto, sink = _make_proto()
    host = EngineHost(proto)
    host.handle({"id": 5, "cmd": "run_single", "args": {"form": {"source_raster": "Z:/nope.tif"}}})
    reply = sink.payloads()[0]
    assert reply["ok"] is False
    assert "输入图片" in reply["error"]


def test_resolve_ogr2ogr_stale_env_path_falls_back_to_bundle(monkeypatch, tmp_path):
    """A saved/env ogr2ogr path that no longer exists must not shadow the
    bundled gdal/ next to the frozen exe (colleague-machine regression)."""
    from geoscan import production_accuracy_workflow as paw

    bundle = tmp_path / "gdal"
    bundle.mkdir()
    (bundle / "ogr2ogr.exe").write_bytes(b"x")
    monkeypatch.setattr(paw, "bundled_gdal_dir", lambda: bundle)

    monkeypatch.setenv("MAPGIS_OGR2OGR", str(tmp_path / "uninstalled_qgis" / "ogr2ogr.exe"))
    assert paw.resolve_ogr2ogr() == bundle / "ogr2ogr.exe"

    # A VALID configured path still wins over the bundle.
    real = tmp_path / "qgis_ogr2ogr.exe"
    real.write_bytes(b"x")
    monkeypatch.setenv("MAPGIS_OGR2OGR", str(real))
    assert paw.resolve_ogr2ogr() == real

    # No bundle + stale env: surface the configured path for the error message.
    monkeypatch.setattr(paw, "bundled_gdal_dir", lambda: None)
    stale = tmp_path / "uninstalled_qgis" / "ogr2ogr.exe"
    monkeypatch.setenv("MAPGIS_OGR2OGR", str(stale))
    assert paw.resolve_ogr2ogr() == stale


def test_apply_engine_update_flow(monkeypatch, tmp_path):
    import types

    from geoscan import updater

    info = updater.UpdateInfo(current="0.1.0", latest="0.2.0", update_available=True, kind="engine")
    calls = []
    fake = types.SimpleNamespace(
        check_for_update=lambda: info,
        download_engine=lambda i, progress=None: (progress and progress(50, 100)) or tmp_path,
        apply_engine_update=lambda staging: calls.append(("apply", str(staging))),
        UpdateError=updater.UpdateError,
    )
    monkeypatch.setitem(sys.modules, "geoscan.updater", fake)
    import geoscan as _g

    monkeypatch.setattr(_g, "updater", fake, raising=False)

    proto, sink = _make_proto()
    host = EngineHost(proto)
    host.handle({"id": 1, "cmd": "apply_engine_update"})
    payloads = sink.payloads()
    reply = next(p for p in payloads if p.get("id") == 1)
    assert reply["ok"] is True
    assert reply["data"]["applied"] == "0.2.0"
    assert calls == [("apply", str(tmp_path))]
    assert any(p.get("event") == "update_progress" for p in payloads)
    # The busy lock must be free again afterwards.
    assert host._busy.acquire(blocking=False)
    host._busy.release()


def test_apply_engine_update_rejects_when_not_engine_kind(monkeypatch):
    import types

    from geoscan import updater

    info = updater.UpdateInfo(current="0.1.0", latest="0.2.0", update_available=True, kind="installer")
    fake = types.SimpleNamespace(check_for_update=lambda: info, UpdateError=updater.UpdateError)
    monkeypatch.setitem(sys.modules, "geoscan.updater", fake)
    import geoscan as _g

    monkeypatch.setattr(_g, "updater", fake, raising=False)

    proto, sink = _make_proto()
    host = EngineHost(proto)
    host.handle({"id": 2, "cmd": "apply_engine_update"})
    reply = sink.payloads()[0]
    assert reply["ok"] is False
    assert "完整安装包" in reply["error"]
    assert host._busy.acquire(blocking=False)
    host._busy.release()


def test_inject_saved_ai_key_only_when_form_has_none(monkeypatch, tmp_path):
    import geoscan.engine_host as engine_host

    monkeypatch.setattr(engine_host, "load_encrypted_api_key", lambda: "sk-stored")
    base = {
        "source_raster": str(tmp_path / "a.tif"),
        "map_id": "A",
        "project_root": str(tmp_path),
        "output_parent": str(tmp_path),
        "ai_provider": "openai-compatible",
    }
    state = engine_host.EngineHost._inject_saved_ai_key(form_state_from_args(base))
    assert state.ai_api_key == "sk-stored"
    # An explicit key in the form wins; provider none never injects.
    explicit = engine_host.EngineHost._inject_saved_ai_key(
        form_state_from_args(dict(base, ai_api_key="sk-form"))
    )
    assert explicit.ai_api_key == "sk-form"
    none_provider = engine_host.EngineHost._inject_saved_ai_key(
        form_state_from_args(dict(base, ai_provider="none"))
    )
    assert none_provider.ai_api_key == ""


def test_save_settings_merges_with_stored(monkeypatch, tmp_path):
    import geoscan.engine_host as engine_host

    saved = {}
    monkeypatch.setattr(
        engine_host, "read_machine_settings", lambda: {"section_exe": "C:/mapgis/section.exe", "ai_model": "old"}
    )
    monkeypatch.setattr(engine_host, "save_settings", lambda s: saved.update(s) or (tmp_path / "s.json"))
    monkeypatch.setattr(engine_host, "apply_settings_to_env", lambda s, override: {})

    proto, sink = _make_proto()
    host = EngineHost(proto)
    host.handle({"id": 1, "cmd": "save_settings", "args": {"settings": {"ai_model": "new-model"}}})
    assert sink.payloads()[0]["ok"] is True
    # AI-only save keeps the tool path AND updates the AI field.
    assert saved["section_exe"] == "C:/mapgis/section.exe"
    assert saved["ai_model"] == "new-model"


def test_test_ai_connection_validates_and_uses_saved_key(monkeypatch):
    import geoscan.engine_host as engine_host

    monkeypatch.setattr(engine_host, "load_encrypted_api_key", lambda: "sk-stored")
    captured = {}

    import geoscan.ai_vision_review as ai_review

    monkeypatch.setattr(ai_review, "test_ai_connection", lambda config: captured.update(key=config.api_key) or {"api_url": "https://x/v1/chat/completions"})

    proto, sink = _make_proto()
    host = EngineHost(proto)
    host.handle({"id": 1, "cmd": "test_ai_connection", "args": {"ai_provider": "none"}})
    assert sink.payloads()[0]["ok"] is False  # provider required

    host.handle(
        {
            "id": 2,
            "cmd": "test_ai_connection",
            "args": {
                "ai_provider": "openai-compatible",
                "ai_base_url": "https://api.siliconflow.cn/v1/chat/completions",
                "ai_model": "Qwen/Qwen3-VL",
            },
        }
    )
    reply = next(p for p in sink.payloads() if p.get("id") == 2)
    assert reply["ok"] is True
    assert captured["key"] == "sk-stored"
    # The key must never appear in the protocol stream.
    assert "sk-stored" not in "".join(json.dumps(p) for p in sink.payloads())


def test_engine_host_one_shot_pipe_gets_reply_before_exit():
    """A piped client that sends one request then EOF must still get the reply
    (main() joins in-flight request threads after stdin closes)."""
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-m", "geoscan.engine_host"],
        input=json.dumps({"id": 9, "cmd": "ping"}) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(repo_root),
        timeout=60,
    )
    replies = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert any(payload.get("id") == 9 and payload.get("ok") for payload in replies)


def test_engine_host_subprocess_ping_roundtrip():
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(
        [sys.executable, "-m", "geoscan.engine_host"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(repo_root),
    )
    try:
        process.stdin.write(json.dumps({"id": 1, "cmd": "ping"}) + "\n")
        process.stdin.flush()
        reply = None
        for _ in range(50):
            line = process.stdout.readline()
            if not line:
                break
            payload = json.loads(line)
            if payload.get("id") == 1:
                reply = payload
                break
        assert reply is not None, "engine host never answered ping"
        assert reply["ok"] is True
        assert reply["data"]["app"] == "geoscan"
    finally:
        process.kill()
