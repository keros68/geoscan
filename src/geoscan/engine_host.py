"""JSONL engine host: the application boundary for desktop shells.

Long-lived process speaking JSON-Lines over stdio, designed for the Tauri/React
GeoScan Control Console (see docs/superpowers/specs/2026-07-05-geoscan-ui-
architecture-design.md). The shell submits validated commands and receives
structured events; it never imports production modules directly.

Protocol
--------
Request  (stdin, one JSON object per line):
    {"id": 7, "cmd": "run_single", "args": {...}}
Response (stdout):
    {"id": 7, "ok": true, "data": {...}}   |   {"id": 7, "ok": false, "error": "..."}
Event    (stdout, no id — may arrive any time):
    {"event": "log",    "data": {"level": "info", "message": "..."}}
    {"event": "stage",  "data": {"stage": "04_LINE_WORKFLOW", "state": "running"}}
    {"event": "status", "data": {"state": "running", "label": "线候选"}}
    {"event": "batch_row", "data": {...}}
    {"event": "result", "data": {"kind": "ok|warning|error|cancelled", ...}}

Anything the pipeline prints to stdout is re-emitted as ``log`` events, so the
protocol stream stays valid JSONL. Events never contain API keys (redacted at
the source). All candidates stay checked=no; nothing here writes geological
content.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from geoscan import __version__
from geoscan.app_settings import (
    apply_settings_to_env,
    load_encrypted_api_key,
    read_machine_settings,
    save_encrypted_api_key,
    save_settings,
    settings_save_path,
)
from geoscan.batch_runner import discover_source_rasters
from geoscan.run_form import (
    DEFAULT_LINE_CONNECT,
    DEFAULT_LINE_ENGINE,
    DEFAULT_LINE_EXPORT_SOURCE,
    DEFAULT_LINE_REPAIR,
    GuiFormState,
    autodetect_tool_paths,
    build_ai_config_from_gui,
    build_batch_config_from_gui,
    build_program_config_from_gui,
    completion_message_for_report,
    default_map_id_from_image,
    default_output_root_from_parent,
    default_project_root,
    friendly_error_message,
    invalid_settings_paths,
    run_notice_for_state,
    validate_form_state,
)
from geoscan.production_program import (
    RunCancelledError,
    conversion_outcome,
    redact_api_key,
    run_production_program,
)

STAGES: tuple[tuple[str, str], ...] = (
    ("00_INPUT_FREEZE", "输入冻结"),
    ("04_LINE_WORKFLOW", "线候选"),
    ("05_TEXT_WORKFLOW", "文字候选"),
    ("DXF_EXPORT", "交换文件"),
    ("08_SECTION_W60", "MapGIS 转换"),
    ("MAPGIS_LOAD_READY", "交付包"),
)
STAGE_KEYS = tuple(key for key, _ in STAGES)
STAGE_LABELS = dict(STAGES)
# Directory-backed stages, polled while a run is active (DXF_EXPORT has no
# folder of its own and MAPGIS_LOAD_READY is finalized from the report).
_DIR_STAGES = ("00_INPUT_FREEZE", "04_LINE_WORKFLOW", "05_TEXT_WORKFLOW", "08_SECTION_W60", "MAPGIS_LOAD_READY")

MAX_OVERLAY_FEATURES = 6000


class Protocol:
    """Thread-safe JSONL writer bound to the original stdout stream."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            with self._lock:
                self._stream.write(text + "\n")
                self._stream.flush()
        except OSError:
            # The protocol peer is gone (shell closed / handle invalid). A host
            # without its channel is useless and every further send would raise
            # in some worker thread — exit instead of limping on.
            os._exit(3)

    def reply(self, request_id: Any, data: dict[str, Any]) -> None:
        self.send({"id": request_id, "ok": True, "data": data})

    def fail(self, request_id: Any, error: str) -> None:
        self.send({"id": request_id, "ok": False, "error": error})

    def event(self, name: str, **data: Any) -> None:
        self.send({"event": name, "data": data})


class _StdoutToLog(io.TextIOBase):
    """Replaces sys.stdout so stray pipeline prints become ``log`` events."""

    def __init__(self, proto: Protocol) -> None:
        self._proto = proto
        self._buffer = ""
        self._lock = threading.Lock()

    def writable(self) -> bool:  # pragma: no cover - io plumbing
        return True

    def write(self, text: str) -> int:
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                if line.strip():
                    self._proto.event("log", level="info", message=line)
        return len(text)

    def flush(self) -> None:  # pragma: no cover - io plumbing
        pass


