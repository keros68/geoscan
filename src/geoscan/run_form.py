"""UI-agnostic application layer for the desktop shell.

Form state, config builders, validation, tool probing, and report-to-message
formatting used by the JSONL engine host (``engine_host``) behind the Tauri
console. Everything here must stay free of UI-toolkit imports — the headless
engine host loads this module on every console launch.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geoscan.ai_vision_review import AiVisionConfig
from geoscan.app_settings import read_machine_settings
from geoscan.batch_runner import BatchConfig
from geoscan.line_connectivity import VALID_LINE_CONNECT_MODES
from geoscan.production_accuracy_workflow import short_output_root_for_map_id
from geoscan.production_program import (
    ProgramConfig,
    VALID_CONVERSION_MODES,
    VALID_ENHANCED_PREVIEW_MODES,
    VALID_LEVEL_INPUT_MODES,
    VALID_LINE_ENGINES,
    conversion_outcome,
    default_area_target_file,
    default_line_target_file,
    default_text_target_file,
    derive_map_id_from_filename,
    sanitize_map_id,
)


def _writable_default_root() -> Path:
    """A user-writable base for outputs, decoupled from where the app is installed.

    A frozen app's working directory is its install dir (e.g. Program Files),
    which is read-only without admin — defaulting outputs there fails with a
    PermissionError. Prefer the user's Documents, then their home folder. In dev
    (not frozen) the repo cwd is fine.
    """
    if getattr(sys, "frozen", False):
        home = Path.home()
        for candidate in (home / "Documents" / "GeoScan", home / "GeoScan"):
            if candidate.parent.is_dir():
                return candidate
        return home / "GeoScan"
    return Path.cwd()


DEFAULT_PROJECT_ROOT = _writable_default_root()
IMAGE_EXTENSIONS = (".tif", ".tiff", ".jpg", ".jpeg", ".png", ".bmp")

DEFAULT_AI_PROVIDER = "none"
DEFAULT_AI_BASE_URL = "https://api.siliconflow.cn/v1/chat/completions"
DEFAULT_AI_MODEL = "Qwen/Qwen3-VL-32B-Instruct"
DEFAULT_LINE_ENGINE = "trace"
# 默认"标准"连接档：补断线更省人工，且每条桥都要求图上有墨迹证据。
DEFAULT_LINE_CONNECT = "standard"
DEFAULT_LINE_REPAIR = "conservative"
DEFAULT_LINE_EXPORT_SOURCE = "repaired"

# One save-validation table for the settings page: (label, key, is_dir).
SETTINGS_PATH_RULES: tuple[tuple[str, str, bool], ...] = (
    ("SECTION 程序", "section_exe", False),
    ("W60 转换程序", "w60_conv_exe", False),
    ("ogr2ogr", "ogr2ogr", False),
    ("GDAL 数据目录", "gdal_data", True),
    ("OCR 解释器", "ocr_python", False),
    ("项目根目录", "project_root", True),
)


def default_project_root(machine_settings: dict[str, str] | None = None) -> Path:
    """Working directory for this machine: saved setting > writable user default.

    Never the install dir — an app under Program Files is read-only, so outputs
    must default somewhere the user can write. The user can always pick their own
    folder, and choosing an input image points this at the image's folder.
    """
    settings = machine_settings if machine_settings is not None else {}
    saved = settings.get("project_root", "").strip()
    if saved and Path(saved).is_dir():
        return Path(saved)
    if DEFAULT_PROJECT_ROOT.is_dir():
        return DEFAULT_PROJECT_ROOT
    return _writable_default_root()


@dataclass(frozen=True)
class GuiFormState:
    project_root: Path
    source_raster: Path
    map_id: str
    output_parent: Path
    text_candidates: Path | None = None
    target_line_file: str | None = None
    target_text_file: str | None = None
    target_area_file: str | None = None
    ai_provider: str = "none"
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    include_areas: bool = False
    conversion_mode: str = "cli"
    line_engine: str = DEFAULT_LINE_ENGINE
    line_connect: str = DEFAULT_LINE_CONNECT
    line_bridge_gap_px: float | None = None
    line_close_gap_px: float | None = None
    line_repair: str = DEFAULT_LINE_REPAIR
    line_export_source: str = DEFAULT_LINE_EXPORT_SOURCE
    ai_enhance: bool = False
    ocr_python: Path | None = None
    export_dxf: bool = True
    qgis_files: bool = True
    reset_output: bool = False
    wait_timeout_seconds: int = 300
    level_input: str = "off"
    enhanced_preview: str = "standard"
    skip_dongle_check: bool = False


def default_map_id_from_image(path: Path) -> str:
    """Auto-fill the Map ID field from any input filename.

    Falls back to a sanitized stem when the name is not in the t01_0007
    convention (e.g. a pure-number name), so the field never lands empty.
    """
    return derive_map_id_from_filename(path)


def default_output_root_from_parent(output_parent: Path, map_id: str) -> Path:
    try:
        return short_output_root_for_map_id(Path(output_parent), map_id)
    except ValueError:
        # Live preview may run with an empty Map ID field.
        return Path(output_parent) / "_P"


def build_program_config_from_gui(state: GuiFormState) -> ProgramConfig:
    output_root = default_output_root_from_parent(state.output_parent, state.map_id)
    text_candidates = state.text_candidates if state.text_candidates and str(state.text_candidates).strip() else None
    target_line_file = state.target_line_file or default_line_target_file(state.map_id)
    target_text_file = state.target_text_file or default_text_target_file(state.map_id)
    target_area_file = state.target_area_file or default_area_target_file(state.map_id)
    return ProgramConfig(
        project_root=state.project_root,
        source_raster=state.source_raster,
        map_id=state.map_id,
        output_root=output_root,
        text_candidates=text_candidates,
        target_line_file=target_line_file,
        target_text_file=target_text_file,
        target_area_file=target_area_file,
        ai_provider=state.ai_provider,
        ai_base_url=state.ai_base_url,
        ai_api_key=state.ai_api_key,
        ai_model=state.ai_model,
        include_areas=state.include_areas,
        conversion_mode=state.conversion_mode,
        line_engine=state.line_engine,
        line_connect=state.line_connect,
        line_bridge_gap_px=state.line_bridge_gap_px,
        line_close_gap_px=state.line_close_gap_px,
        line_repair=state.line_repair,
        line_export_source=state.line_export_source,
        ai_enhance=state.ai_enhance,
        ocr_python=state.ocr_python,
        export_dxf=state.export_dxf,
        qgis_files=state.qgis_files,
        reset_output=state.reset_output,
        wait_timeout_seconds=state.wait_timeout_seconds,
        level_input=state.level_input,
        enhanced_preview=state.enhanced_preview,
        skip_dongle_check=state.skip_dongle_check,
    )


def build_batch_config_from_gui(
    state: GuiFormState,
    *,
    source_rasters: tuple[Path, ...],
    retry_incomplete: bool = False,
    limit: int | None = None,
) -> BatchConfig:
    return BatchConfig(
        project_root=state.project_root,
        source_rasters=source_rasters,
        conversion_mode=state.conversion_mode,
        line_engine=state.line_engine,
        line_connect=state.line_connect,
        line_bridge_gap_px=state.line_bridge_gap_px,
        line_close_gap_px=state.line_close_gap_px,
        line_repair=state.line_repair,
        line_export_source=state.line_export_source,
        ai_enhance=state.ai_enhance,
        ai_provider=state.ai_provider,
        ai_base_url=state.ai_base_url,
        ai_api_key=state.ai_api_key,
        ai_model=state.ai_model,
        include_areas=state.include_areas,
        export_dxf=state.export_dxf,
        qgis_files=state.qgis_files,
        ocr_python=state.ocr_python,
        retry_incomplete=retry_incomplete,
        limit=limit,
        wait_timeout_seconds=state.wait_timeout_seconds,
        level_input=state.level_input,
        enhanced_preview=state.enhanced_preview,
        skip_dongle_check=state.skip_dongle_check,
    )


def build_ai_config_from_gui(
    state: GuiFormState,
    *,
    timeout_seconds: int | None = None,
) -> AiVisionConfig:
    return AiVisionConfig(
        provider=state.ai_provider,
        base_url=state.ai_base_url,
        api_key=state.ai_api_key,
        model=state.ai_model,
        timeout_seconds=timeout_seconds if timeout_seconds is not None else state.wait_timeout_seconds,
    )


def missing_conversion_tools(settings: dict[str, str] | None = None) -> list[str]:
    """Settings-first probe for SECTION/W60 — one rule for every caller."""
    from geoscan.env_probe import program_candidates

    settings = settings if settings is not None else read_machine_settings()
    missing: list[str] = []
    for key, program, label in (
        ("section_exe", "section", "SECTION 程序 (section.exe)"),
        ("w60_conv_exe", "w60_conv", "W60 转换程序 (W60_Conv.exe)"),
    ):
        configured = (settings.get(key) or os.environ.get(f"MAPGIS67_{program.upper()}_EXE", "")).strip()
        if configured and Path(configured).is_file():
            continue
        if any(candidate.is_file() for candidate in program_candidates(program)):
            continue
        missing.append(label)
    return missing


def validate_form_state(state: GuiFormState, *, settings: dict[str, str] | None = None) -> str | None:
    """User-facing validation run by the engine host before a run starts.

    ``settings`` feeds the cli tool probe; None means the stored machine
    settings (a caller may pass unsaved settings-page values instead).
    """
    if not str(state.source_raster).strip() or not state.source_raster.is_file():
        return "请选择存在的输入图片。"
    if state.source_raster.suffix.lower() not in IMAGE_EXTENSIONS:
        return "输入图片格式不在当前支持列表内。"
    if not state.map_id:
        return "请填写 Map ID，例如 T01_0006。"
    if not sanitize_map_id(state.map_id):
        return "Map ID 至少要包含一个字母或数字，例如 T01_0006 或 12345。"
    if not str(state.output_parent).strip():
        return "请选择输出父文件夹。"
    if state.text_candidates is not None and not state.text_candidates.is_file():
        return "文字候选 GeoJSON 不存在。"
    if state.conversion_mode not in VALID_CONVERSION_MODES:
        return "转换模式只能是 none、prepare 或 cli。"
    if state.conversion_mode in {"cli", "prepare"} and not state.export_dxf:
        return (
            "MapGIS 转换依赖 DXF 交换文件：请先打开“导出 DXF”，"
            "或关闭 MapGIS 转换（转换模式 none）。"
        )
    if state.conversion_mode == "none" and not state.export_dxf and not state.qgis_files:
        return (
            "没有选择任何输出文件类别：请至少打开 MapGIS 转换、导出 DXF、"
            "QGIS 文件中的一项。"
        )
    if state.line_engine not in VALID_LINE_ENGINES:
        return "线提取引擎只能是 hough 或 trace。"
    if state.line_connect not in VALID_LINE_CONNECT_MODES:
        return "线条连接程度只能是 conservative、standard 或 aggressive。"
    for label, value in (
        ("桥接最大断口", state.line_bridge_gap_px),
        ("闭合收口最大缺口", state.line_close_gap_px),
    ):
        if value is not None and value < 0:
            return f"{label}不能为负数；填 0 表示关闭该功能。"
    if state.level_input not in VALID_LEVEL_INPUT_MODES:
        return "输入调平只能是 auto、force 或 off。"
    if state.enhanced_preview not in VALID_ENHANCED_PREVIEW_MODES:
        return "增强底图只能是 none、light、standard 或 strong。"
    if state.line_export_source == "repaired" and state.line_repair == "off":
        return "导出线层选 repaired 时，必须同时把线修复设为 conservative（新鲜运行规则）。"
    if state.ai_enhance and state.ai_provider == "none":
        return "启用 AI 增强时，必须先在 AI 接入页选择 Provider 并填好 Key。"
    if state.ai_enhance and not (state.ai_base_url and state.ai_api_key and state.ai_model):
        return "启用 AI 增强时，AI 接入页的 Base URL / API Key / Model 都必须填写。"
    if state.ai_enhance and state.line_repair == "off":
        return "AI 增强在修复层上运行，必须同时把线修复设为 conservative。"
    if state.line_export_source == "ai_enhanced" and not state.ai_enhance:
        return "导出线层选 ai_enhanced 时，必须同时启用 AI 增强（新鲜运行规则）。"
    if state.ocr_python is not None and not state.ocr_python.is_file():
        return "OCR 解释器路径不存在；留空可自动探测。"
    if state.conversion_mode == "cli":
        missing = missing_conversion_tools(settings)
        if missing:
            return (
                f"转换模式 cli 需要本机的 {'、'.join(missing)}，但没有找到。\n"
                "请到“设置”页点“自动探测本机程序”，或手动选择 MapGIS67 安装目录下的"
                " program\\section.exe 与 W60_Conv.exe 并保存；"
                "也可以先把转换模式改为 none/prepare 只生成候选包。"
            )
    return None


def invalid_settings_paths(settings: dict[str, str]) -> list[tuple[str, str]]:
    """Non-empty settings paths that don't exist, as (label, value) pairs.

    Only keys present in ``settings`` are checked, so a page that saves a
    partial settings dict never trips over another page's stale path.
    """
    return [
        (label, value)
        for label, key, must_be_dir in SETTINGS_PATH_RULES
        if key in settings
        and (value := settings[key].strip())
        and not (Path(value).is_dir() if must_be_dir else Path(value).is_file())
    ]


def autodetect_tool_paths(current: dict[str, str]) -> dict[str, str]:
    """Probe the machine for tool paths not yet configured in ``current``.

    Returns only the newly found values — already-filled entries are never
    overwritten. Backs the console's "自动探测本机程序" action.
    """
    from geoscan.env_probe import program_candidates
    from geoscan.production_accuracy_workflow import resolve_gdal_data, resolve_ogr2ogr

    filled: dict[str, str] = {}
    for key, program in (("section_exe", "section"), ("w60_conv_exe", "w60_conv")):
        if current.get(key, "").strip():
            continue
        for candidate in program_candidates(program):
            if candidate.is_file():
                filled[key] = str(candidate)
                break
    if not current.get("ogr2ogr", "").strip():
        try:
            candidate = Path(resolve_ogr2ogr())
            if candidate.is_file():
                filled["ogr2ogr"] = str(candidate)
        except Exception:
            pass
    if not current.get("gdal_data", "").strip():
        try:
            candidate = Path(resolve_gdal_data())
            if candidate.is_dir():
                filled["gdal_data"] = str(candidate)
        except Exception:
            pass
    return filled


def _conversion_mode_notice(mode: str) -> str:
    if mode == "cli":
        return "转换模式 cli：会尝试通过 MCP/CLI 桥接生成 WT/WL；失败会在日志和弹窗中明确说明。"
    if mode == "prepare":
        return "转换模式 prepare：只生成 DXF 和 SECTION 批次；不会生成 WT/WL。"
    if mode == "none":
        return "转换模式 none：只生成候选/占位包；不会生成 WT/WL。"
    return f"转换模式 {mode}：未知模式，运行前会被参数校验拦截。"


def run_notice_for_state(state: GuiFormState) -> str:
    if state.text_candidates is None:
        if state.ai_provider != "none":
            return (
                "未选择文字候选 GeoJSON：本次会自动从输入图生成 05_TEXT_WORKFLOW 文字候选，"
                "继续生成文字占位包，并额外写入 AI_VISUAL_REVIEW。"
                + _conversion_mode_notice(state.conversion_mode)
            )
        return (
            "未选择文字候选 GeoJSON：本次会自动从输入图生成 05_TEXT_WORKFLOW 文字候选，"
            "并继续生成文字占位包。"
            + _conversion_mode_notice(state.conversion_mode)
        )
    return (
        "已选择文字候选 GeoJSON：将使用该文件作为覆盖输入生成文字占位包，并按转换模式继续处理。"
        + _conversion_mode_notice(state.conversion_mode)
    )


def _conversion_error_from_report(conversion: dict[str, Any]) -> str:
    containers: list[dict[str, Any]] = [conversion]
    pipeline = conversion.get("pipeline")
    if isinstance(pipeline, dict):
        containers.append(pipeline)
        for key in ("prepare", "diagnose", "convert", "verify", "collect"):
            nested = pipeline.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
    prepare = conversion.get("prepare")
    if isinstance(prepare, dict):
        containers.append(prepare)

    for key in ("error", "message", "note"):
        for container in containers:
            value = container.get(key)
            if value:
                return str(value)
    return ""


def completion_message_for_report(report: dict[str, Any]) -> tuple[str, str]:
    lines: list[str] = []
    output_root = report.get("output_root")
    if output_root:
        lines.append(f"输出目录: {output_root}")

    line_candidates = report.get("line_candidate_generation")
    if isinstance(line_candidates, dict):
        count = line_candidates.get("feature_count")
        path = line_candidates.get("output_geojson")
        if count is not None:
            lines.append(f"线候选: {count} 个")
        if path:
            lines.append(f"线候选 GeoJSON: {path}")

    line_report = report.get("line")
    if isinstance(line_report, dict):
        output_line_count = line_report.get("output_line_count")
        if output_line_count is not None:
            lines.append(f"线条候选: {output_line_count} 条")
        dxf_export = line_report.get("dxf_export")
        if isinstance(dxf_export, dict) and dxf_export.get("path"):
            lines.append(f"线 DXF: {dxf_export['path']}")

    text_candidates = report.get("text_candidate_generation")
    if isinstance(text_candidates, dict):
        count = text_candidates.get("feature_count")
        path = text_candidates.get("output_geojson")
        if count is not None:
            lines.append(f"文字候选: {count} 个")
        if path:
            lines.append(f"文字候选 GeoJSON: {path}")

    text_report = report.get("text")
    if isinstance(text_report, dict):
        output_text_count = text_report.get("output_text_count")
        if output_text_count is not None:
            lines.append(f"文字占位: {output_text_count} 个")
        dxf_export = text_report.get("dxf_export")
        if isinstance(dxf_export, dict) and dxf_export.get("path"):
            lines.append(f"文字 DXF: {dxf_export['path']}")

    area_candidates = report.get("area_candidate_generation")
    if isinstance(area_candidates, dict):
        count = area_candidates.get("feature_count")
        path = area_candidates.get("output_geojson")
        if count is not None:
            lines.append(f"区候选: {count} 个")
        if path:
            lines.append(f"区候选 GeoJSON: {path}")

    area_report = report.get("area")
    if isinstance(area_report, dict):
        output_area_count = area_report.get("output_area_count")
        if output_area_count is not None:
            lines.append(f"区交换候选: {output_area_count} 个")
        shp_export = area_report.get("shp_export")
        if isinstance(shp_export, dict) and shp_export.get("path"):
            lines.append(f"区 Shapefile: {shp_export['path']}")

    conversion = report.get("conversion")
    if not isinstance(conversion, dict):
        lines.append("转换状态: 未返回转换报告。未生成 WT/WL。")
        return "warning", "\n".join(lines)

    outcome = conversion_outcome(conversion)
    status = str(conversion.get("status") or "")
    mode = str(conversion.get("mode") or "")
    ok = conversion.get("ok")

    if outcome == "converted":
        lines.append("转换状态: 已生成 WT/WL，并收集到 MAPGIS_READY。")
        lines.append("")
        lines.append(
            "下一步：点“打开输出文件夹”，进 MAPGIS_LOAD_READY，"
            "在 MapGIS 中装入其中的 tif + WL + WT 做叠加检查和手工编辑。"
        )
        return "ok", "\n".join(lines)

    if outcome == "prepared":
        lines.append("转换状态: prepare 已完成，只准备了 SECTION 批次；未生成 WT/WL。")
        lines.append("下一步: 切换到 conversion_mode=cli 重新运行，或单独修复/运行 MCP 桥接转换。")
        return "warning", "\n".join(lines)

    if status == "no_exchange_package":
        # 零候选也算 skipped，但值得提醒：整张图没有产出任何可转换的导出。
        lines.append("转换状态: 本次没有可转换的交换包（未生成任何线/文字导出）。未生成 WT/WL。")
        return "warning", "\n".join(lines)

    if outcome == "skipped":
        lines.append(f"转换状态: {status or mode}；本次未请求生成 WT/WL。")
        return "ok", "\n".join(lines)

    if ok is False:
        lines.append(f"转换状态: 转换未完成（{status or mode}）。未生成 WT/WL。")
        error = _conversion_error_from_report(conversion)
        if error:
            lines.append(f"原因: {error}")
        return "warning", "\n".join(lines)

    lines.append(f"转换状态: {status or mode or 'unknown'}；未确认生成 WT/WL。")
    return "warning", "\n".join(lines)


def friendly_error_message(exc: Exception) -> str:
    message = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, FileExistsError):
        return (
            "输出文件夹已经存在。若要重新跑同一张图，请勾选“覆盖已有输出（fresh rerun）”；"
            "或者换一个 Map ID / 输出父文件夹。\n\n"
            + message
        )
    return message
