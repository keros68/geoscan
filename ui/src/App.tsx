import { useCallback, useEffect, useRef, useState } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { open as openDialog, ask } from "@tauri-apps/plugin-dialog";
import { engineCall, initEngine, onEngine, restartEngine } from "./engine";
import {
  BatchRow,
  HistoryRow,
  LogEntry,
  OverlayData,
  Preflight,
  PreviewData,
  RunForm,
  RunSummary,
  STAGE_ORDER,
  StageKey,
  StageState,
} from "./types";
import Toolbar from "./components/Toolbar";
import StageRail from "./components/StageRail";
import ProjectPanel from "./components/ProjectPanel";
import PreviewPane from "./components/PreviewPane";
import Inspector from "./components/Inspector";
import BottomDock, { DockTab } from "./components/BottomDock";
import StatusBar from "./components/StatusBar";
import SettingsDialog from "./components/SettingsDialog";

const DEFAULT_FORM: RunForm = {
  source_raster: "",
  map_id: "",
  project_root: "",
  output_parent: "",
  level_input: "off",
  enhanced_preview: "standard",
  conversion_mode: "cli",
  include_areas: false,
  reset_output: false,
  line_engine: "trace",
  line_connect: "standard",
  line_repair: "conservative",
  line_export_source: "repaired",
  line_bridge_gap_px: "",
  line_close_gap_px: "",
  export_dxf: true,
  qgis_files: true,
  wait_timeout_seconds: "300",
  target_line_file: "",
  target_text_file: "",
  target_area_file: "",
  text_candidates: "",
  ai_provider: "none",
  ai_base_url: "",
  ai_model: "",
  ai_api_key: "",
  ai_enhance: false,
};

const INITIAL_STAGES = Object.fromEntries(STAGE_ORDER.map((key) => [key, "pending"])) as Record<
  StageKey,
  StageState
>;

interface Toast {
  id: number;
  kind: "ok" | "warning" | "error" | "info";
  title: string;
  message: string;
}

let toastId = 1;

function now(): string {
  return new Date().toTimeString().slice(0, 8);
}

function normalizeOutputSelection(prev: RunForm, patch: Partial<RunForm>): RunForm {
  const next = { ...prev, ...patch };
  if (patch.conversion_mode && patch.conversion_mode !== "none" && patch.export_dxf !== false) {
    next.export_dxf = true;
  }
  if (patch.export_dxf === false && next.conversion_mode !== "none") {
    next.conversion_mode = "none";
  }
  return next;
}

function settingText(settings: Record<string, unknown>, key: string): string {
  return String(settings[key] ?? "").trim();
}

function settingBool(settings: Record<string, unknown>, key: string, fallback: boolean): boolean {
  const value = settingText(settings, key).toLowerCase();
  if (["1", "true", "yes", "on"].includes(value)) return true;
  if (["0", "false", "no", "off"].includes(value)) return false;
  return fallback;
}

function settingChoice<T extends string>(
  settings: Record<string, unknown>,
  key: string,
  allowed: readonly T[],
  fallback: T,
): T {
  const value = settingText(settings, key);
  return allowed.includes(value as T) ? (value as T) : fallback;
}

function runParamSettings(current: RunForm): Record<string, string> {
  return {
    level_input: current.level_input,
    enhanced_preview: current.enhanced_preview,
    conversion_mode: current.conversion_mode,
    include_areas: String(current.include_areas),
    export_dxf: String(current.export_dxf),
    qgis_files: String(current.qgis_files),
    wait_timeout_seconds: current.wait_timeout_seconds,
    target_line_file: current.target_line_file,
    target_text_file: current.target_text_file,
    target_area_file: current.target_area_file,
  };
}

function advancedParamSettings(current: RunForm): Record<string, string> {
  return {
    line_engine: current.line_engine,
    line_connect: current.line_connect,
    line_repair: current.line_repair,
    line_export_source: current.line_export_source,
    line_bridge_gap_px: current.line_bridge_gap_px,
    line_close_gap_px: current.line_close_gap_px,
    ai_enhance: String(current.ai_enhance),
  };
}