def form_state_from_args(form: dict[str, Any]) -> GuiFormState:
    """Build a GuiFormState from the shell's JSON form payload."""

    def _s(key: str, default: str = "") -> str:
        return str(form.get(key) or default).strip()

    def _opt_path(key: str) -> Path | None:
        raw = _s(key)
        return Path(raw) if raw else None

    def _opt_float(key: str) -> float | None:
        value = form.get(key)
        if value is None or value == "":
            return None
        return float(value)

    return GuiFormState(
        project_root=Path(_s("project_root") or str(default_project_root())),
        source_raster=Path(_s("source_raster")),
        map_id=_s("map_id"),
        output_parent=Path(_s("output_parent") or _s("project_root") or "."),
        text_candidates=_opt_path("text_candidates"),
        target_line_file=_s("target_line_file") or None,
        target_text_file=_s("target_text_file") or None,
        target_area_file=_s("target_area_file") or None,
        ai_provider=_s("ai_provider", "none") or "none",
        ai_base_url=_s("ai_base_url"),
        ai_api_key=str(form.get("ai_api_key") or ""),
        ai_model=_s("ai_model"),
        include_areas=bool(form.get("include_areas", False)),
        conversion_mode=_s("conversion_mode", "cli") or "cli",
        line_engine=_s("line_engine", DEFAULT_LINE_ENGINE) or DEFAULT_LINE_ENGINE,
        line_connect=_s("line_connect", DEFAULT_LINE_CONNECT) or DEFAULT_LINE_CONNECT,
        line_bridge_gap_px=_opt_float("line_bridge_gap_px"),
        line_close_gap_px=_opt_float("line_close_gap_px"),
        line_repair=_s("line_repair", DEFAULT_LINE_REPAIR) or DEFAULT_LINE_REPAIR,
        line_export_source=_s("line_export_source", DEFAULT_LINE_EXPORT_SOURCE) or DEFAULT_LINE_EXPORT_SOURCE,
        ai_enhance=bool(form.get("ai_enhance", False)),
        ocr_python=_opt_path("ocr_python"),
        export_dxf=bool(form.get("export_dxf", True)),
        qgis_files=bool(form.get("qgis_files", True)),
        reset_output=bool(form.get("reset_output", False)),
        wait_timeout_seconds=int(form.get("wait_timeout_seconds") or 300),
        level_input=_s("level_input", "off") or "off",
        enhanced_preview=_s("enhanced_preview", "standard") or "standard",
        skip_dongle_check=bool(form.get("skip_dongle_check", False)),
    )


def stage_states_from_report(report: dict[str, Any], *, cancelled: bool = False) -> dict[str, str]:
    """Map a PROGRAM_RUN_REPORT to final stage-rail states (honest, per design doc)."""
    states: dict[str, str] = {key: "pending" for key in STAGE_KEYS}

    if report.get("input") or report.get("raster_alignment") or report.get("line_candidate_generation"):
        states["00_INPUT_FREEZE"] = "completed"
    line_gen = report.get("line_candidate_generation")
    if isinstance(line_gen, dict):
        states["04_LINE_WORKFLOW"] = "completed" if line_gen.get("ok", True) else "failed"
    text_gen = report.get("text_candidate_generation")
    if isinstance(text_gen, dict):
        states["05_TEXT_WORKFLOW"] = "completed" if text_gen.get("ok", True) else "failed"

    line_report = report.get("line")
    text_report = report.get("text")

    def _has_dxf(section: Any) -> bool:
        if not isinstance(section, dict):
            return False
        dxf = section.get("dxf_export")
        return isinstance(dxf, dict) and bool(dxf.get("path"))

    line_dxf = _has_dxf(line_report)
    text_dxf = _has_dxf(text_report)
    if line_dxf and text_dxf:
        states["DXF_EXPORT"] = "completed"
    elif line_dxf or text_dxf:
        # Repo rule: line AND text DXF must always be produced — a missing
        # DXF is a bug, so half an export is a failure, not a success.
        states["DXF_EXPORT"] = "failed"
    elif isinstance(line_report, dict) or isinstance(text_report, dict):
        states["DXF_EXPORT"] = "skipped"

    conversion = report.get("conversion")
    if isinstance(conversion, dict):
        outcome = conversion_outcome(conversion)
        if outcome == "converted":
            states["08_SECTION_W60"] = "completed"
        elif outcome in {"prepared", "skipped"}:
            states["08_SECTION_W60"] = "skipped"
        else:
            states["08_SECTION_W60"] = "failed"

    load_ready = report.get("mapgis_load_ready")
    if isinstance(load_ready, dict):
        folder = str(load_ready.get("load_folder") or "")
        if folder and Path(folder).is_dir() and not (Path(folder) / "INCOMPLETE_DO_NOT_USE.txt").is_file():
            states["MAPGIS_LOAD_READY"] = "completed"
        else:
            states["MAPGIS_LOAD_READY"] = "blocked"

    if cancelled:
        for key, value in states.items():
            if value == "pending":
                states[key] = "cancelled"
    return states


