from __future__ import annotations

import os
import queue
import re
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from geoscan.app_settings import (
    apply_settings_to_env,
    load_encrypted_api_key,
    read_machine_settings,
    save_encrypted_api_key,
    save_settings,
    settings_save_path,
)
from geoscan.ai_vision_review import (
    AiVisionConfig,
    analyze_map_image_with_ai,
    normalize_chat_completions_url,
    test_ai_connection,
)
from geoscan.batch_runner import (
    BatchConfig,
    discover_source_rasters,
    run_batch,
)
from geoscan.production_program import (
    ProgramConfig,
    RunCancelledError,
    default_line_target_file,
    default_text_target_file,
    derive_map_id_from_filename,
    redact_api_key,
    run_production_program,
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


def _app_icon_path() -> Path | None:
    if getattr(sys, "frozen", False):
        candidate = Path(getattr(sys, "_MEIPASS", "")) / "app_icon.ico"
        if candidate.is_file():
            return candidate
    candidate = Path(__file__).resolve().parents[2] / "packaging" / "app_icon.ico"
    return candidate if candidate.is_file() else None


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
AI_CONNECTION_TIMEOUT_SECONDS = 30
DEFAULT_AI_PROVIDER = "none"
DEFAULT_AI_BASE_URL = "https://api.siliconflow.cn/v1/chat/completions"
DEFAULT_AI_MODEL = "Qwen/Qwen3-VL-32B-Instruct"
DEFAULT_LINE_ENGINE = "trace"
DEFAULT_LINE_REPAIR = "conservative"
DEFAULT_LINE_EXPORT_SOURCE = "repaired"


@dataclass(frozen=True)
class GuiFormState:
    project_root: Path
    source_raster: Path
    map_id: str
    output_parent: Path
    text_candidates: Path | None = None
    target_line_file: str | None = None
    target_text_file: str | None = None
    ai_provider: str = "none"
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""
    conversion_mode: str = "cli"
    line_engine: str = DEFAULT_LINE_ENGINE
    line_repair: str = DEFAULT_LINE_REPAIR
    line_export_source: str = DEFAULT_LINE_EXPORT_SOURCE
    ai_enhance: bool = False
    ocr_python: Path | None = None
    export_dxf: bool = True
    reset_output: bool = False
    wait_timeout_seconds: int = 300
    level_input: str = "off"
    enhanced_preview: str = "standard"


def default_map_id_from_image(path: Path) -> str:
    """Auto-fill the Map ID field from any input filename.

    Falls back to a sanitized stem when the name is not in the t01_0007
    convention (e.g. a pure-number name), so the field never lands empty.
    """
    return derive_map_id_from_filename(path)


def default_output_root_from_parent(output_parent: Path, map_id: str) -> Path:
    compact = "".join(char for char in str(map_id).upper() if char.isalnum() or char == "_").strip("_")
    return Path(output_parent) / f"{compact}_P"


def build_program_config_from_gui(state: GuiFormState) -> ProgramConfig:
    output_root = default_output_root_from_parent(state.output_parent, state.map_id)
    text_candidates = state.text_candidates if state.text_candidates and str(state.text_candidates).strip() else None
    target_line_file = state.target_line_file or default_line_target_file(state.map_id)
    target_text_file = state.target_text_file or default_text_target_file(state.map_id)
    return ProgramConfig(
        project_root=state.project_root,
        source_raster=state.source_raster,
        map_id=state.map_id,
        output_root=output_root,
        text_candidates=text_candidates,
        target_line_file=target_line_file,
        target_text_file=target_text_file,
        ai_provider=state.ai_provider,
        ai_base_url=state.ai_base_url,
        ai_api_key=state.ai_api_key,
        ai_model=state.ai_model,
        conversion_mode=state.conversion_mode,
        line_engine=state.line_engine,
        line_repair=state.line_repair,
        line_export_source=state.line_export_source,
        ai_enhance=state.ai_enhance,
        ocr_python=state.ocr_python,
        export_dxf=state.export_dxf,
        reset_output=state.reset_output,
        wait_timeout_seconds=state.wait_timeout_seconds,
        level_input=state.level_input,
        enhanced_preview=state.enhanced_preview,
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
        line_repair=state.line_repair,
        line_export_source=state.line_export_source,
        ai_enhance=state.ai_enhance,
        ai_provider=state.ai_provider,
        ai_base_url=state.ai_base_url,
        ai_api_key=state.ai_api_key,
        ai_model=state.ai_model,
        ocr_python=state.ocr_python,
        retry_incomplete=retry_incomplete,
        limit=limit,
        wait_timeout_seconds=state.wait_timeout_seconds,
        level_input=state.level_input,
        enhanced_preview=state.enhanced_preview,
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

    conversion = report.get("conversion")
    if not isinstance(conversion, dict):
        lines.append("转换状态: 未返回转换报告。未生成 WT/WL。")
        return "warning", "\n".join(lines)

    status = str(conversion.get("status") or "")
    mode = str(conversion.get("mode") or "")
    ok = conversion.get("ok")

    if status == "converted" and ok is True:
        lines.append("转换状态: 已生成 WT/WL，并收集到 MAPGIS_READY。")
        lines.append("")
        lines.append(
            "下一步：点“打开输出文件夹”，进 MAPGIS_LOAD_READY，"
            "在 MapGIS 中装入其中的 tif + WL + WT 做叠加检查和手工编辑。"
        )
        return "ok", "\n".join(lines)

    if status == "prepared":
        lines.append("转换状态: prepare 已完成，只准备了 SECTION 批次；未生成 WT/WL。")
        lines.append("下一步: 切换到 conversion_mode=cli 重新运行，或单独修复/运行 MCP 桥接转换。")
        return "warning", "\n".join(lines)

    if status in {"not_requested", "no_text_package"} or mode == "none":
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


class ProductionGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("GeoScan")
        self.geometry("1300x880")
        self.minsize(1120, 780)
        self._setup_style()
        self._messages: queue.Queue[tuple[str, str]] = queue.Queue()

        self._machine_settings = read_machine_settings()
        initial_root = default_project_root(self._machine_settings)

        self.project_root_var = tk.StringVar(value=str(initial_root))
        self.source_raster_var = tk.StringVar()
        self.map_id_var = tk.StringVar()
        self.output_parent_var = tk.StringVar(value=str(initial_root))
        self.output_preview_var = tk.StringVar(value="")
        self.text_candidates_var = tk.StringVar()
        self.target_line_file_var = tk.StringVar()
        self.target_text_file_var = tk.StringVar()
        self.ai_provider_var = tk.StringVar(
            value=self._machine_settings.get("ai_provider", "") or DEFAULT_AI_PROVIDER
        )
        self.ai_base_url_var = tk.StringVar(
            value=self._machine_settings.get("ai_base_url", "") or DEFAULT_AI_BASE_URL
        )
        saved_key = load_encrypted_api_key()
        self.ai_api_key_var = tk.StringVar(value=saved_key)
        self.ai_save_key_var = tk.BooleanVar(value=bool(saved_key))
        self.ai_model_var = tk.StringVar(
            value=self._machine_settings.get("ai_model", "") or DEFAULT_AI_MODEL
        )
        self.ai_enhance_var = tk.BooleanVar(
            value=self._machine_settings.get("ai_enhance", "").lower() == "true"
        )
        self.conversion_mode_var = tk.StringVar(value="cli")
        self.level_input_var = tk.StringVar(value="off")
        self.enhanced_preview_var = tk.StringVar(value="standard")
        self.line_engine_var = tk.StringVar(value=DEFAULT_LINE_ENGINE)
        self.line_repair_var = tk.StringVar(value=DEFAULT_LINE_REPAIR)
        self.line_export_source_var = tk.StringVar(value=DEFAULT_LINE_EXPORT_SOURCE)
        self.ocr_python_var = tk.StringVar(
            value=self._machine_settings.get("ocr_python", "")
            or os.environ.get("MAPGIS_OCR_PYTHON", "")
        )
        self.settings_section_exe_var = tk.StringVar(
            value=self._machine_settings.get("section_exe", "")
            or os.environ.get("MAPGIS67_SECTION_EXE", "")
        )
        self.settings_w60_conv_exe_var = tk.StringVar(
            value=self._machine_settings.get("w60_conv_exe", "")
            or os.environ.get("MAPGIS67_W60_CONV_EXE", "")
        )
        self.settings_ogr2ogr_var = tk.StringVar(
            value=self._machine_settings.get("ogr2ogr", "")
            or os.environ.get("MAPGIS_OGR2OGR", "")
        )
        self.settings_gdal_data_var = tk.StringVar(
            value=self._machine_settings.get("gdal_data", "")
            or os.environ.get("MAPGIS_GDAL_DATA", "")
        )
        self.settings_file_var = tk.StringVar(value=str(settings_save_path()))
        self.export_dxf_var = tk.BooleanVar(value=True)
        self.reset_output_var = tk.BooleanVar(value=False)
        self.timeout_var = tk.StringVar(value="300")
        self.batch_source_dir_var = tk.StringVar()
        self.batch_limit_var = tk.StringVar()
        self.batch_retry_incomplete_var = tk.BooleanVar(value=False)
        self._batch_stop_requested = threading.Event()
        self._run_stop_requested = threading.Event()
        self._batch_rows: queue.Queue[dict[str, Any]] = queue.Queue()
        self.status_var = tk.StringVar(value="就绪")
        self.env_status_var = tk.StringVar(value="")

        self._build_layout()
        self._wire_updates()
        self._refresh_env_status()
        self.after(200, self._drain_messages)
        self.after(300, self._drain_batch_rows)
        self.after(400, self._warn_non_ascii_install_path)

    def _setup_style(self) -> None:
        try:
            style = ttk.Style(self)
            themed = False
            try:
                import sv_ttk  # Sun Valley: modern rounded Fluent look.

                sv_ttk.set_theme("light")
                themed = True
            except Exception:
                if "vista" in style.theme_names():
                    style.theme_use("vista")
            self.option_add("*Font", "{Microsoft YaHei UI} 10")
            style.configure(".", font=("Microsoft YaHei UI", 10))
            style.configure("TNotebook.Tab", font=("Microsoft YaHei UI", 10))
            # Primary action button: sv-ttk's accent style (blue, rounded) when
            # available, enlarged either way.
            self._run_button_style = "Accent.TButton" if themed else "TButton"
            style.configure(
                self._run_button_style,
                font=("Microsoft YaHei UI", 11, "bold"),
                padding=(26, 9),
            )
            # Secondary buttons on the same row share the primary button's
            # height (same font size + vertical padding), just not bold/blue.
            style.configure(
                "Side.TButton",
                font=("Microsoft YaHei UI", 11),
                padding=(16, 9),
            )
            style.configure("Guide.TLabel", foreground="#0b5d1e", font=("Microsoft YaHei UI", 10, "bold"))
            style.configure("Hint.TLabel", foreground="#6b6b6b")
            style.configure("Ok.TLabel", foreground="#0b7a2b")
            style.configure("Warn.TLabel", foreground="#b3261e", font=("Microsoft YaHei UI", 10, "bold"))
            try:
                icon = _app_icon_path()
                if icon is not None:
                    self.iconbitmap(default=str(icon))
            except tk.TclError:
                pass
        except tk.TclError:
            pass

    def _refresh_env_status(self) -> None:
        from geoscan.env_probe import (
            DONGLE_PROCESS_NAME,
            dongle_process_running,
        )

        missing = self._missing_conversion_tools()
        if missing:
            self.env_status_var.set(
                "✘ 未找到 " + "、".join(missing) + " —— 请到“设置”页点“自动探测本机程序”并保存"
            )
            if hasattr(self, "env_status_label"):
                self.env_status_label.configure(style="Warn.TLabel")
            return
        # Tools are present; also surface the dongle service state (cli needs it).
        if dongle_process_running():
            self.env_status_var.set(
                "✔ 本机转换环境就绪（SECTION / W60 / 自带 ogr2ogr 都已找到；"
                f"密码狗 {DONGLE_PROCESS_NAME} 运行中）"
            )
            if hasattr(self, "env_status_label"):
                self.env_status_label.configure(style="Ok.TLabel")
        else:
            self.env_status_var.set(
                f"⚠ SECTION/W60/ogr2ogr 已找到，但密码狗服务 {DONGLE_PROCESS_NAME} 未运行"
                "——cli 转换会失败，请插好加密狗并启动它（改用 none/prepare 则不需要）"
            )
            if hasattr(self, "env_status_label"):
                self.env_status_label.configure(style="Warn.TLabel")

    def _build_layout(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill=tk.BOTH, expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(8, weight=1)

        ttk.Label(
            root,
            text="新手三步：①选择输入图片（Map ID 自动识别）→ ②点“开始运行” → "
            "③完成后点“打开输出文件夹”，在 MapGIS 中装入 MAPGIS_LOAD_READY 里的文件",
            style="Guide.TLabel",
            wraplength=940,
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))
        self.env_status_label = ttk.Label(root, textvariable=self.env_status_var, style="Hint.TLabel")
        self.env_status_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self._path_row(root, 2, "① 输入图片", self.source_raster_var, self._choose_source_raster)
        self._entry_row(root, 3, "Map ID（自动识别）", self.map_id_var)
        self._path_row(root, 4, "工作目录", self.project_root_var, self._choose_project_root)
        self._path_row(root, 5, "输出到", self.output_parent_var, self._choose_output_parent)
        self._readonly_row(root, 6, "结果将保存在", self.output_preview_var)

        options_row = ttk.Frame(root)
        options_row.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(options_row, text="输入调平").pack(side=tk.LEFT)
        ttk.Combobox(
            options_row,
            textvariable=self.level_input_var,
            values=("off", "auto", "force"),
            state="readonly",
            width=8,
        ).pack(side=tk.LEFT, padx=(4, 4))
        ttk.Label(
            options_row,
            text="（默认 off=不调平，已处理好的图直接用；原始扫描图想纠偏转 TIFF 才选 auto/force）",
            style="Hint.TLabel",
        ).pack(side=tk.LEFT, padx=(0, 18))
        ttk.Label(options_row, text="增强底图（人工看图）").pack(side=tk.LEFT)
        ttk.Combobox(
            options_row,
            textvariable=self.enhanced_preview_var,
            values=("none", "light", "standard", "strong"),
            state="readonly",
            width=10,
        ).pack(side=tk.LEFT, padx=(4, 4))
        ttk.Label(
            options_row,
            text="（额外生成锐化底图，几何与矢量对齐，装它修图更清楚；none=不生成）",
            style="Hint.TLabel",
        ).pack(side=tk.LEFT)

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(10, 8))

        self.log_tab = ttk.Frame(self.notebook, padding=10)
        self.batch_tab = ttk.Frame(self.notebook, padding=10)
        settings_tab = ttk.Frame(self.notebook, padding=10)
        ai_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.log_tab, text="运行日志")
        self.notebook.add(self.batch_tab, text="批量运行")
        self.notebook.add(settings_tab, text="设置")
        self.notebook.add(ai_tab, text="AI 接入（可选）")

        self._build_log_tab(self.log_tab)
        self._build_batch_tab(self.batch_tab)
        self._build_settings_tab(settings_tab)
        self._build_ai_tab(ai_tab)

        progress_row = ttk.Frame(root)
        progress_row.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(0, 6))
        progress_row.columnconfigure(1, weight=1)
        self.status_label = ttk.Label(progress_row, textvariable=self.status_var, style="Hint.TLabel")
        self.status_label.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(progress_row, mode="indeterminate", length=260)
        self.progress.grid(row=0, column=1, sticky="e")
        self.progress.grid_remove()

        button_bar = ttk.Frame(root)
        button_bar.grid(row=10, column=0, columnspan=3, sticky="ew")
        button_bar.columnconfigure(1, weight=1)

        ttk.Checkbutton(
            button_bar,
            text="覆盖已有输出（重跑同一张图时勾选；会先自动备份手工成果）",
            variable=self.reset_output_var,
        ).grid(row=0, column=0, sticky="w")
        self.run_button = ttk.Button(
            button_bar,
            text="② 开始运行",
            style=getattr(self, "_run_button_style", "TButton"),
            command=self._run,
        )
        self.run_button.grid(row=0, column=2, padx=(8, 0))
        self.stop_button = ttk.Button(
            button_bar,
            text="安全停止",
            style="Side.TButton",
            command=self._request_stop,
            state="disabled",
        )
        self.stop_button.grid(row=0, column=3, padx=(8, 0))
        ttk.Button(
            button_bar,
            text="③ 打开输出文件夹",
            style="Side.TButton",
            command=self._open_output_folder,
        ).grid(row=0, column=4, padx=(8, 0))

    def _request_stop(self) -> None:
        self._run_stop_requested.set()
        self._batch_stop_requested.set()
        self.status_var.set("已请求停止：当前阶段结束后立即停止（SECTION 转换不会被中途打断）…")
        self._log("已请求安全停止：单图会在当前阶段结束后停止；批量会在当前图完成后停止。")

    def _open_advanced_dialog(self) -> None:
        if getattr(self, "_advanced_dialog", None) is not None and self._advanced_dialog.winfo_exists():
            self._advanced_dialog.lift()
            self._advanced_dialog.focus_force()
            return
        dialog = tk.Toplevel(self)
        dialog.title("高级运行参数")
        dialog.transient(self)
        dialog.resizable(False, False)
        self._advanced_dialog = dialog
        parent = ttk.Frame(dialog, padding=14)
        parent.pack(fill=tk.BOTH, expand=True)
        parent.columnconfigure(1, weight=1)
        ttk.Label(
            parent,
            text="默认值就是正式推荐配置，一般无需改动。关闭窗口即生效（随本次运行使用）。",
            style="Hint.TLabel",
        ).grid(row=99, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(parent, text="关闭", command=dialog.destroy).grid(
            row=100, column=2, sticky="e", pady=(10, 0)
        )
        ttk.Label(parent, text="转换模式").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(
            parent,
            textvariable=self.conversion_mode_var,
            values=("none", "prepare", "cli"),
            state="readonly",
            width=18,
        ).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(
            parent,
            text="cli=自动转出 WT/WL（推荐）；none=只出候选和 DXF",
            style="Hint.TLabel",
        ).grid(row=0, column=2, sticky="w", pady=4)

        ttk.Label(parent, text="目标线文件 WL").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.target_line_file_var).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(parent, text="目标文字文件 WT").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.target_text_file_var).grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(parent, text="线提取引擎").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(
            parent,
            textvariable=self.line_engine_var,
            values=("hough", "trace"),
            state="readonly",
            width=18,
        ).grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(
            parent,
            text="trace=手工修改优先；hough=快速直线旧模式",
            foreground="#555555",
        ).grid(row=3, column=2, sticky="w", pady=4)

        ttk.Label(parent, text="线修复").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(
            parent,
            textvariable=self.line_repair_var,
            values=("off", "conservative"),
            state="readonly",
            width=18,
        ).grid(row=4, column=1, sticky="w", pady=4)

        ttk.Label(parent, text="导出线层").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Combobox(
            parent,
            textvariable=self.line_export_source_var,
            values=("raw", "repaired", "ai_enhanced"),
            state="readonly",
            width=18,
        ).grid(row=5, column=1, sticky="w", pady=4)
        ttk.Label(
            parent,
            text="repaired 需开启线修复；ai_enhanced 需同时在 AI 页启用 AI 增强",
            foreground="#555555",
        ).grid(row=5, column=2, sticky="w", pady=4)

        # 输入调平 / 增强底图 已移到主界面（更常用、更好找），不再放在此高级弹窗。

        ttk.Label(parent, text="转换等待秒数").grid(row=7, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.timeout_var, width=12).grid(row=7, column=1, sticky="w", pady=4)

        ttk.Checkbutton(parent, text="导出 DXF", variable=self.export_dxf_var).grid(row=8, column=1, sticky="w", pady=4)

        ttk.Label(parent, text="文字候选 GeoJSON（高级，可选）").grid(row=10, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.text_candidates_var).grid(
            row=10, column=1, sticky="ew", pady=4, padx=(8, 8)
        )
        ttk.Button(parent, text="选择", command=self._choose_text_candidates).grid(
            row=10, column=2, sticky="w", pady=4
        )
        ttk.Label(
            parent,
            text="留空=自动生成（正常用法）。只有要用人工整理过的文字层时才选择；不要选旧运行的输出。",
            style="Hint.TLabel",
        ).grid(row=11, column=1, columnspan=2, sticky="w")

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)

        ttk.Label(
            parent,
            text="每台电脑的 MapGIS/QGIS 安装目录不同；在这里选择本机路径并保存，"
            "保存后立即生效，下次启动自动加载。",
            foreground="#555555",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        rows = (
            (1, "SECTION 程序 (section.exe)", self.settings_section_exe_var,
             lambda: self._choose_settings_file(self.settings_section_exe_var, "选择 SECTION 程序", "section*.exe")),
            (2, "W60 转换程序 (W60_Conv.exe)", self.settings_w60_conv_exe_var,
             lambda: self._choose_settings_file(self.settings_w60_conv_exe_var, "选择 W60 转换程序", "*.exe")),
            (3, "ogr2ogr (QGIS)", self.settings_ogr2ogr_var,
             lambda: self._choose_settings_file(self.settings_ogr2ogr_var, "选择 ogr2ogr.exe", "ogr2ogr*.exe")),
        )
        for row, label, variable, command in rows:
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4, padx=(8, 8))
            ttk.Button(parent, text="选择", command=command).grid(row=row, column=2, sticky="e", pady=4)

        ttk.Label(parent, text="GDAL 数据目录（可选）").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.settings_gdal_data_var).grid(
            row=4, column=1, sticky="ew", pady=4, padx=(8, 8)
        )
        ttk.Button(parent, text="选择", command=self._choose_settings_gdal_data).grid(
            row=4, column=2, sticky="e", pady=4
        )

        ttk.Label(parent, text="OCR 解释器（可选）").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.ocr_python_var).grid(
            row=5, column=1, sticky="ew", pady=4, padx=(8, 8)
        )
        ttk.Button(parent, text="选择", command=self._choose_ocr_python).grid(
            row=5, column=2, sticky="e", pady=4
        )

        ttk.Label(parent, text="设置文件").grid(row=6, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.settings_file_var, state="readonly").grid(
            row=6, column=1, columnspan=2, sticky="ew", pady=4, padx=(8, 0)
        )

        ttk.Label(
            parent,
            text="设置文件不保存任何 API Key。OCR 解释器留空时文字候选为占位框，属正常。",
            style="Hint.TLabel",
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(8, 0))

        button_bar = ttk.Frame(parent)
        button_bar.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        button_bar.columnconfigure(0, weight=1)
        from geoscan import __version__ as _app_version

        self._update_button = ttk.Button(
            button_bar,
            text=f"检查更新（当前 v{_app_version}）",
            command=self._check_for_update,
        )
        self._update_button.grid(row=0, column=0, sticky="w")
        ttk.Button(button_bar, text="高级运行参数…", command=self._open_advanced_dialog).grid(
            row=0, column=1, sticky="e", padx=(0, 8)
        )
        ttk.Button(button_bar, text="自动探测本机程序", command=self._autodetect_tools).grid(
            row=0, column=2, sticky="e", padx=(0, 8)
        )
        ttk.Button(button_bar, text="保存本机设置", command=self._save_machine_settings).grid(
            row=0, column=3, sticky="e"
        )

    # ------------------------------------------------------------------
    # Auto-update (GitHub Releases)
    # ------------------------------------------------------------------
    def _check_for_update(self) -> None:
        """Check GitHub Releases for a newer version, then offer to install it."""
        button = getattr(self, "_update_button", None)
        if button is not None:
            button.configure(state="disabled")
        self.status_var.set("正在检查更新…")

        def worker() -> None:
            from geoscan import updater

            try:
                info = updater.check_for_update()
                self.after(0, lambda: self._on_update_checked(info, None))
            except updater.UpdateError as exc:
                self.after(0, lambda: self._on_update_checked(None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_checked(self, info, error: str | None) -> None:
        button = getattr(self, "_update_button", None)
        if button is not None:
            button.configure(state="normal")
        self.status_var.set("")

        if error is not None:
            messagebox.showwarning("检查更新", error)
            return
        if not info.update_available:
            messagebox.showinfo("检查更新", f"已是最新版本（v{info.current}）。")
            return

        notes = info.notes.strip()
        if len(notes) > 800:
            notes = notes[:800] + "…"
        size_mb = info.installer_size / (1024 * 1024) if info.installer_size else 0
        prompt = (
            f"发现新版本：v{info.latest}（当前 v{info.current}）。\n"
            f"安装包约 {size_mb:.1f} MB。\n\n"
            f"{notes}\n\n"
            "现在下载并安装？安装时本程序会关闭，你的本机设置不会丢失。"
        )
        if not messagebox.askyesno("发现新版本", prompt):
            return
        self._download_and_install_update(info)

    def _download_and_install_update(self, info) -> None:
        from geoscan import updater

        button = getattr(self, "_update_button", None)
        if button is not None:
            button.configure(state="disabled")

        def progress(done: int, total: int) -> None:
            if total:
                pct = done * 100 // total
                self.after(0, lambda: self.status_var.set(f"正在下载更新… {pct}%"))
            else:
                mb = done / (1024 * 1024)
                self.after(0, lambda: self.status_var.set(f"正在下载更新… {mb:.1f} MB"))

        def worker() -> None:
            try:
                path = updater.download_installer(info, progress=progress)
                self.after(0, lambda: self._launch_downloaded_installer(path))
            except updater.UpdateError as exc:
                self.after(0, lambda: self._on_update_download_failed(str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_download_failed(self, error: str) -> None:
        self.status_var.set("")
        button = getattr(self, "_update_button", None)
        if button is not None:
            button.configure(state="normal")
        messagebox.showerror("下载更新失败", error)

    def _launch_downloaded_installer(self, path) -> None:
        from geoscan import updater

        self.status_var.set("下载完成，正在启动安装程序…")
        if not messagebox.askyesno(
            "安装更新",
            "下载完成。现在关闭本程序并开始安装？\n（点“否”可稍后手动运行安装包）",
        ):
            self.status_var.set("")
            button = getattr(self, "_update_button", None)
            if button is not None:
                button.configure(state="normal")
            messagebox.showinfo("安装更新", f"安装包已保存到：\n{path}")
            return
        try:
            updater.launch_installer_and_exit(path)  # exits the process
        except updater.UpdateError as exc:
            messagebox.showerror("启动安装程序失败", str(exc))

    def _warn_non_ascii_install_path(self) -> None:
        # ogr2ogr cannot read a GDAL_DATA dir whose path has Chinese characters;
        # export auto-falls-back to an ASCII temp copy, so only warn when even
        # that fallback is unavailable on this machine.
        from geoscan.production_accuracy_workflow import (
            non_ascii_install_path_problem,
        )

        try:
            problem = non_ascii_install_path_problem()
        except Exception:
            return
        if problem:
            self._log(problem)
            messagebox.showwarning("程序路径包含中文", problem)

    def _autodetect_tools(self) -> None:
        from geoscan.env_probe import program_candidates
        from geoscan.production_accuracy_workflow import (
            resolve_gdal_data,
            resolve_ogr2ogr,
        )

        filled: list[str] = []
        for variable, program, label in (
            (self.settings_section_exe_var, "section", "SECTION 程序"),
            (self.settings_w60_conv_exe_var, "w60_conv", "W60 转换程序"),
        ):
            if variable.get().strip():
                continue
            for candidate in program_candidates(program):
                if candidate.is_file():
                    variable.set(str(candidate))
                    filled.append(f"{label}: {candidate}")
                    break
        if not self.settings_ogr2ogr_var.get().strip():
            candidate = Path(resolve_ogr2ogr())
            if candidate.is_file():
                self.settings_ogr2ogr_var.set(str(candidate))
                filled.append(f"ogr2ogr: {candidate}")
        if not self.settings_gdal_data_var.get().strip():
            candidate = Path(resolve_gdal_data())
            if candidate.is_dir():
                self.settings_gdal_data_var.set(str(candidate))
                filled.append(f"GDAL 数据目录: {candidate}")
        if filled:
            self._refresh_env_status()
            self._log("自动探测到本机程序：")
            for line in filled:
                self._log(f"  {line}")
            messagebox.showinfo("探测完成", "已填入探测到的路径，请确认后点“保存本机设置”。")
        else:
            messagebox.showwarning(
                "未探测到新路径",
                "没有探测到可自动填入的程序路径（已填写的不会覆盖）。请手动选择本机的"
                " MapGIS67 安装目录下 program\\section.exe 与 W60_Conv.exe。",
            )

    def _choose_settings_file(self, variable: tk.StringVar, title: str, pattern: str) -> None:
        current = variable.get().strip()
        initialdir = str(Path(current).parent) if current and Path(current).parent.is_dir() else None
        path = filedialog.askopenfilename(
            title=title,
            initialdir=initialdir,
            filetypes=((pattern, pattern), ("All files", "*.*")),
        )
        if path:
            variable.set(path)

    def _choose_settings_gdal_data(self) -> None:
        folder = filedialog.askdirectory(title="选择 GDAL 数据目录（QGIS 的 share\\gdal）")
        if folder:
            self.settings_gdal_data_var.set(folder)

    def _machine_settings_from_form(self) -> dict[str, str]:
        return {
            "section_exe": self.settings_section_exe_var.get().strip(),
            "w60_conv_exe": self.settings_w60_conv_exe_var.get().strip(),
            "ogr2ogr": self.settings_ogr2ogr_var.get().strip(),
            "gdal_data": self.settings_gdal_data_var.get().strip(),
            "ocr_python": self.ocr_python_var.get().strip(),
            "project_root": self.project_root_var.get().strip(),
            # AI provider/url/model only — the API key is NEVER saved.
            "ai_provider": self.ai_provider_var.get().strip(),
            "ai_base_url": self.ai_base_url_var.get().strip(),
            "ai_model": self.ai_model_var.get().strip(),
            "ai_enhance": "true" if self.ai_enhance_var.get() else "",
        }

    def _save_machine_settings(self) -> None:
        settings = self._machine_settings_from_form()
        missing = [
            (label, value)
            for label, key, must_be_dir in (
                ("SECTION 程序", "section_exe", False),
                ("W60 转换程序", "w60_conv_exe", False),
                ("ogr2ogr", "ogr2ogr", False),
                ("GDAL 数据目录", "gdal_data", True),
                ("OCR 解释器", "ocr_python", False),
                ("项目根目录", "project_root", True),
            )
            if (value := settings.get(key, "").strip())
            and not (Path(value).is_dir() if must_be_dir else Path(value).is_file())
        ]
        if missing:
            details = "\n".join(f"{label}: {value}" for label, value in missing)
            messagebox.showerror("路径不存在", f"以下路径在本机不存在，请重新选择：\n{details}")
            return
        try:
            target = save_settings(settings)
        except ValueError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        applied = apply_settings_to_env(settings, override=True)
        self.settings_file_var.set(str(target))
        self._refresh_env_status()
        self._log(f"已保存本机设置: {target}")
        for env_name, value in applied.items():
            self._log(f"  {env_name} = {value}")

        key_note = ""
        try:
            if self.ai_save_key_var.get() and self.ai_api_key_var.get().strip():
                key_file = save_encrypted_api_key(self.ai_api_key_var.get())
                key_note = f"\nAPI Key 已加密保存（仅本机本用户可解密）：\n{key_file}"
                self._log(f"API Key 已用 Windows 账户级加密保存: {key_file}")
            else:
                removed = save_encrypted_api_key("")
                if self.ai_api_key_var.get().strip():
                    key_note = "\nAPI Key 未保存（未勾选加密保存，仅本次会话有效）。"
                _ = removed
        except Exception as exc:
            key_note = f"\nAPI Key 保存失败（本次会话仍可用）: {exc}"
            self._log(f"API Key 加密保存失败: {exc}")

        messagebox.showinfo("已保存", f"本机设置已保存并立即生效：\n{target}{key_note}")

    def _build_ai_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        ttk.Label(parent, text="Provider").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(
            parent,
            textvariable=self.ai_provider_var,
            values=("none", "openai-compatible", "qwen", "custom"),
            state="readonly",
            width=24,
        ).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(parent, text="Base URL").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.ai_base_url_var).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(parent, text="API Key").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.ai_api_key_var, show="*").grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(parent, text="Model").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.ai_model_var).grid(row=3, column=1, sticky="ew", pady=4)

        ttk.Checkbutton(
            parent,
            text="在本机加密保存 API Key（Windows 账户级加密；拷贝文件夹给别人时 Key 解不出来）",
            variable=self.ai_save_key_var,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        ttk.Checkbutton(
            parent,
            text="启用 AI 增强（可选附加层：断线桥接提名 + OCR 常用词纠错建议）",
            variable=self.ai_enhance_var,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(
            parent,
            text="AI 只提名，程序算坐标并用栅格证据验证；结果写入全新增强层，"
            "raw/repaired 永不改动，全部 checked=no。",
            foreground="#555555",
        ).grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(
            parent,
            text="AI 只写复核建议到 AI_VISUAL_REVIEW；不写最终坐标、不写 checked=yes。",
            foreground="#555555",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))

        button_bar = ttk.Frame(parent)
        button_bar.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        button_bar.columnconfigure(0, weight=1)
        ttk.Button(
            button_bar, text="保存 AI 设置", command=self._save_machine_settings
        ).grid(row=0, column=1, sticky="e")
        self.ai_test_button = ttk.Button(button_bar, text="测试 AI 连接", command=self._test_ai_connection)
        self.ai_test_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.ai_analyze_button = ttk.Button(
            button_bar, text="AI 看图描述（仅诊断，不影响结果）", command=self._run_ai_visual_analysis
        )
        self.ai_analyze_button.grid(row=0, column=3, sticky="e", padx=(8, 0))

    def _build_batch_tab(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(4, weight=1)

        ttk.Label(parent, text="图源文件夹").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.batch_source_dir_var).grid(
            row=0, column=1, sticky="ew", pady=4, padx=(8, 8)
        )
        ttk.Button(parent, text="选择", command=self._choose_batch_source_dir).grid(
            row=0, column=2, sticky="e", pady=4
        )

        ttk.Label(parent, text="本次最多跑 N 张（留空=全部）").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=self.batch_limit_var, width=12).grid(
            row=1, column=1, sticky="w", pady=4, padx=(8, 0)
        )
        ttk.Checkbutton(
            parent,
            text="重跑不完整的图（会清空其输出后重来）",
            variable=self.batch_retry_incomplete_var,
        ).grid(row=2, column=1, sticky="w", pady=4, padx=(8, 0))

        ttk.Label(
            parent,
            text="批量使用当前设置（含“设置”页高级参数和“AI 接入”页）；一次一张图；已完成的图自动跳过（可断点续跑）。",
            foreground="#555555",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 4))

        columns = ("map_id", "status", "lines", "texts", "conversion", "error")
        self.batch_tree = ttk.Treeview(parent, columns=columns, show="headings", height=10)
        headings = {
            "map_id": "Map ID",
            "status": "状态",
            "lines": "线候选",
            "texts": "文字候选",
            "conversion": "转换",
            "error": "错误",
        }
        widths = {"map_id": 100, "status": 150, "lines": 70, "texts": 70, "conversion": 110, "error": 260}
        for column in columns:
            self.batch_tree.heading(column, text=headings[column])
            self.batch_tree.column(column, width=widths[column], anchor="w")
        self.batch_tree.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(2, 6))
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.batch_tree.yview)
        scrollbar.grid(row=4, column=3, sticky="ns")
        self.batch_tree.configure(yscrollcommand=scrollbar.set)

        button_bar = ttk.Frame(parent)
        button_bar.grid(row=5, column=0, columnspan=3, sticky="ew")
        button_bar.columnconfigure(0, weight=1)
        self.batch_run_button = ttk.Button(button_bar, text="开始批量", command=self._run_batch)
        self.batch_run_button.grid(row=0, column=1, padx=(8, 0))
        self.batch_stop_button = ttk.Button(
            button_bar, text="完成当前图后停止", command=self._request_batch_stop, state="disabled"
        )
        self.batch_stop_button.grid(row=0, column=2, padx=(8, 0))

    def _build_log_tab(self, parent: ttk.Frame) -> None:
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        self.log_text = tk.Text(parent, height=14, wrap="word")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        command: object,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=4, padx=(8, 8))
        ttk.Button(parent, text="选择", command=command).grid(row=row, column=2, sticky="e", pady=4)

    def _entry_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, columnspan=2, sticky="ew", pady=4, padx=(8, 0))

    def _readonly_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable, state="readonly").grid(
            row=row, column=1, columnspan=2, sticky="ew", pady=4, padx=(8, 0)
        )

    def _wire_updates(self) -> None:
        for variable in (self.output_parent_var, self.map_id_var):
            variable.trace_add("write", lambda *_: self._refresh_output_preview())
        self.map_id_var.trace_add("write", lambda *_: self._refresh_target_files())

    def _choose_project_root(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.project_root_var.get() or str(DEFAULT_PROJECT_ROOT))
        if folder:
            self.project_root_var.set(folder)

    def _choose_source_raster(self) -> None:
        current_root = self.project_root_var.get().strip()
        initialdir = current_root if current_root and Path(current_root).is_dir() else None
        path = filedialog.askopenfilename(
            title="选择输入图片",
            initialdir=initialdir,
            filetypes=(
                ("Image files", "*.tif *.tiff *.jpg *.jpeg *.png *.bmp"),
                ("All files", "*.*"),
            ),
        )
        if path:
            self.source_raster_var.set(path)
            map_id = default_map_id_from_image(Path(path))
            if map_id and not self.map_id_var.get().strip():
                self.map_id_var.set(map_id)
            # Point the working + output folders at the image's own folder (the
            # user's data workspace). This keeps outputs next to the data and,
            # crucially, out of the read-only install dir. The user can still
            # override either folder with its 选择 button afterwards.
            image_dir = str(Path(path).resolve().parent)
            self.project_root_var.set(image_dir)
            self.output_parent_var.set(image_dir)
            self._log(f"工作目录/输出目录已跟随图片文件夹: {image_dir}")

    def _choose_output_parent(self) -> None:
        folder = filedialog.askdirectory(initialdir=self.output_parent_var.get() or str(DEFAULT_PROJECT_ROOT))
        if folder:
            self.output_parent_var.set(folder)

    def _choose_text_candidates(self) -> None:
        path = filedialog.askopenfilename(
            title="选择文字候选 GeoJSON",
            filetypes=(("GeoJSON files", "*.geojson *.json"), ("All files", "*.*")),
        )
        if path:
            self.text_candidates_var.set(path)

    def _choose_ocr_python(self) -> None:
        path = filedialog.askopenfilename(
            title="选择带 rapidocr 的 Python 解释器（留空则自动探测）",
            filetypes=(("Python", "python*.exe"), ("All files", "*.*")),
        )
        if path:
            self.ocr_python_var.set(path)

    def _choose_batch_source_dir(self) -> None:
        folder = filedialog.askdirectory(title="选择包含源 TIFF 的文件夹")
        if folder:
            self.batch_source_dir_var.set(folder)

    def _refresh_output_preview(self) -> None:
        map_id = self.map_id_var.get().strip()
        parent = self.output_parent_var.get().strip()
        if map_id and parent:
            self.output_preview_var.set(str(default_output_root_from_parent(Path(parent), map_id)))
        else:
            self.output_preview_var.set("")

    def _refresh_target_files(self) -> None:
        if not self.target_line_file_var.get().strip() and self.map_id_var.get().strip():
            self.target_line_file_var.set(default_line_target_file(self.map_id_var.get().strip()))
        if not self.target_text_file_var.get().strip() and self.map_id_var.get().strip():
            self.target_text_file_var.set(default_text_target_file(self.map_id_var.get().strip()))

    def _form_state(self) -> GuiFormState:
        wait_timeout = int(self.timeout_var.get().strip() or "300")
        text_path = self.text_candidates_var.get().strip()
        return GuiFormState(
            project_root=Path(self.project_root_var.get().strip()),
            source_raster=Path(self.source_raster_var.get().strip()),
            map_id=self.map_id_var.get().strip(),
            output_parent=Path(self.output_parent_var.get().strip()),
            text_candidates=Path(text_path) if text_path else None,
            target_line_file=self.target_line_file_var.get().strip() or None,
            target_text_file=self.target_text_file_var.get().strip() or None,
            ai_provider=self.ai_provider_var.get().strip() or "none",
            ai_base_url=self.ai_base_url_var.get().strip(),
            ai_api_key=self.ai_api_key_var.get().strip(),
            ai_model=self.ai_model_var.get().strip(),
            conversion_mode=self.conversion_mode_var.get().strip(),
            line_engine=self.line_engine_var.get().strip() or "hough",
            line_repair=self.line_repair_var.get().strip() or "off",
            line_export_source=self.line_export_source_var.get().strip() or "raw",
            ai_enhance=self.ai_enhance_var.get(),
            ocr_python=Path(self.ocr_python_var.get().strip()) if self.ocr_python_var.get().strip() else None,
            export_dxf=self.export_dxf_var.get(),
            reset_output=self.reset_output_var.get(),
            wait_timeout_seconds=wait_timeout,
            level_input=self.level_input_var.get().strip() or "off",
            enhanced_preview=self.enhanced_preview_var.get().strip() or "standard",
        )

    def _validate_form(self, state: GuiFormState) -> str | None:
        if not state.source_raster.is_file():
            return "请选择存在的输入图片。"
        if state.source_raster.suffix.lower() not in IMAGE_EXTENSIONS:
            return "输入图片格式不在当前支持列表内。"
        if not state.map_id:
            return "请填写 Map ID，例如 T01_0006。"
        if not sanitize_map_id(state.map_id):
            return "Map ID 至少要包含一个字母或数字，例如 T01_0006 或 12345。"
        if not state.output_parent:
            return "请选择输出父文件夹。"
        if state.text_candidates is not None and not state.text_candidates.is_file():
            return "文字候选 GeoJSON 不存在。"
        if state.conversion_mode not in {"none", "prepare", "cli"}:
            return "转换模式只能是 none、prepare 或 cli。"
        if state.line_engine not in {"hough", "trace"}:
            return "线提取引擎只能是 hough 或 trace。"
        if state.level_input not in {"auto", "force", "off"}:
            return "输入调平只能是 auto、force 或 off。"
        if state.enhanced_preview not in {"none", "light", "standard", "strong"}:
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
            return "导出线层选 ai_enhanced 时，必须同时在 AI 接入页勾选启用 AI 增强（新鲜运行规则）。"
        if state.ocr_python is not None and not state.ocr_python.is_file():
            return "OCR 解释器路径不存在；留空可自动探测。"
        if state.conversion_mode == "cli":
            missing = self._missing_conversion_tools()
            if missing:
                return (
                    f"转换模式 cli 需要本机的 {'、'.join(missing)}，但没有找到。\n"
                    "请到“设置”页点“自动探测本机程序”，或手动选择 MapGIS67 安装目录下的"
                    " program\\section.exe 与 W60_Conv.exe 并保存；"
                    "也可以先把转换模式改为 none/prepare 只生成候选包。"
                )
        return None

    def _missing_conversion_tools(self) -> list[str]:
        from geoscan.env_probe import program_candidates

        missing: list[str] = []
        for variable, program, label in (
            (self.settings_section_exe_var, "section", "SECTION 程序 (section.exe)"),
            (self.settings_w60_conv_exe_var, "w60_conv", "W60 转换程序 (W60_Conv.exe)"),
        ):
            configured = variable.get().strip()
            if configured and Path(configured).is_file():
                continue
            if any(candidate.is_file() for candidate in program_candidates(program)):
                continue
            missing.append(label)
        return missing

    def _validate_ai_form(self, state: GuiFormState, *, need_image: bool) -> str | None:
        if state.ai_provider == "none":
            return "请先在 AI 接入页选择 Provider。"
        if not state.ai_base_url:
            return "请填写 AI Base URL。"
        if not state.ai_api_key:
            return "请填写 AI API Key。"
        if not state.ai_model:
            return "请填写 AI Model。"
        if need_image:
            base_error = self._validate_form(state)
            if base_error:
                return base_error
        return None

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        if hasattr(self, "progress"):
            if busy:
                self.progress.grid()
                self.progress.start(12)
                self.status_var.set("正在运行，请勿关闭窗口…（大图可能需要几分钟到几十分钟，进度看“运行日志”页）")
            else:
                self.progress.stop()
                self.progress.grid_remove()
                self.status_var.set("就绪")
        self.run_button.configure(state=state)
        if hasattr(self, "stop_button"):
            self.stop_button.configure(state="normal" if busy else "disabled")
        if hasattr(self, "ai_test_button"):
            self.ai_test_button.configure(state=state)
        if hasattr(self, "ai_analyze_button"):
            self.ai_analyze_button.configure(state=state)
        if hasattr(self, "batch_run_button"):
            self.batch_run_button.configure(state=state)
        if hasattr(self, "batch_stop_button"):
            self.batch_stop_button.configure(state="normal" if busy else "disabled")

    def _run(self) -> None:
        try:
            state = self._form_state()
        except ValueError:
            messagebox.showerror("参数错误", "等待秒数必须是整数。")
            return
        error = self._validate_form(state)
        if error:
            messagebox.showerror("参数错误", error)
            return

        config = build_program_config_from_gui(state)
        if config.conversion_mode == "cli":
            from geoscan.env_probe import (
                DONGLE_PROCESS_NAME,
                dongle_process_running,
            )

            if not dongle_process_running():
                proceed = messagebox.askyesno(
                    "密码狗未检测到",
                    f"没有检测到 MapGIS 密码狗服务 {DONGLE_PROCESS_NAME} 在运行。\n"
                    "cli 转换很可能在最后一步失败（生成不了 WL/WT），前面的矢量化就白跑了。\n\n"
                    "请先插好加密狗并启动它，再点“开始运行”。\n"
                    "确认已插好、仍要继续吗？（点“否”取消；也可把转换模式改成 none/prepare 不需要密码狗）",
                    icon="warning",
                )
                if not proceed:
                    self._log(f"已取消：未检测到密码狗服务 {DONGLE_PROCESS_NAME}。")
                    return
                config = replace(config, skip_dongle_check=True)
                self._log(f"警告：未检测到密码狗 {DONGLE_PROCESS_NAME}，用户选择继续运行。")
        apply_settings_to_env(self._machine_settings_from_form(), override=True)
        self._run_stop_requested.clear()
        self._batch_stop_requested.clear()
        self.notebook.select(self.log_tab)
        self._set_busy(True)
        self._log("开始运行")
        self._log(f"输出目录: {config.output_root}")
        self._log(run_notice_for_state(state))
        if config.ai_provider != "none":
            self._log(f"AI: {config.ai_provider}, model={config.ai_model}, key={redact_api_key(config.ai_api_key)}")

        def worker() -> None:
            try:
                report = run_production_program(
                    config, should_stop=self._run_stop_requested.is_set
                )
                self._messages.put(completion_message_for_report(report))
            except RunCancelledError:
                self._messages.put(
                    (
                        "warning",
                        "已按请求安全停止。本图输出不完整，不能用于 MapGIS 编辑；"
                        "需要时勾选“覆盖已有输出”重跑这张图。",
                    )
                )
            except Exception as exc:  # pragma: no cover - UI runtime path.
                self._messages.put(("error", friendly_error_message(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _run_batch(self) -> None:
        try:
            state = self._form_state()
        except ValueError:
            messagebox.showerror("参数错误", "等待秒数必须是整数。")
            return
        if state.line_export_source == "repaired" and state.line_repair == "off":
            messagebox.showerror(
                "参数错误", "导出线层选 repaired 时，必须同时把线修复设为 conservative。"
            )
            return
        source_dir = self.batch_source_dir_var.get().strip()
        if not source_dir or not Path(source_dir).is_dir():
            messagebox.showerror("参数错误", "请选择存在的图源文件夹。")
            return
        limit_text = self.batch_limit_var.get().strip()
        limit: int | None = None
        if limit_text:
            try:
                limit = max(1, int(limit_text))
            except ValueError:
                messagebox.showerror("参数错误", "本次最多跑 N 张必须是整数或留空。")
                return
        rasters = discover_source_rasters(Path(source_dir))
        if not rasters:
            messagebox.showerror("参数错误", "图源文件夹里没有 .tif/.tiff 文件。")
            return

        config = build_batch_config_from_gui(
            state,
            source_rasters=tuple(rasters),
            retry_incomplete=self.batch_retry_incomplete_var.get(),
            limit=limit,
        )
        if config.conversion_mode == "cli":
            from geoscan.env_probe import (
                DONGLE_PROCESS_NAME,
                dongle_process_running,
            )

            if not dongle_process_running():
                proceed = messagebox.askyesno(
                    "密码狗未检测到",
                    f"没有检测到 MapGIS 密码狗服务 {DONGLE_PROCESS_NAME} 在运行。\n"
                    "整批 cli 转换很可能都在最后一步失败。\n\n"
                    "请先插好加密狗并启动它。确认已插好、仍要继续整批吗？"
                    "（点“否”取消；也可把转换模式改成 none/prepare 不需要密码狗）",
                    icon="warning",
                )
                if not proceed:
                    self._log(f"已取消批量：未检测到密码狗服务 {DONGLE_PROCESS_NAME}。")
                    return
                config = replace(config, skip_dongle_check=True)
                self._log(f"警告：未检测到密码狗 {DONGLE_PROCESS_NAME}，用户选择继续整批。")
        apply_settings_to_env(self._machine_settings_from_form(), override=True)
        for item in self.batch_tree.get_children():
            self.batch_tree.delete(item)
        self._batch_stop_requested.clear()
        self._run_stop_requested.clear()
        self.notebook.select(self.batch_tab)
        self._set_busy(True)
        self._log(f"开始批量：{len(rasters)} 张图，引擎={state.line_engine}，转换={state.conversion_mode}")
        if config.ai_api_key:
            self._log(f"AI key={redact_api_key(config.ai_api_key)}（仅本次会话，不落盘）")

        def worker() -> None:
            try:
                report = run_batch(
                    config,
                    progress=self._batch_rows.put,
                    should_stop=self._batch_stop_requested.is_set,
                )
                counts = report["counts"]
                summary = (
                    f"批量结束：完成 {counts['completed']}，失败 {counts['failed']}，"
                    f"跳过已完成 {counts['skipped_completed']}，"
                    f"待处理不完整 {counts['incomplete_needs_attention']}，"
                    f"未开始 {counts['not_started']}。\n"
                    f"状态表：{Path(config.project_root) / 'BATCH_OPS' / 'BATCH_STATUS.csv'}"
                )
                kind = "ok" if counts["failed"] == 0 else "warning"
                self._messages.put((kind, summary))
            except Exception as exc:  # pragma: no cover - UI runtime path.
                self._messages.put(("error", friendly_error_message(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _request_batch_stop(self) -> None:
        self._batch_stop_requested.set()
        self._log("已请求停止：完成当前图后不再开始新图。")

    def _drain_batch_rows(self) -> None:
        while True:
            try:
                row = self._batch_rows.get_nowait()
            except queue.Empty:
                break
            self.batch_tree.insert(
                "",
                "end",
                values=(
                    row.get("map_id", ""),
                    row.get("status", ""),
                    row.get("line_candidates", ""),
                    row.get("text_candidates", ""),
                    row.get("conversion_status", ""),
                    row.get("error", ""),
                ),
            )
            self._log(f"[{row.get('status', '')}] {row.get('map_id', '')}")
        self.after(300, self._drain_batch_rows)

    def _test_ai_connection(self) -> None:
        try:
            state = self._form_state()
        except ValueError:
            messagebox.showerror("参数错误", "等待秒数必须是整数。")
            return
        error = self._validate_ai_form(state, need_image=False)
        if error:
            messagebox.showerror("参数错误", error)
            return

        config = build_ai_config_from_gui(state, timeout_seconds=AI_CONNECTION_TIMEOUT_SECONDS)
        try:
            api_url = normalize_chat_completions_url(config.base_url)
        except Exception as exc:
            messagebox.showerror("参数错误", friendly_error_message(exc))
            return
        self.notebook.select(self.log_tab)
        self._set_busy(True)
        self._log("开始测试 AI 连接")
        self._log(f"AI: {config.provider}, model={config.model}, key={redact_api_key(config.api_key)}")
        self._log(f"请求地址: {api_url}")
        self._log(f"连接测试最长等待: {config.timeout_seconds} 秒")

        def worker() -> None:
            try:
                report = test_ai_connection(config)
                self._messages.put(("ok", f"AI 连接成功: {report['api_url']}"))
            except Exception as exc:  # pragma: no cover - UI runtime path.
                self._messages.put(("error", friendly_error_message(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _run_ai_visual_analysis(self) -> None:
        try:
            state = self._form_state()
        except ValueError:
            messagebox.showerror("参数错误", "等待秒数必须是整数。")
            return
        error = self._validate_ai_form(state, need_image=True)
        if error:
            messagebox.showerror("参数错误", error)
            return

        output_root = default_output_root_from_parent(state.output_parent, state.map_id)
        config = build_ai_config_from_gui(state)
        self.notebook.select(self.log_tab)
        self._set_busy(True)
        self._log("开始 AI 分析输入图")
        self._log(f"输出目录: {output_root}")
        self._log(f"AI: {config.provider}, model={config.model}, key={redact_api_key(config.api_key)}")

        def worker() -> None:
            try:
                report = analyze_map_image_with_ai(
                    config,
                    image_path=state.source_raster,
                    output_root=output_root,
                    map_id=state.map_id,
                )
                self._messages.put(("ok", f"AI 分析完成: {report['analysis_path']}"))
            except Exception as exc:  # pragma: no cover - UI runtime path.
                self._messages.put(("error", friendly_error_message(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _drain_messages(self) -> None:
        while True:
            try:
                kind, message = self._messages.get_nowait()
            except queue.Empty:
                break
            self._log(message)
            self._set_busy(False)
            if kind == "error":
                messagebox.showerror("运行失败", message)
            elif kind == "warning":
                messagebox.showwarning("运行未完全完成", message)
            else:
                messagebox.showinfo("运行完成", message)
        self.after(200, self._drain_messages)

    def _log(self, message: str) -> None:
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")

    def _open_output_folder(self) -> None:
        folder = self.output_preview_var.get().strip()
        if folder:
            Path(folder).mkdir(parents=True, exist_ok=True)
            os.startfile(folder)  # type: ignore[attr-defined]


def main() -> None:
    from geoscan.app_settings import bootstrap_settings

    settings_report = bootstrap_settings()
    app = ProductionGui()
    if settings_report.get("settings_file"):
        app._log(f"已加载机器设置: {settings_report['settings_file']}")
        if not settings_report.get("ok"):
            app._log(f"设置文件有问题，被忽略: {settings_report.get('error')}")
        for env_name, value in (settings_report.get("applied_env") or {}).items():
            app._log(f"  {env_name} = {value}")
    else:
        app._log(
            "未找到 mapgis_settings.json。第一次在本机使用时，请到“设置”页"
            "点“自动探测本机程序”并保存本机设置。"
        )
    app.mainloop()


if __name__ == "__main__":
    main()