export default function App() {
  const [form, setForm] = useState<RunForm>(DEFAULT_FORM);
  const [outputRoot, setOutputRoot] = useState("");
  const [running, setRunning] = useState(false);
  const [batchRunning, setBatchRunning] = useState(false);
  const [statusLabel, setStatusLabel] = useState("就绪");
  const [engineState, setEngineState] = useState<"starting" | "ok" | "dead">("starting");
  const [stages, setStages] = useState<Record<StageKey, StageState>>(INITIAL_STAGES);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [stderrLines, setStderrLines] = useState<string[]>([]);
  const [preflightData, setPreflightData] = useState<Preflight | null>(null);
  const [summary, setSummary] = useState<RunSummary | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [selectedRoot, setSelectedRoot] = useState("");
  const [preview, setPreview] = useState<PreviewData | null>(null);
  // Path of whatever the preview currently shows: the chosen scan, or a run's
  // pixel-unit raster after completion / history selection.
  const [previewPath, setPreviewPath] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [overlay, setOverlay] = useState<OverlayData | null>(null);
  const [showLines, setShowLines] = useState(true);
  const [showTexts, setShowTexts] = useState(true);
  const [batchRows, setBatchRows] = useState<BatchRow[]>([]);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [inspectorVisible, setInspectorVisible] = useState(true);
  const [runParamsOpen, setRunParamsOpen] = useState(true);
  const [runParamsFocusTick, setRunParamsFocusTick] = useState(0);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  // Bumped by the menu entry so the inspector scrolls to + flashes the
  // advanced section even when it is already open.
  const [advancedFocusTick, setAdvancedFocusTick] = useState(0);
  const [batchOpen, setBatchOpen] = useState(false);
  const [batchFocusTick, setBatchFocusTick] = useState(0);
  const [aiOpen, setAiOpen] = useState(false);
  const [aiFocusTick, setAiFocusTick] = useState(0);
  const [dockTab, setDockTab] = useState<DockTab>("summary");
  const [appVersion, setAppVersion] = useState("");
  const [hasSavedKey, setHasSavedKey] = useState(false);
  const [aiBusy, setAiBusy] = useState(false);

  // Refs so long-lived engine-event subscriptions see current values.
  const formRef = useRef(form);
  formRef.current = form;
  const previewRef = useRef(preview);
  previewRef.current = preview;
  const outputRootRef = useRef(outputRoot);
  outputRootRef.current = outputRoot;
  const busyRef = useRef(false);
  busyRef.current = running || batchRunning;
  // Monotonic token so rapid history clicks can't interleave preview/overlay.
  const selectSeqRef = useRef(0);

  const pushLog = useCallback((level: string, message: string) => {
    setLogs((prev) => {
      const next = [...prev, { time: now(), level, message }];
      return next.length > 3000 ? next.slice(next.length - 3000) : next;
    });
  }, []);

  const pushToast = useCallback((kind: Toast["kind"], title: string, message: string) => {
    const id = toastId++;
    setToasts((prev) => [...prev, { id, kind, title, message }]);
    window.setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), kind === "error" ? 20000 : 9000);
  }, []);

  const refreshPreflight = useCallback(
    async (next?: Pick<RunForm, "conversion_mode" | "export_dxf">) => {
      try {
        const data = await engineCall<Preflight>("preflight", {
          conversion_mode: next?.conversion_mode ?? formRef.current.conversion_mode,
          export_dxf: next?.export_dxf ?? formRef.current.export_dxf,
        });
        setPreflightData(data);
      } catch (error) {
        pushLog("warn", `预检失败: ${error}`);
      }
    },
    [pushLog],
  );

  const refreshHistory = useCallback(async (parent: string) => {
    if (!parent) return;
    try {
      const data = await engineCall<{ rows: HistoryRow[] }>("list_history", { parent });
      setHistory(data.rows);
    } catch {
      /* history is best-effort */
    }
  }, []);

  const refreshSummary = useCallback(
    async (root: string, applyStages = true): Promise<RunSummary | null> => {
      if (!root) return null;
      try {
        const data = await engineCall<RunSummary>("run_summary", { output_root: root });
        setSummary(data);
        if (applyStages && data.stage_states) {
          setStages((prev) => ({ ...prev, ...(data.stage_states as Record<StageKey, StageState>) }));
        }
        return data;
      } catch {
        return null;
      }
    },
    [],
  );

  const loadOverlay = useCallback(async (root: string) => {
    const currentPreview = previewRef.current;
    if (!root || !currentPreview) return;
    try {
      const data = await engineCall<OverlayData>("load_candidates", {
        output_root: root,
        image_height: currentPreview.source_height,
      });
      setOverlay(data);
    } catch {
      /* overlay is best-effort */
    }
  }, []);

  // ---- engine lifecycle -------------------------------------------------
  const bootstrappedRef = useRef(false);

  const bootstrapEngine = useCallback(
    (version: string) => {
      // The engine's `hello` event can fire before the webview registers its
      // listeners (Python starts in parallel with the page load), so both the
      // hello handler AND a ping-retry loop funnel into this idempotent hook.
      if (bootstrappedRef.current) return;
      bootstrappedRef.current = true;
      setEngineState("ok");
      if (version) setAppVersion(version);
      pushLog("info", `引擎已连接 (geoscan v${version || "?"})`);
      void engineCall("get_settings")
        .then((data: any) => {
          const root = String(data.project_root ?? "");
          const stored = (data.settings ?? {}) as Record<string, unknown>;
          setHasSavedKey(Boolean(data.has_saved_key));
          setForm((prev) => {
            const next = normalizeOutputSelection(prev, {
              project_root: prev.project_root || root,
              output_parent: prev.output_parent || root,
              level_input: settingChoice(stored, "level_input", ["off", "auto", "force"] as const, prev.level_input),
              enhanced_preview: settingChoice(
                stored,
                "enhanced_preview",
                ["none", "light", "standard", "strong"] as const,
                prev.enhanced_preview,
              ),
              conversion_mode: settingChoice(
                stored,
                "conversion_mode",
                ["none", "prepare", "cli"] as const,
                prev.conversion_mode,
              ),
              include_areas: settingBool(stored, "include_areas", prev.include_areas),
              export_dxf: settingBool(stored, "export_dxf", prev.export_dxf),
              qgis_files: settingBool(stored, "qgis_files", prev.qgis_files),
              wait_timeout_seconds: settingText(stored, "wait_timeout_seconds") || prev.wait_timeout_seconds,
              target_line_file: settingText(stored, "target_line_file") || prev.target_line_file,
              target_text_file: settingText(stored, "target_text_file") || prev.target_text_file,
              target_area_file: settingText(stored, "target_area_file") || prev.target_area_file,
              line_engine: settingChoice(stored, "line_engine", ["hough", "trace"] as const, prev.line_engine),
              line_connect: settingChoice(
                stored,
                "line_connect",
                ["conservative", "standard", "aggressive"] as const,
                prev.line_connect,
              ),
              line_repair: settingChoice(stored, "line_repair", ["off", "conservative"] as const, prev.line_repair),
              line_export_source: settingChoice(
                stored,
                "line_export_source",
                ["raw", "repaired", "ai_enhanced"] as const,
                prev.line_export_source,
              ),
              line_bridge_gap_px: settingText(stored, "line_bridge_gap_px") || prev.line_bridge_gap_px,
              line_close_gap_px: settingText(stored, "line_close_gap_px") || prev.line_close_gap_px,
              ai_provider: settingChoice(
                stored,
                "ai_provider",
                ["none", "openai-compatible", "qwen", "custom"] as const,
                prev.ai_provider,
              ),
              ai_base_url: settingText(stored, "ai_base_url") || prev.ai_base_url,
              ai_model: settingText(stored, "ai_model") || prev.ai_model,
              ai_enhance: settingBool(stored, "ai_enhance", prev.ai_enhance),
            });
            formRef.current = next;
            void refreshPreflight({
              conversion_mode: next.conversion_mode,
              export_dxf: next.export_dxf,
            });
            return next;
          });
          void refreshHistory(root);
        })
        .catch(() => undefined);
      void refreshPreflight();
    },
    [pushLog, refreshHistory, refreshPreflight],
  );

  useEffect(() => {
    const offs = [
      onEngine("hello", (data) => {
        bootstrapEngine(String(data.version ?? ""));
      }),
      onEngine("log", (data) => pushLog(String(data.level ?? "info"), String(data.message ?? ""))),
      onEngine("status", (data) => {
        setStatusLabel(String(data.label ?? ""));
      }),
      onEngine("stage", (data) => {
        const stage = String(data.stage) as StageKey;
        if (STAGE_ORDER.includes(stage)) {
          setStages((prev) => ({ ...prev, [stage]: String(data.state) as StageState }));
        }
      }),
      onEngine("batch_row", (data) => {
        setBatchRows((prev) => [...prev, data as BatchRow]);
      }),
      onEngine("result", (data) => {
        const kind = String(data.kind ?? "");
        const scope = String(data.scope ?? "single");
        const message = String(data.message ?? "");
        if (scope === "batch") {
          setBatchRunning(false);
          pushToast(kind === "ok" ? "ok" : kind === "error" ? "error" : "warning", "批量结束", message);
          pushLog("info", message);
          return;
        }
        setRunning(false);
        const root = String(data.output_root ?? outputRootRef.current);
        pushLog(kind === "error" ? "error" : "info", message);
        if (kind === "ok") {
          pushToast("ok", "运行完成", message);
          const loadFolder = String(data.load_folder ?? "");
          if (loadFolder) void engineCall("open_path", { path: loadFolder }).catch(() => undefined);
        } else if (kind === "cancelled") {
          pushToast("warning", "已安全停止", message);
        } else if (kind === "error") {
          pushToast("error", "运行失败", message);
        } else {
          pushToast("warning", "运行未完全完成", message);
        }
        void refreshSummary(root).then(async (data) => {
          if (kind === "error") return;
          // Overlay-correct backdrop: candidates live in the frozen/leveled
          // raster's coordinate system, so swap the preview to the run's
          // pixel-unit TIFF before overlaying (leveling may rotate the image).
          const pixelRaster = String(data?.pixel_unit_raster ?? "");
          if (pixelRaster) {
            try {
              const fresh = await engineCall<PreviewData>("render_preview", { path: pixelRaster, max_dim: 1800 });
              previewRef.current = fresh;
              setPreview(fresh);
              setPreviewPath(pixelRaster);
            } catch {
              /* keep the original-scan preview; overlay still loads */
            }
          }
          void loadOverlay(root);
        });
        void refreshHistory(formRef.current.output_parent);
      }),
      onEngine("update_progress", (data) => {
        const done = Number(data.done ?? 0);
        const total = Number(data.total ?? 0);
        setStatusLabel(
          total > 0
            ? `正在下载更新… ${Math.floor((done * 100) / total)}%`
            : `正在下载更新… ${(done / 1048576).toFixed(1)} MB`,
        );
      }),
      onEngine("engine_stderr", (data) => {
        setStderrLines((prev) => [...prev.slice(-500), String(data.message ?? "")]);
      }),
      onEngine("engine_exit", () => {
        bootstrappedRef.current = false;
        setEngineState("dead");
        setRunning(false);
        setBatchRunning(false);
        pushToast("error", "引擎进程已退出", "Python 引擎意外退出。点状态栏“重启引擎”恢复；诊断页有 stderr 输出。");
      }),
    ];
    let alive = true;
    void initEngine()
      .then(async () => {
        // Ping until the engine answers: request/response is id-correlated so
        // it cannot be lost the way a startup event can. Halfway through the
        // attempts, respawn once in case the first engine died before we
        // could hear about it.
        for (let attempt = 0; attempt < 20 && alive && !bootstrappedRef.current; attempt++) {
          try {
            const info = await Promise.race([
              engineCall<{ version: string }>("ping"),
              new Promise<never>((_, reject) => window.setTimeout(() => reject(new Error("timeout")), 2500)),
            ]);
            bootstrapEngine(String(info.version ?? ""));
            return;
          } catch {
            if (attempt === 9) await restartEngine().catch(() => undefined);
            await new Promise((resolve) => window.setTimeout(resolve, 1000));
          }
        }
        if (alive && !bootstrappedRef.current) {
          setEngineState("dead");
          pushToast("error", "引擎无响应", "Python 引擎没有应答。请确认本机 python 可用（诊断页有 stderr 输出），然后点状态栏“重启引擎”。");
        }
      })
      .catch((error) => {
        setEngineState("dead");
        pushToast("error", "引擎启动失败", String(error));
      });
    return () => {
      alive = false;
      offs.forEach((off) => off());
    };
  }, [bootstrapEngine, pushLog, pushToast, refreshHistory, refreshPreflight, refreshSummary, loadOverlay]);

  // ---- actions ----------------------------------------------------------
  const chooseImage = useCallback(async () => {
    const picked = await openDialog({
      title: "选择输入图片",
      multiple: false,
      filters: [
        { name: "图片", extensions: ["tif", "tiff", "jpg", "jpeg", "png", "bmp"] },
        { name: "全部文件", extensions: ["*"] },
      ],
    });
    if (typeof picked !== "string" || !picked) return;
    setOverlay(null);
    setSummary(null);
    setStages(INITIAL_STAGES);
    setSelectedRoot("");
    try {
      const derived = await engineCall<{ map_id: string; output_parent: string; output_root: string }>(
        "derive_map_id",
        { path: picked },
      );
      setForm((prev) => ({
        ...prev,
        source_raster: picked,
        map_id: derived.map_id,
        project_root: derived.output_parent,
        output_parent: derived.output_parent,
      }));
      setOutputRoot(derived.output_root);
      pushLog("info", `已选择输入图片: ${picked}（Map ID: ${derived.map_id}）`);
      void refreshHistory(derived.output_parent);
      setPreviewLoading(true);
      setPreview(null);
      setPreviewPath("");
      try {
        const data = await engineCall<PreviewData>("render_preview", { path: picked, max_dim: 1800 });
        setPreview(data);
        setPreviewPath(picked);
      } finally {
        setPreviewLoading(false);
      }
    } catch (error) {
      pushToast("error", "读取图片失败", String(error));
      setPreviewLoading(false);
    }
  }, [pushLog, pushToast, refreshHistory]);

  const chooseOutputParent = useCallback(async () => {
    const picked = await openDialog({ title: "选择输出父文件夹", directory: true });
    if (typeof picked !== "string" || !picked) return;
    setForm((prev) => ({ ...prev, output_parent: picked }));
    const data = await engineCall<{ output_root: string }>("output_preview", {
      map_id: formRef.current.map_id,
      output_parent: picked,
    }).catch(() => ({ output_root: "" }));
    setOutputRoot(data.output_root);
    void refreshHistory(picked);
  }, [refreshHistory]);

  const setMapId = useCallback((mapId: string) => {
    setForm((prev) => ({ ...prev, map_id: mapId }));
    void engineCall<{ output_root: string }>("output_preview", {
      map_id: mapId,
      output_parent: formRef.current.output_parent,
    })
      .then((data) => setOutputRoot(data.output_root))
      .catch(() => undefined);
  }, []);

  const startRun = useCallback(async () => {
    const current = formRef.current;
    if (!current.source_raster) {
      pushToast("warning", "还没有选择输入图片", "点工具栏“打开影像”选择一张扫描图，Map ID 会自动识别。");
      return;
    }
    let skipDongle = false;
    if (current.conversion_mode === "cli") {
      try {
        const dongle = await engineCall<{ running: boolean; process: string }>("dongle_status");
        if (!dongle.running) {
          const proceed = await ask(
            `没有检测到 MapGIS 密码狗服务 ${dongle.process} 在运行。\ncli 转换很可能在最后一步失败（生成不了 WL/WT），前面的矢量化就白跑了。\n\n确认已插好、仍要继续吗？（也可把转换模式改成 none/prepare，不需要密码狗）`,
            { title: "密码狗未检测到", kind: "warning", okLabel: "仍要继续", cancelLabel: "取消" },
          );
          if (!proceed) return;
          skipDongle = true;
        }
      } catch {
        /* dongle probe failure is not fatal; the engine pre-flights again */
      }
    }
    // Reset BEFORE the ack: the engine's StageTracker starts emitting stage
    // events the moment the run is accepted, and they may arrive ahead of the
    // ack response — resetting afterwards would wipe them.
    setSelectedRoot("");
    setStages(INITIAL_STAGES);
    setSummary(null);
    setOverlay(null);
    setRunning(true);
    setDockTab("log");
    try {
      const payload = {
        ...current,
        skip_dongle_check: skipDongle,
        line_bridge_gap_px: current.line_bridge_gap_px.trim() || null,
        line_close_gap_px: current.line_close_gap_px.trim() || null,
        wait_timeout_seconds: parseInt(current.wait_timeout_seconds, 10) || 300,
      };
      const ack = await engineCall<{ accepted: boolean; output_root: string }>("run_single", { form: payload });
      setOutputRoot(ack.output_root);
    } catch (error) {
      setRunning(false);
      pushToast("error", "无法开始运行", String(error instanceof Error ? error.message : error));
    }
  }, [pushToast]);

  const stopRun = useCallback(() => {
    void engineCall("stop").catch(() => undefined);
  }, []);

  const openOutput = useCallback(() => {
    const target = summary?.load_folder || selectedRoot || outputRootRef.current || formRef.current.output_parent;
    if (!target) {
      pushToast("warning", "没有可打开的输出目录", "先选择输入图片或完成一次运行。");
      return;
    }
    void engineCall("open_path", { path: target }).catch((error) =>
      pushToast("warning", "打开失败", String(error instanceof Error ? error.message : error)),
    );
  }, [summary, selectedRoot, pushToast]);

  const selectHistory = useCallback(
    (row: HistoryRow) => {
      setSelectedRoot(row.output_root);
      const seq = ++selectSeqRef.current;
      // While a run is active the stage rail belongs to the live StageTracker;
      // browsing history must not overwrite it with an old run's states.
      void refreshSummary(row.output_root, !busyRef.current).then(async (data) => {
        if (!data || selectSeqRef.current !== seq) return;
        // Show the selected run: its pixel-unit raster as backdrop + overlay.
        const pixelRaster = String(data.pixel_unit_raster ?? "");
        if (!pixelRaster) return;
        try {
          const fresh = await engineCall<PreviewData>("render_preview", { path: pixelRaster, max_dim: 1800 });
          if (selectSeqRef.current !== seq) return;
          previewRef.current = fresh;
          setPreview(fresh);
          setPreviewPath(pixelRaster);
          setOverlay(null);
          const overlayData = await engineCall<OverlayData>("load_candidates", {
            output_root: row.output_root,
            image_height: fresh.source_height,
          });
          if (selectSeqRef.current === seq) setOverlay(overlayData);
        } catch {
          /* preview of an old run is best-effort */
        }
      });
    },
    [refreshSummary],
  );

  const startBatch = useCallback(
    async (sourceDir: string, limit: string, retryIncomplete: boolean) => {
      const current = formRef.current;
      let skipDongle = false;
      if (current.conversion_mode === "cli") {
        try {
          const dongle = await engineCall<{ running: boolean; process: string }>("dongle_status");
          if (!dongle.running) {
            const proceed = await ask(
              `没有检测到密码狗服务 ${dongle.process}，整批 cli 转换很可能都失败。仍要继续整批吗？`,
              { title: "密码狗未检测到", kind: "warning", okLabel: "仍要继续", cancelLabel: "取消" },
            );
            if (!proceed) return;
            skipDongle = true;
          }
        } catch {
          /* engine re-checks */
        }
      }
      // Reset BEFORE the ack: already-completed maps produce batch_row events
      // within milliseconds of acceptance, which can beat the ack response —
      // clearing afterwards would silently drop those rows.
      setBatchRows([]);
      setBatchRunning(true);
      try {
        const payload = {
          ...current,
          skip_dongle_check: skipDongle,
          line_bridge_gap_px: current.line_bridge_gap_px.trim() || null,
          line_close_gap_px: current.line_close_gap_px.trim() || null,
          wait_timeout_seconds: parseInt(current.wait_timeout_seconds, 10) || 300,
        };
        const ack = await engineCall<{ accepted: boolean; count: number }>("run_batch", {
          form: payload,
          source_dir: sourceDir,
          limit: limit.trim() ? parseInt(limit, 10) : null,
          retry_incomplete: retryIncomplete,
        });
        pushLog("info", `批量已开始：共 ${ack.count} 张图。`);
      } catch (error) {
        setBatchRunning(false);
        pushToast("error", "无法开始批量", String(error instanceof Error ? error.message : error));
      }
    },
    [pushLog, pushToast],
  );

  const testAi = useCallback(async () => {
    const current = formRef.current;
    setAiBusy(true);
    try {
      const result = await engineCall<{ api_url: string }>("test_ai_connection", {
        ai_provider: current.ai_provider,
        ai_base_url: current.ai_base_url,
        ai_model: current.ai_model,
        ai_api_key: current.ai_api_key,
      });
      pushToast("ok", "AI 连接成功", result.api_url);
    } catch (error) {
      pushToast("error", "AI 连接失败", String(error instanceof Error ? error.message : error));
    } finally {
      setAiBusy(false);
    }
  }, [pushToast]);

  const analyzeAi = useCallback(async () => {
    const current = formRef.current;
    if (!current.source_raster) {
      pushToast("warning", "还没有选择输入图片", "先在工具栏“打开影像”选择一张图，再做 AI 看图描述。");
      return;
    }
    setAiBusy(true);
    pushLog("info", "开始 AI 看图描述（仅诊断，不影响结果）…");
    try {
      const payload = {
        ...current,
        line_bridge_gap_px: current.line_bridge_gap_px.trim() || null,
        line_close_gap_px: current.line_close_gap_px.trim() || null,
        wait_timeout_seconds: parseInt(current.wait_timeout_seconds, 10) || 300,
      };
      const result = await engineCall<{ analysis_path: string }>("analyze_image", { form: payload });
      pushLog("info", `AI 分析完成: ${result.analysis_path}`);
      pushToast("ok", "AI 分析完成", `结果已写入:\n${result.analysis_path}`);
    } catch (error) {
      pushToast("error", "AI 分析失败", String(error instanceof Error ? error.message : error));
    } finally {
      setAiBusy(false);
    }
  }, [pushLog, pushToast]);

  const saveAiSettings = useCallback(
    async (saveKey: boolean) => {
      const current = formRef.current;
      setAiBusy(true);
      try {
        const args: Record<string, any> = {
          settings: {
            ai_provider: current.ai_provider,
            ai_base_url: current.ai_base_url,
            ai_model: current.ai_model,
            ai_enhance: current.ai_enhance ? "true" : "",
          },
        };
        // Only touch the stored key when the user asked to (or typed one).
        if (current.ai_api_key.trim() || saveKey !== hasSavedKey) {
          args.save_key = saveKey;
          args.ai_api_key = current.ai_api_key;
        }
        const result = await engineCall<{ key_saved: boolean }>("save_settings", args);
        if (args.save_key !== undefined) setHasSavedKey(Boolean(result.key_saved));
        if (result.key_saved) setForm((prev) => ({ ...prev, ai_api_key: "" }));
        const keyMessage = result.key_saved
          ? "API Key 已用本机账户级加密保存。"
          : args.save_key === undefined && hasSavedKey
            ? "连接设置已保存；继续使用本机已加密保存的 API Key。"
            : "设置已保存（Key 未保存，仅本次会话有效）。";
        pushToast("ok", "AI 设置已保存", keyMessage);
      } catch (error) {
        pushToast("error", "保存失败", String(error instanceof Error ? error.message : error));
      } finally {
        setAiBusy(false);
      }
    },
    [hasSavedKey, pushToast],
  );

  const saveRunParams = useCallback(async () => {
    try {
      await engineCall("save_settings", { settings: runParamSettings(formRef.current) });
      pushToast("ok", "运行参数已保存", "下次打开会自动带回这些默认值；“覆盖已有输出”不会保存为默认。");
    } catch (error) {
      pushToast("error", "保存失败", String(error instanceof Error ? error.message : error));
    }
  }, [pushToast]);

  const saveAdvancedParams = useCallback(async () => {
    try {
      await engineCall("save_settings", { settings: advancedParamSettings(formRef.current) });
      pushToast("ok", "高级参数已保存", "下次打开会自动带回这些默认值；文字候选覆盖路径不会保存为默认。");
    } catch (error) {
      pushToast("error", "保存失败", String(error instanceof Error ? error.message : error));
    }
  }, [pushToast]);

  const restart = useCallback(() => {
    setEngineState("starting");
    bootstrappedRef.current = false;
    void restartEngine()
      .then(async () => {
        pushLog("info", "已请求重启引擎…");
        for (let attempt = 0; attempt < 15 && !bootstrappedRef.current; attempt++) {
          try {
            const info = await Promise.race([
              engineCall<{ version: string }>("ping"),
              new Promise<never>((_, reject) => window.setTimeout(() => reject(new Error("timeout")), 2500)),
            ]);
            bootstrapEngine(String(info.version ?? ""));
            return;
          } catch {
            await new Promise((resolve) => window.setTimeout(resolve, 1000));
          }
        }
        if (!bootstrappedRef.current) setEngineState("dead");
      })
      .catch((error) => pushToast("error", "重启引擎失败", String(error)));
  }, [bootstrapEngine, pushLog, pushToast]);

  const checkUpdate = useCallback(async () => {
    pushLog("info", "正在检查更新…");
    try {
      const info = await engineCall<any>("check_update");
      if (!info.update_available) {
        pushToast("ok", "已是最新版本", `当前 v${info.current}`);
        return;
      }
      const size = Number(info.download_size ?? 0);
      const sizeText = size > 0 ? `约 ${(size / 1048576).toFixed(1)} MB` : "大小未知";
      if (info.kind === "engine") {
        const go = await ask(
          `发现新版本 v${info.latest}（当前 v${info.current}）。\n轻量引擎更新 ${sizeText}：下载校验后就地替换，然后自动重启引擎——界面不中断，设置不丢。\n\n现在更新？`,
          { title: "发现新版本", kind: "info", okLabel: "现在更新", cancelLabel: "以后再说" },
        );
        if (!go) return;
        setStatusLabel("正在下载更新…");
        const result = await engineCall<{ applied: string }>("apply_engine_update");
        pushLog("info", `引擎已更新至 v${result.applied}，正在重启引擎…`);
        restart();
        pushToast("ok", "更新完成", `引擎已更新至 v${result.applied}（界面无需重启）。`);
      } else {
        const go = await ask(
          `发现新版本 v${info.latest}（当前 v${info.current}）。\n本次需要完整安装包 ${sizeText}：下载校验后将启动安装程序并关闭本窗口，设置不丢。\n\n现在下载安装？`,
          { title: "发现新版本", kind: "info", okLabel: "下载并安装", cancelLabel: "以后再说" },
        );
        if (!go) return;
        setStatusLabel("正在下载安装包…");
        await engineCall("download_installer_update");
        pushToast("ok", "安装程序已启动", "本窗口即将关闭，按安装向导完成升级。");
        window.setTimeout(() => void getCurrentWindow().close(), 2500);
      }
    } catch (error) {
      setStatusLabel("就绪");
      pushToast("warning", "检查更新失败", String(error instanceof Error ? error.message : error));
    }
  }, [pushLog, pushToast, restart]);

  const updateForm = useCallback((patch: Partial<RunForm>) => {
    const next = normalizeOutputSelection(formRef.current, patch);
    formRef.current = next;
    setForm(next);
    if ("conversion_mode" in patch || "export_dxf" in patch) {
      void refreshPreflight({
        conversion_mode: next.conversion_mode,
        export_dxf: next.export_dxf,
      });
    }
  }, [refreshPreflight]);

  const copyDiagnostics = useCallback(() => {
    const lines: string[] = [];
    lines.push(`GeoScan Console 诊断 (engine v${appVersion || "?"})`);
    if (preflightData) {
      lines.push(`预检: ${preflightData.overall}`);
      preflightData.checks.forEach((check) => lines.push(`  [${check.state}] ${check.label}: ${check.detail}`));
    }
    if (summary?.has_report) {
      lines.push(`最近运行: ${summary.output_root}`);
      lines.push(`  线候选=${summary.line_candidates ?? "--"} 文字候选=${summary.text_candidates ?? "--"} 转换=${summary.conversion_status ?? "--"}`);
    }
    logs.slice(-60).forEach((entry) => lines.push(`[${entry.time}] ${entry.message}`));
    void navigator.clipboard.writeText(lines.join("\n")).then(
      () => pushToast("ok", "诊断已复制", "可直接粘贴给维护者。"),
      () => pushToast("warning", "复制失败", "剪贴板不可用。"),
    );
  }, [appVersion, preflightData, summary, logs, pushToast]);

  const showHelp = useCallback(() => {
    window.alert(
      [
        "GeoScan —— 扫描地质图半自动矢量化工具。",
        "所有候选保持 checked=no，AI 仅复核建议，最终以人工复核为准。",
        "",
        "作者：keros68",
        "项目仓库：https://github.com/keros68/geoscan",
        "邮箱：keros68@gmail.com",
      ].join("\n"),
    );
  }, []);

  const busy = running || batchRunning;

  return (
    <div className="app">
      <Toolbar
        form={form}
        busy={busy}
        engineReady={engineState === "ok"}
        inspectorVisible={inspectorVisible}
        onOpenImage={chooseImage}
        onChooseOutputParent={chooseOutputParent}
        onPreflight={() => void refreshPreflight()}
        onRunParams={() => {
          setInspectorVisible(true);
          setRunParamsOpen(true);
          setRunParamsFocusTick((tick) => tick + 1);
        }}
        onAdvanced={() => {
          setInspectorVisible(true);
          setAdvancedOpen(true);
          setAdvancedFocusTick((tick) => tick + 1);
        }}
        onBatch={() => {
          setInspectorVisible(true);
          setBatchOpen(true);
          setBatchFocusTick((tick) => tick + 1);
        }}
        onAiSettings={() => {
          setInspectorVisible(true);
          setAiOpen(true);
          setAiFocusTick((tick) => tick + 1);
        }}
        onToggleInspector={() => setInspectorVisible((visible) => !visible)}
        onSettings={() => setSettingsOpen(true)}
        onCheckUpdate={checkUpdate}
        onHelp={showHelp}
        onStart={startRun}
        onStop={stopRun}
        onUpdateForm={updateForm}
      />
      <StageRail stages={stages} />
      <div className="body">
        <ProjectPanel
          history={history}
          selectedRoot={selectedRoot || outputRoot}
          batchRows={batchRows}
          batchRunning={batchRunning}
          onSelect={selectHistory}
        />
        <PreviewPane
          sourcePath={previewPath || form.source_raster}
          preview={preview}
          loading={previewLoading}
          overlay={overlay}
          showLines={showLines}
          showTexts={showTexts}
          onToggleLines={() => setShowLines((v) => !v)}
          onToggleTexts={() => setShowTexts((v) => !v)}
          onOpenImage={chooseImage}
        />
        {inspectorVisible && (
          <Inspector
            form={form}
            outputRoot={selectedRoot || outputRoot}
            preflight={preflightData}
            summary={summary}
            busy={busy}
            runParamsOpen={runParamsOpen}
            runParamsFocusTick={runParamsFocusTick}
            advancedOpen={advancedOpen}
            advancedFocusTick={advancedFocusTick}
            batchOpen={batchOpen}
            batchFocusTick={batchFocusTick}
            aiOpen={aiOpen}
            aiFocusTick={aiFocusTick}
            batchRunning={batchRunning}
            hasSavedKey={hasSavedKey}
            aiBusy={aiBusy || busy}
            onToggleRunParams={setRunParamsOpen}
            onToggleAdvanced={setAdvancedOpen}
            onToggleBatch={setBatchOpen}
            onToggleAi={setAiOpen}
            onCollapse={() => setInspectorVisible(false)}
            onUpdateForm={updateForm}
            onSetMapId={setMapId}
            onChooseOutputParent={chooseOutputParent}
            onPreflight={() => void refreshPreflight()}
            onStartBatch={startBatch}
            onStopBatch={stopRun}
            onTestAi={() => void testAi()}
            onAnalyzeAi={() => void analyzeAi()}
            onSaveAiSettings={(saveKey) => void saveAiSettings(saveKey)}
            onSaveRunParams={() => void saveRunParams()}
            onSaveAdvancedParams={() => void saveAdvancedParams()}
          />
        )}
      </div>
      <BottomDock
        tab={dockTab}
        onTab={setDockTab}
        logs={logs}
        summary={summary}
        preflight={preflightData}
        stderrLines={stderrLines}
        onOpenOutput={openOutput}
        onCopyDiagnostics={copyDiagnostics}
        onOpenPath={(path) => void engineCall("open_path", { path }).catch(() => undefined)}
      />
      <StatusBar
        statusLabel={statusLabel}
        busy={busy}
        engineState={engineState}
        overlayActive={Boolean(overlay && (overlay.lines.length || overlay.texts.length))}
        version={appVersion}
        onRestartEngine={restart}
      />
      {settingsOpen && (
        <SettingsDialog
          onClose={() => setSettingsOpen(false)}
          onSaved={() => {
            void refreshPreflight();
            pushToast("ok", "设置已保存", "本机设置已保存并立即生效。");
          }}
          pushToast={pushToast}
        />
      )}
      <div className="toast-stack">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`toast ${toast.kind}`}
            onClick={() => setToasts((prev) => prev.filter((t) => t.id !== toast.id))}
          >
            <div className="toast-title">{toast.title}</div>
            {toast.message}
          </div>
        ))}
      </div>
    </div>
  );
}