class StageTracker(threading.Thread):
    """Polls the run's output folders and emits honest stage-rail events.

    Directory appearance is the only live signal the pipeline exposes without
    modification, so during the run: latest existing stage dir = running,
    earlier ones = completed. Final states always come from the run report.
    """

    def __init__(self, output_root: Path, proto: Protocol) -> None:
        super().__init__(daemon=True)
        self._output_root = output_root
        self._proto = proto
        self._done = threading.Event()
        # scan() (tracker thread) and finish() (run worker) race on _states;
        # the lock + the done-check inside it guarantee final states can never
        # be overwritten by an in-flight scan.
        self._lock = threading.Lock()
        self._states: dict[str, str] = {key: "pending" for key in STAGE_KEYS}
        self._emit_all()

    def _emit_all(self) -> None:
        for key in STAGE_KEYS:
            self._proto.event("stage", stage=key, state=self._states[key])

    def _set(self, stage: str, state: str) -> None:
        if self._states.get(stage) != state:
            self._states[stage] = state
            self._proto.event("stage", stage=stage, state=state)
            if state == "running":
                self._proto.event("status", state="running", label=STAGE_LABELS[stage])

    def run(self) -> None:  # pragma: no cover - timing loop; scan() is tested
        while not self._done.wait(0.7):
            self.scan()

    def scan(self) -> None:
        with self._lock:
            if self._done.is_set():
                return
            existing = [key for key in _DIR_STAGES if (self._output_root / key).is_dir()]
            if not existing:
                return
            for key in existing[:-1]:
                self._set(key, "completed")
            self._set(existing[-1], "running")

    def finish(self, report: dict[str, Any] | None, *, cancelled: bool = False, failed: bool = False) -> None:
        with self._lock:
            self._done.set()
            if report is not None:
                for key, state in stage_states_from_report(report, cancelled=cancelled).items():
                    self._set(key, state)
                return
            # No report — the run stopped early. Freeze the rail honestly.
            for key in STAGE_KEYS:
                current = self._states[key]
                if current == "running":
                    self._set(key, "cancelled" if cancelled else "failed")
                elif current == "pending":
                    self._set(key, "cancelled" if cancelled else "blocked")


def _read_image_bgr(path: Path):
    """cv2.imread that survives non-ASCII (Chinese) Windows paths."""
    import cv2
    import numpy as np

    data = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取图片: {path}")
    return image


def render_preview(path: Path, max_dim: int = 1800) -> dict[str, Any]:
    import cv2

    image = _read_image_bgr(path)
    height, width = image.shape[:2]
    scale = min(1.0, float(max_dim) / float(max(height, width)))
    if scale < 1.0:
        image = cv2.resize(image, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError("PNG 编码失败")
    return {
        "png_base64": base64.b64encode(encoded.tobytes()).decode("ascii"),
        "source_width": width,
        "source_height": height,
        "preview_width": image.shape[1],
        "preview_height": image.shape[0],
        "scale": scale,
    }


def _load_geojson_features(path: Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    features = payload.get("features")
    return features if isinstance(features, list) else []


def load_candidates(output_root: Path, image_height: float | None) -> dict[str, Any]:
    """Line polylines + text boxes in IMAGE pixel coords (y-down) for overlay.

    GeoJSON on disk is map-space (y = height - row). The run report's own
    raster size (raster_alignment.source_size_px) is the authoritative height;
    ``image_height`` is only a fallback for reports that predate that field.
    """
    report_path = output_root / "PROGRAM_RUN_REPORT.json"
    lines: list[list[list[float]]] = []
    texts: list[dict[str, Any]] = []
    dropped = 0
    line_path: Path | None = None
    text_path: Path | None = None
    if report_path.is_file():
        with open(report_path, "r", encoding="utf-8") as handle:
            report = json.load(handle)
        line_gen = report.get("line_candidate_generation") or {}
        text_gen = report.get("text_candidate_generation") or {}
        if line_gen.get("output_geojson"):
            line_path = Path(str(line_gen["output_geojson"]))
        if text_gen.get("output_geojson"):
            text_path = Path(str(text_gen["output_geojson"]))
        size = (report.get("raster_alignment") or {}).get("source_size_px")
        if isinstance(size, (list, tuple)) and len(size) == 2 and size[1]:
            image_height = float(size[1])

    if line_path and line_path.is_file() and image_height:
        features = _load_geojson_features(line_path)
        if len(features) > MAX_OVERLAY_FEATURES:
            dropped = len(features) - MAX_OVERLAY_FEATURES
            features = features[:MAX_OVERLAY_FEATURES]
        for feature in features:
            geometry = feature.get("geometry") or {}
            gtype = geometry.get("type")
            coords = geometry.get("coordinates")
            parts = []
            if gtype == "LineString" and isinstance(coords, list):
                parts = [coords]
            elif gtype == "MultiLineString" and isinstance(coords, list):
                parts = coords
            for part in parts:
                try:
                    lines.append([[float(x), float(image_height) - float(y)] for x, y in part])
                except (TypeError, ValueError):
                    continue

    if text_path and text_path.is_file():
        for feature in _load_geojson_features(text_path)[:MAX_OVERLAY_FEATURES]:
            props = feature.get("properties") or {}
            try:
                texts.append(
                    {
                        "left": float(props["bbox_left_px"]),
                        "top": float(props["bbox_top_px"]),
                        "right": float(props["bbox_right_px"]),
                        "bottom": float(props["bbox_bottom_px"]),
                        "text": str(props.get("text") or props.get("ocr_text") or ""),
                    }
                )
            except (KeyError, TypeError, ValueError):
                continue

    return {"lines": lines, "texts": texts, "dropped_lines": dropped}


def _count_checked_yes(report: dict[str, Any]) -> int:
    """Count candidates NOT marked checked=no — must be 0 by product rule.

    An honest number (instead of a hardcoded 0) so the UI would surface a
    violation if an external candidate file or an upstream bug ever wrote
    checked=yes.
    """
    total = 0
    for section in ("line_candidate_generation", "text_candidate_generation", "area_candidate_generation"):
        data = report.get(section)
        if not isinstance(data, dict) or not data.get("output_geojson"):
            continue
        path = Path(str(data["output_geojson"]))
        if not path.is_file():
            continue
        try:
            for feature in _load_geojson_features(path):
                checked = str((feature.get("properties") or {}).get("checked", "no")).strip().lower()
                if checked not in {"no", "", "false", "0"}:
                    total += 1
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return total


def run_summary(output_root: Path, *, light: bool = False) -> dict[str, Any]:
    """Summarize one run from its PROGRAM_RUN_REPORT.

    ``light=True`` reads only the report itself — it skips the checked=yes
    audit (which parses every candidate GeoJSON, tens of MB on big maps) and
    the load-ready dir scan. Used by the history list, which needs neither.
    """
    report_path = output_root / "PROGRAM_RUN_REPORT.json"
    if not report_path.is_file():
        return {"has_report": False}
    with open(report_path, "r", encoding="utf-8") as handle:
        report = json.load(handle)
    kind, message = completion_message_for_report(report)

    def _count(section: str, key: str) -> Any:
        data = report.get(section)
        return data.get(key) if isinstance(data, dict) else None

    load_ready = report.get("mapgis_load_ready") or {}
    load_folder = str(load_ready.get("load_folder") or "")
    ready_files: list[str] = []
    if not light and load_folder and Path(load_folder).is_dir():
        ready_files = sorted(p.name for p in Path(load_folder).iterdir() if p.is_file())

    conversion = report.get("conversion") or {}
    # The pixel-unit raster is the overlay-correct backdrop (CLAUDE.md rule:
    # overlay checks use it, never the original scan — leveling may rotate).
    pixel_unit_raster = str((report.get("raster_alignment") or {}).get("pixel_unit_raster") or "")
    if pixel_unit_raster and not Path(pixel_unit_raster).is_file():
        pixel_unit_raster = ""
    summary = {
        "has_report": True,
        "kind": kind,
        "message": message,
        "output_root": str(report.get("output_root") or output_root),
        "map_id": report.get("map_id"),
        "pixel_unit_raster": pixel_unit_raster,
        "line_candidates": _count("line_candidate_generation", "feature_count"),
        "text_candidates": _count("text_candidate_generation", "feature_count"),
        "area_candidates": _count("area_candidate_generation", "feature_count"),
        "conversion_status": conversion.get("status") if isinstance(conversion, dict) else None,
        "load_folder": load_folder,
        "ready_files": ready_files,
    }
    if not light:
        summary["stage_states"] = stage_states_from_report(report)
        summary["checked_yes"] = _count_checked_yes(report)
    return summary


def list_history(parent: Path, limit: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not parent.is_dir():
        return rows
    candidates = []
    for child in parent.iterdir():
        if child.is_dir() and child.name.upper().endswith("_P") and (child / "PROGRAM_RUN_REPORT.json").is_file():
            candidates.append(((child / "PROGRAM_RUN_REPORT.json").stat().st_mtime, child))
    for mtime, child in sorted(candidates, reverse=True)[:limit]:
        summary = run_summary(child, light=True)
        rows.append(
            {
                "output_root": str(child),
                "name": child.name,
                "mtime": mtime,
                "kind": summary.get("kind"),
                "map_id": summary.get("map_id"),
                "line_candidates": summary.get("line_candidates"),
                "text_candidates": summary.get("text_candidates"),
                "conversion_status": summary.get("conversion_status"),
            }
        )
    return rows


def preflight(conversion_mode: str = "cli", export_dxf: bool = True) -> dict[str, Any]:
    from geoscan.env_probe import DONGLE_PROCESS_NAME, dongle_process_running, program_candidates
    from geoscan.production_accuracy_workflow import resolve_ogr2ogr

    settings = read_machine_settings()
    checks: list[dict[str, Any]] = []
    needs_mapgis = conversion_mode == "cli"
    conversion_needs_dxf = conversion_mode in {"cli", "prepare"}

    def _tool_state(key: str, program: str) -> tuple[str, str]:
        configured = (settings.get(key) or "").strip()
        if configured and Path(configured).is_file():
            return "ok", configured
        for candidate in program_candidates(program):
            if candidate.is_file():
                return "ok", str(candidate)
        return "missing", ""

    if needs_mapgis:
        section_state, section_path = _tool_state("section_exe", "section")
        w60_state, w60_path = _tool_state("w60_conv_exe", "w60_conv")
        checks.append(
            {"key": "section", "label": "SECTION 程序", "state": section_state, "detail": section_path or "未找到；设置→自动探测本机程序"}
        )
        checks.append(
            {"key": "w60", "label": "W60 转换程序", "state": w60_state, "detail": w60_path or "未找到；设置→自动探测本机程序"}
        )
    else:
        section_state, w60_state = "skip", "skip"
        checks.append(
            {"key": "section", "label": "SECTION 程序", "state": "skip", "detail": "当前未请求 MapGIS WL/WT 输出"}
        )
        checks.append(
            {"key": "w60", "label": "W60 转换程序", "state": "skip", "detail": "当前未请求 MapGIS WL/WT 输出"}
        )

    ogr_state, ogr_detail = "missing", ""
    if conversion_needs_dxf and not export_dxf:
        ogr_detail = "MapGIS/SECTION 转换依赖 DXF 输出；请打开 DXF，或关闭 MapGIS 转换"
    elif export_dxf:
        try:
            from geoscan.production_accuracy_workflow import bundled_gdal_dir

            ogr = Path(resolve_ogr2ogr())
            if ogr.is_file():
                ogr_state, ogr_detail = "ok", str(ogr)
            else:
                # Say WHAT was tried, so a remote screenshot is diagnosable:
                # a stale configured path vs. a missing bundled gdal\ folder.
                tried = [f"已试 {ogr}"]
                if getattr(sys, "frozen", False) and bundled_gdal_dir() is None:
                    tried.append("安装目录缺少 gdal\\（重新安装可恢复）")
                else:
                    tried.append("到设置里选择本机 QGIS 的 ogr2ogr.exe，或清掉失效的旧路径")
                ogr_detail = "未找到；" + "；".join(tried)
        except Exception as exc:  # resolver may raise on odd setups
            ogr_detail = str(exc)
    else:
        ogr_state = "skip"
        ogr_detail = "当前未请求 DXF/SHP 输出"
    checks.append({"key": "ogr2ogr", "label": "ogr2ogr / GDAL", "state": ogr_state, "detail": ogr_detail or "未找到；DXF 导出需要"})

    if needs_mapgis:
        dongle_ok = dongle_process_running()
        checks.append(
            {
                "key": "dongle",
                "label": f"MapGIS 密码狗 ({DONGLE_PROCESS_NAME})",
                "state": "ok" if dongle_ok else "warn",
                "detail": "运行中" if dongle_ok else "未检测到——cli 转换会失败",
            }
        )
    else:
        checks.append(
            {
                "key": "dongle",
                "label": f"MapGIS 密码狗 ({DONGLE_PROCESS_NAME})",
                "state": "skip",
                "detail": "当前未请求 MapGIS WL/WT 输出",
            }
        )

    import importlib.util

    ocr_available = importlib.util.find_spec("rapidocr") is not None or bool((settings.get("ocr_python") or "").strip())
    checks.append(
        {
            "key": "ocr",
            "label": "OCR (rapidocr)",
            "state": "ok" if ocr_available else "warn",
            "detail": "可用" if ocr_available else "不可用；文字候选将退化为占位框（属正常）",
        }
    )

    blocked = needs_mapgis and (section_state != "ok" or w60_state != "ok")
    if not blocked and (export_dxf or conversion_needs_dxf) and ogr_state == "missing":
        blocked = True
    warned = any(check["state"] == "warn" for check in checks)
    overall = "blocked" if blocked else ("needs_attention" if warned else "ready")
    return {"overall": overall, "checks": checks}


class EngineHost:
    def __init__(self, proto: Protocol) -> None:
        self.proto = proto
        self._stop_single = threading.Event()
        self._stop_batch = threading.Event()
        self._busy = threading.Lock()

    # ------------------------------------------------------------------
    # Command handlers (each returns response data or raises ValueError)
    # ------------------------------------------------------------------
    def cmd_ping(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"app": "geoscan", "version": __version__, "pid": os.getpid()}

    def cmd_get_settings(self, args: dict[str, Any]) -> dict[str, Any]:
        settings = read_machine_settings()
        # Never ship the plaintext key across the JSONL pipe — the console UI
        # only needs to know whether one is stored.
        return {
            "settings": settings,
            "settings_file": str(settings_save_path()),
            "has_saved_key": bool(load_encrypted_api_key()),
            "project_root": str(default_project_root(settings)),
            "defaults": {
                "line_engine": DEFAULT_LINE_ENGINE,
                "line_connect": DEFAULT_LINE_CONNECT,
                "line_repair": DEFAULT_LINE_REPAIR,
                "line_export_source": DEFAULT_LINE_EXPORT_SOURCE,
            },
        }

    def cmd_save_settings(self, args: dict[str, Any]) -> dict[str, Any]:
        # MERGE into the stored settings: a caller that only edits one page
        # (e.g. the AI panel) must not wipe the tool paths saved by another.
        # Validate only the keys THIS call provides — a stale stored path must
        # not block saving an unrelated page.
        provided = {str(k): str(v) for k, v in (args.get("settings") or {}).items()}
        settings = dict(read_machine_settings())
        settings.update(provided)
        missing = invalid_settings_paths(provided)
        if missing:
            details = "；".join(f"{label}: {value}" for label, value in missing)
            raise ValueError(f"以下路径在本机不存在，请重新选择：{details}")
        target = save_settings(settings)
        apply_settings_to_env(settings, override=True)
        # Only touch the stored DPAPI key when the request EXPLICITLY carries
        # save_key. A tool-paths-only save (no AI fields in the dialog) must
        # never silently delete a key the user stored elsewhere.
        key_saved = False
        if "save_key" in args:
            if args.get("save_key") and str(args.get("ai_api_key") or "").strip():
                save_encrypted_api_key(str(args["ai_api_key"]))
                key_saved = True
            elif not args.get("save_key"):
                save_encrypted_api_key("")
        return {"settings_file": str(target), "key_saved": key_saved}

    def cmd_autodetect_tools(self, args: dict[str, Any]) -> dict[str, Any]:
        current = {str(k): str(v) for k, v in (args.get("settings") or read_machine_settings()).items()}
        return {"filled": autodetect_tool_paths(current)}

    def cmd_preflight(self, args: dict[str, Any]) -> dict[str, Any]:
        return preflight(
            conversion_mode=str(args.get("conversion_mode") or "cli"),
            export_dxf=bool(args.get("export_dxf", True)),
        )

    def cmd_derive_map_id(self, args: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(args.get("path") or ""))
        map_id = default_map_id_from_image(path)
        parent = str(args.get("output_parent") or "").strip() or str(path.parent)
        output_root = str(default_output_root_from_parent(Path(parent), map_id)) if map_id else ""
        return {"map_id": map_id, "output_parent": parent, "output_root": output_root}

    def cmd_output_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        map_id = str(args.get("map_id") or "").strip()
        parent = str(args.get("output_parent") or "").strip()
        if not (map_id and parent):
            return {"output_root": ""}
        return {"output_root": str(default_output_root_from_parent(Path(parent), map_id))}

    def cmd_render_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        return render_preview(Path(str(args["path"])), max_dim=int(args.get("max_dim") or 1800))

    def cmd_load_candidates(self, args: dict[str, Any]) -> dict[str, Any]:
        height = args.get("image_height")
        return load_candidates(
            Path(str(args["output_root"])),
            float(height) if height else None,
        )

    def cmd_run_summary(self, args: dict[str, Any]) -> dict[str, Any]:
        return run_summary(Path(str(args["output_root"])))

    def cmd_list_history(self, args: dict[str, Any]) -> dict[str, Any]:
        return {"rows": list_history(Path(str(args.get("parent") or "")))}

    def cmd_open_path(self, args: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(args["path"]))
        if not path.exists():
            raise ValueError(f"路径不存在: {path}")
        os.startfile(str(path))  # type: ignore[attr-defined]
        return {"opened": str(path)}

    def cmd_dongle_status(self, args: dict[str, Any]) -> dict[str, Any]:
        from geoscan.env_probe import DONGLE_PROCESS_NAME, dongle_process_running

        return {"running": dongle_process_running(), "process": DONGLE_PROCESS_NAME}

    def cmd_check_update(self, args: dict[str, Any]) -> dict[str, Any]:
        from geoscan import updater

        info = updater.check_for_update()
        return {
            "current": info.current,
            "latest": info.latest,
            "update_available": info.update_available,
            "kind": info.kind,
            "download_size": info.download_size,
            "notes": info.notes,
            "html_url": info.html_url,
        }

    def _update_progress(self, done: int, total: int) -> None:
        self.proto.event("update_progress", done=done, total=total)

    def cmd_apply_engine_update(self, args: dict[str, Any]) -> dict[str, Any]:
        """Lightweight update: swap the loose engine in place.

        The engine PROCESS keeps running old code after this returns — the
        shell must restart the engine (engine_restart) to load the new code.
        The console window itself never restarts.
        """
        from geoscan import updater

        if not self._busy.acquire(blocking=False):
            raise ValueError("有任务正在运行；请等它完成后再更新。")
        try:
            info = updater.check_for_update()
            if not info.update_available or info.kind != "engine":
                raise ValueError("当前没有可用的轻量引擎更新（可能需要完整安装包）。")
            staging = updater.download_engine(info, progress=self._update_progress)
            updater.apply_engine_update(staging)
            return {"applied": info.latest, "restart_engine": True}
        except updater.UpdateError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            self._busy.release()

    def cmd_download_installer_update(self, args: dict[str, Any]) -> dict[str, Any]:
        """Full-installer update: download, verify, launch detached.

        The installer needs the app's files unlocked, so the SHELL must close
        its window (killing this engine) right after this returns.
        """
        from geoscan import updater

        if not self._busy.acquire(blocking=False):
            raise ValueError("有任务正在运行；请等它完成后再更新。")
        try:
            info = updater.check_for_update()
            if not info.update_available or not info.installer_url:
                raise ValueError("当前没有可下载的安装包更新。")
            installer = updater.download_installer(info, progress=self._update_progress)
            updater._spawn_detached([str(installer)])
            return {"launched": True, "installer": str(installer), "latest": info.latest}
        except updater.UpdateError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            self._busy.release()

    def cmd_test_ai_connection(self, args: dict[str, Any]) -> dict[str, Any]:
        """Probe the configured AI endpoint. Never echoes the key anywhere."""
        from geoscan.ai_vision_review import (
            AiVisionConfig,
            normalize_chat_completions_url,
            test_ai_connection,
        )

        provider = str(args.get("ai_provider") or "none").strip()
        base_url = str(args.get("ai_base_url") or "").strip()
        model = str(args.get("ai_model") or "").strip()
        api_key = str(args.get("ai_api_key") or "").strip() or load_encrypted_api_key()
        if provider == "none":
            raise ValueError("请先选择 Provider。")
        if not base_url:
            raise ValueError("请填写 AI Base URL。")
        if not api_key:
            raise ValueError("请填写 API Key（或先加密保存一个）。")
        if not model:
            raise ValueError("请填写 AI Model。")
        api_url = normalize_chat_completions_url(base_url)
        config = AiVisionConfig(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=30,
        )
        report = test_ai_connection(config)
        return {"api_url": str(report.get("api_url") or api_url)}

    def cmd_analyze_image(self, args: dict[str, Any]) -> dict[str, Any]:
        """AI 看图描述（诊断用，写入 AI_VISUAL_REVIEW，不影响结果）。"""
        from geoscan.ai_vision_review import analyze_map_image_with_ai

        form = dict(args.get("form") or {})
        state = self._inject_saved_ai_key(form_state_from_args(form))
        if state.ai_provider == "none":
            raise ValueError("请先在 AI 页选择 Provider。")
        if not (state.ai_base_url and state.ai_api_key and state.ai_model):
            raise ValueError("AI 页的 Base URL / API Key / Model 都必须填写。")
        error = validate_form_state(state)
        if error:
            raise ValueError(error)
        if not self._busy.acquire(blocking=False):
            raise ValueError("已有任务在运行；请先停止或等待完成。")
        try:
            output_root = default_output_root_from_parent(state.output_parent, state.map_id)
            config = build_ai_config_from_gui(state)
            report = analyze_map_image_with_ai(
                config,
                image_path=state.source_raster,
                output_root=output_root,
                map_id=state.map_id,
            )
            return {"analysis_path": str(report.get("analysis_path") or "")}
        finally:
            self._busy.release()

    def cmd_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        self._stop_single.set()
        self._stop_batch.set()
        self.proto.event(
            "log",
            level="warn",
            message="已请求安全停止：单图在当前阶段结束后停止；批量在当前图完成后停止（SECTION 转换不会被中途打断）。",
        )
        return {"stopping": True}

    @staticmethod
    def _inject_saved_ai_key(state: GuiFormState) -> GuiFormState:
        """The console never holds the API key — when the form carries none,
        use the DPAPI-stored one so AI features work off the saved key."""
        if state.ai_api_key.strip() or state.ai_provider == "none":
            return state
        saved = load_encrypted_api_key()
        return replace(state, ai_api_key=saved) if saved else state

    def cmd_run_single(self, args: dict[str, Any]) -> dict[str, Any]:
        form = dict(args.get("form") or {})
        state = self._inject_saved_ai_key(form_state_from_args(form))
        error = validate_form_state(state)
        if error:
            raise ValueError(error)
        if not self._busy.acquire(blocking=False):
            raise ValueError("已有任务在运行；请先停止或等待完成。")
        try:
            config = build_program_config_from_gui(state)
            output_root = Path(str(config.output_root))
            self._stop_single.clear()
            self._stop_batch.clear()
        except BaseException:
            # The worker owns the release once started; anything failing before
            # that must give the lock back or every later run is rejected.
            self._busy.release()
            raise

        def worker() -> None:
            tracker = StageTracker(output_root, self.proto)
            try:
                apply_settings_to_env(read_machine_settings(), override=True)
                self.proto.event("status", state="running", label="开始运行")
                self.proto.event("log", level="info", message=f"开始运行；输出目录: {output_root}")
                self.proto.event("log", level="info", message=run_notice_for_state(state))
                if config.ai_provider != "none":
                    self.proto.event(
                        "log",
                        level="info",
                        message=f"AI: {config.ai_provider}, model={config.ai_model}, key={redact_api_key(config.ai_api_key)}",
                    )
                tracker.start()
                report = run_production_program(config, should_stop=self._stop_single.is_set)
                tracker.finish(report)
                kind, message = completion_message_for_report(report)
                load_folder = str((report.get("mapgis_load_ready") or {}).get("load_folder") or "")
                self.proto.event("status", state="idle", label="就绪")
                self.proto.event(
                    "result",
                    kind=kind,
                    scope="single",
                    message=message,
                    output_root=str(report.get("output_root") or output_root),
                    load_folder=load_folder,
                )
            except RunCancelledError:
                tracker.finish(None, cancelled=True)
                self.proto.event("status", state="idle", label="已停止")
                self.proto.event(
                    "result",
                    kind="cancelled",
                    scope="single",
                    message="已按请求安全停止。本图输出不完整，不能用于 MapGIS 编辑；需要时勾选“覆盖已有输出”重跑这张图。",
                    output_root=str(output_root),
                )
            except Exception as exc:
                tracker.finish(None, failed=True)
                self.proto.event("status", state="idle", label="运行失败")
                self.proto.event(
                    "result",
                    kind="error",
                    scope="single",
                    message=friendly_error_message(exc),
                    output_root=str(output_root),
                )
            finally:
                self._busy.release()

        try:
            threading.Thread(target=worker, daemon=True).start()
        except BaseException:
            self._busy.release()
            raise
        return {"accepted": True, "output_root": str(output_root)}

    def cmd_run_batch(self, args: dict[str, Any]) -> dict[str, Any]:
        from geoscan.batch_runner import run_batch

        form = dict(args.get("form") or {})
        state = self._inject_saved_ai_key(form_state_from_args(form))
        if state.line_export_source == "repaired" and state.line_repair == "off":
            raise ValueError("导出线层选 repaired 时，必须同时把线修复设为 conservative。")
        source_dir = Path(str(args.get("source_dir") or ""))
        if not source_dir.is_dir():
            raise ValueError("请选择存在的图源文件夹。")
        limit_raw = args.get("limit")
        limit = max(1, int(limit_raw)) if limit_raw else None
        rasters = discover_source_rasters(source_dir)
        if not rasters:
            raise ValueError("图源文件夹里没有 .tif/.tiff 文件。")
        if not self._busy.acquire(blocking=False):
            raise ValueError("已有任务在运行；请先停止或等待完成。")
        try:
            config = build_batch_config_from_gui(
                state,
                source_rasters=tuple(rasters),
                retry_incomplete=bool(args.get("retry_incomplete", False)),
                limit=limit,
            )
            # Consoles up to 0.2.2 send this at args level for batch runs (the
            # console exe only updates via the full installer, so an engine-zip
            # update can pair a new engine with an old shell).
            if args.get("skip_dongle_check"):
                config = replace(config, skip_dongle_check=True)
            self._stop_single.clear()
            self._stop_batch.clear()
        except BaseException:
            self._busy.release()
            raise

        def worker() -> None:
            try:
                apply_settings_to_env(read_machine_settings(), override=True)
                self.proto.event("status", state="running", label=f"批量运行（{len(rasters)} 张）")
                self.proto.event(
                    "log",
                    level="info",
                    message=f"开始批量：{len(rasters)} 张图，引擎={state.line_engine}，连接={state.line_connect}，转换={state.conversion_mode}",
                )
                report = run_batch(
                    config,
                    progress=lambda row: self.proto.event("batch_row", **dict(row)),
                    should_stop=self._stop_batch.is_set,
                )
                counts = report["counts"]
                summary = (
                    f"批量结束：完成 {counts['completed']}，失败 {counts['failed']}，"
                    f"跳过已完成 {counts['skipped_completed']}，"
                    f"待处理不完整 {counts['incomplete_needs_attention']}，"
                    f"未开始 {counts['not_started']}。"
                )
                kind = "ok" if counts["failed"] == 0 else "warning"
                self.proto.event("status", state="idle", label="就绪")
                self.proto.event(
                    "result",
                    kind=kind,
                    scope="batch",
                    message=summary,
                    batch_ops=str(Path(config.project_root) / "BATCH_OPS"),
                )
            except Exception as exc:
                self.proto.event("status", state="idle", label="批量失败")
                self.proto.event("result", kind="error", scope="batch", message=friendly_error_message(exc))
            finally:
                self._busy.release()

        try:
            threading.Thread(target=worker, daemon=True).start()
        except BaseException:
            self._busy.release()
            raise
        return {"accepted": True, "count": len(rasters)}

    # ------------------------------------------------------------------
    def handle(self, request: dict[str, Any]) -> None:
        request_id = request.get("id")
        cmd = str(request.get("cmd") or "")
        args = request.get("args") or {}
        handler: Callable[[dict[str, Any]], dict[str, Any]] | None = getattr(self, f"cmd_{cmd}", None)
        if handler is None or not cmd:
            self.proto.fail(request_id, f"未知命令: {cmd}")
            return
        try:
            self.proto.reply(request_id, handler(args))
        except ValueError as exc:
            self.proto.fail(request_id, str(exc))
        except Exception as exc:  # pragma: no cover - defensive runtime path
            self.proto.fail(request_id, friendly_error_message(exc))


def _suppress_child_console_windows() -> None:
    """Stop console children (ogr2ogr/tasklist/W60) from flashing black windows.

    The Tauri shell starts this host with CREATE_NO_WINDOW, so the host has no
    console and every console-subsystem child would otherwise open its own
    visible window. Defaulting CREATE_NO_WINDOW for subprocesses spawned from
    THIS process fixes that in one place; GUI apps (section.exe) are unaffected
    — the flag only suppresses console creation.
    """
    if os.name != "nt":
        return
    import subprocess

    original_init = subprocess.Popen.__init__

    def patched_init(self, *args: Any, **kwargs: Any) -> None:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        original_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched_init  # type: ignore[method-assign]


def main() -> int:
    _suppress_child_console_windows()
    # The protocol must be clean UTF-8 JSONL regardless of the console codepage.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass

    proto = Protocol(sys.stdout)
    sys.stdout = _StdoutToLog(proto)  # stray pipeline prints -> log events

    from geoscan.app_settings import bootstrap_settings

    settings_report = bootstrap_settings()
    host = EngineHost(proto)
    proto.event(
        "hello",
        version=__version__,
        settings_file=str(settings_report.get("settings_file") or ""),
    )

    workers: list[threading.Thread] = []
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            proto.event("protocol_error", message=f"无法解析请求: {exc}")
            continue
        worker = threading.Thread(target=host.handle, args=(request,), daemon=True)
        worker.start()
        workers.append(worker)
        workers = [w for w in workers if w.is_alive()]
    # stdin EOF: give in-flight request threads a moment to write their replies
    # before the process exits (a piped one-shot client sends EOF immediately
    # after its request; without this join the reply would be lost).
    for worker in workers:
        worker.join(timeout=15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
