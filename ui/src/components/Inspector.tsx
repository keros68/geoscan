import { useEffect, useRef, useState } from "react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { Preflight, RunForm, RunSummary } from "../types";
import AiPanel from "./AiPanel";

interface Props {
  form: RunForm;
  outputRoot: string;
  preflight: Preflight | null;
  summary: RunSummary | null;
  busy: boolean;
  runParamsOpen: boolean;
  runParamsFocusTick: number;
  advancedOpen: boolean;
  advancedFocusTick: number;
  batchOpen: boolean;
  batchFocusTick: number;
  aiOpen: boolean;
  aiFocusTick: number;
  batchRunning: boolean;
  hasSavedKey: boolean;
  aiBusy: boolean;
  onToggleRunParams: (open: boolean) => void;
  onToggleAdvanced: (open: boolean) => void;
  onToggleBatch: (open: boolean) => void;
  onToggleAi: (open: boolean) => void;
  onCollapse: () => void;
  onUpdateForm: (patch: Partial<RunForm>) => void;
  onSetMapId: (mapId: string) => void;
  onChooseOutputParent: () => void;
  onPreflight: () => void;
  onStartBatch: (sourceDir: string, limit: string, retryIncomplete: boolean) => void;
  onStopBatch: () => void;
  onTestAi: () => void;
  onAnalyzeAi: () => void;
  onSaveAiSettings: (saveKey: boolean) => void;
  onSaveRunParams: () => void;
  onSaveAdvancedParams: () => void;
}

const READINESS_TEXT = {
  ready: "环境就绪，可以运行",
  needs_attention: "可以运行，但有需要注意的项",
  blocked: "缺少必需工具，请先修复",
} as const;

export default function Inspector(props: Props) {
  const { form, preflight } = props;
  const [showChecks, setShowChecks] = useState(false);
  const runParamsRef = useRef<HTMLDivElement>(null);
  const [runParamsFlash, setRunParamsFlash] = useState(false);
  const advancedRef = useRef<HTMLDivElement>(null);
  const [advancedFlash, setAdvancedFlash] = useState(false);
  const batchRef = useRef<HTMLDivElement>(null);
  const [batchFlash, setBatchFlash] = useState(false);
  const aiRef = useRef<HTMLDivElement>(null);
  const [aiFlash, setAiFlash] = useState(false);
  const [batchDir, setBatchDir] = useState("");
  const [batchLimit, setBatchLimit] = useState("");
  const [retryIncomplete, setRetryIncomplete] = useState(false);

  // A blocked machine must not hide WHY — expand the checklist automatically.
  useEffect(() => {
    if (preflight?.overall === "blocked") setShowChecks(true);
  }, [preflight?.overall]);

  // Opening via the menu is otherwise easy to miss in the side column:
  // scroll the section into view and flash it.
  useEffect(() => {
    if (props.runParamsFocusTick === 0) return;
    runParamsRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    setRunParamsFlash(true);
    const timer = window.setTimeout(() => setRunParamsFlash(false), 1400);
    return () => window.clearTimeout(timer);
  }, [props.runParamsFocusTick]);

  useEffect(() => {
    if (props.advancedFocusTick === 0) return;
    advancedRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    setAdvancedFlash(true);
    const timer = window.setTimeout(() => setAdvancedFlash(false), 1400);
    return () => window.clearTimeout(timer);
  }, [props.advancedFocusTick]);

  useEffect(() => {
    if (props.batchFocusTick === 0) return;
    batchRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    setBatchFlash(true);
    const timer = window.setTimeout(() => setBatchFlash(false), 1400);
    return () => window.clearTimeout(timer);
  }, [props.batchFocusTick]);

  useEffect(() => {
    if (props.aiFocusTick === 0) return;
    aiRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    setAiFlash(true);
    const timer = window.setTimeout(() => setAiFlash(false), 1400);
    return () => window.clearTimeout(timer);
  }, [props.aiFocusTick]);

  return (
    <div className="panel inspector">
      <div className="inspector-header">
        <span>控制栏</span>
        <button className="link-btn" onClick={props.onCollapse}>
          收起
        </button>
      </div>
      {/* 预检：默认只占一行结论，详情按需展开；完整清单也在底部“诊断”页 */}
      <div className="insp-section">
        {preflight ? (
          <>
            <div className={`readiness ${preflight.overall}`}>
              <span
                className={`dot ${preflight.overall === "ready" ? "ok" : preflight.overall === "blocked" ? "missing" : "warn"}`}
              />
              <span style={{ flex: 1 }}>{READINESS_TEXT[preflight.overall]}</span>
              <button className="link-btn" onClick={() => setShowChecks((value) => !value)}>
                {showChecks ? "收起" : "详情"}
              </button>
              <button className="link-btn" onClick={props.onPreflight}>
                重检
              </button>
            </div>
            {showChecks &&
              preflight.checks.map((check) => (
                <div key={check.key} className="check-row" title={check.detail}>
                  <span className={`dot ${check.state}`} />
                  <span>
                    {check.label}
                    <span className="detail">{check.detail}</span>
                  </span>
                </div>
              ))}
          </>
        ) : (
          <div className="readiness needs_attention">
            <span className="dot warn" />
            <span style={{ flex: 1 }}>正在检测本机转换环境…</span>
            <button className="link-btn" onClick={props.onPreflight}>
              重检
            </button>
          </div>
        )}
      </div>

      <div className={`insp-section ${runParamsFlash ? "adv-flash" : ""}`} ref={runParamsRef}>
        <h3>
          <button className="section-toggle" onClick={() => props.onToggleRunParams(!props.runParamsOpen)}>
            {props.runParamsOpen ? "▾" : "▸"} 运行参数
          </button>
          <span className="sub" style={{ fontWeight: 400, color: "var(--ink-muted)" }}>
            常用输出设置
          </span>
        </h3>
        {props.runParamsOpen && (
          <>
            <div className="field-grid">
              <label htmlFor="insp-mapid">Map ID</label>
              <input
                id="insp-mapid"
                type="text"
                value={form.map_id}
                placeholder="选择图片后自动识别"
                disabled={props.busy}
                onChange={(event) => props.onSetMapId(event.target.value)}
              />
              <label htmlFor="insp-level">输入调平</label>
              <select
                id="insp-level"
                value={form.level_input}
                disabled={props.busy}
                onChange={(event) =>
                  props.onUpdateForm({ level_input: event.target.value as RunForm["level_input"] })
                }
              >
                <option value="off">off（默认，不调平）</option>
                <option value="auto">auto（原始扫描图纠偏）</option>
                <option value="force">force（强制调平）</option>
              </select>
              <label htmlFor="insp-preview">增强底图</label>
              <select
                id="insp-preview"
                value={form.enhanced_preview}
                disabled={props.busy}
                onChange={(event) =>
                  props.onUpdateForm({ enhanced_preview: event.target.value as RunForm["enhanced_preview"] })
                }
              >
                <option value="none">none（不生成）</option>
                <option value="light">light</option>
                <option value="standard">standard（推荐）</option>
                <option value="strong">strong</option>
              </select>
            </div>

            <div className="adv-subhead">转换与导出</div>
            <div className="field-grid">
              <label htmlFor="insp-conv">转换模式</label>
              <select
                id="insp-conv"
                value={form.conversion_mode}
                disabled={props.busy}
                onChange={(event) =>
                  props.onUpdateForm({ conversion_mode: event.target.value as RunForm["conversion_mode"] })
                }
              >
                <option value="cli">cli（自动转出 WT/WL，推荐）</option>
                <option value="prepare">prepare（只准备批次）</option>
                <option value="none">none（不出 MapGIS 文件；只出 DXF/QGIS，跳过密码狗检测）</option>
              </select>
              <label htmlFor="run-timeout">转换等待秒</label>
              <input
                id="run-timeout"
                type="text"
                value={form.wait_timeout_seconds}
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ wait_timeout_seconds: event.target.value })}
              />
              <label htmlFor="run-wl">WL 文件名</label>
              <input
                id="run-wl"
                type="text"
                value={form.target_line_file}
                placeholder="空=按 Map ID 命名"
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ target_line_file: event.target.value })}
              />
              <label htmlFor="run-wt">WT 文件名</label>
              <input
                id="run-wt"
                type="text"
                value={form.target_text_file}
                placeholder="空=按 Map ID 命名"
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ target_text_file: event.target.value })}
              />
              <label htmlFor="run-wp">WP 文件名</label>
              <input
                id="run-wp"
                type="text"
                value={form.target_area_file}
                placeholder="空=按 Map ID 命名"
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ target_area_file: event.target.value })}
              />
            </div>
            <div className="checkbox-row">
              <input
                id="run-dxf"
                type="checkbox"
                checked={form.export_dxf}
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ export_dxf: event.target.checked })}
              />
              <label htmlFor="run-dxf">导出 DXF/SHP 交换文件（MapGIS 转换依赖）</label>
            </div>
            <div className="checkbox-row">
              <input
                id="run-qgis"
                type="checkbox"
                checked={form.qgis_files}
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ qgis_files: event.target.checked })}
              />
              <label htmlFor="run-qgis">输出 QGIS 对齐文件（.tfw + 毫米 GeoJSON）</label>
            </div>

            <div className="adv-subhead">运行选项</div>
            <div className="checkbox-row">
              <input
                id="insp-areas"
                type="checkbox"
                checked={form.include_areas}
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ include_areas: event.target.checked })}
              />
              <label htmlFor="insp-areas">造区候选（额外提取区/面并导出 WP）</label>
            </div>
            <div className="checkbox-row">
              <input
                id="insp-reset"
                type="checkbox"
                checked={form.reset_output}
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ reset_output: event.target.checked })}
              />
              <label htmlFor="insp-reset">覆盖已有输出（重跑同一张图；先自动备份）</label>
            </div>
            <div className="hint" title={props.outputRoot}>
              结果将保存在：{props.outputRoot || "（选择图片后自动生成）"}
            </div>
            <div className="insp-actions" style={{ padding: "10px 0 0" }}>
              <button className="btn primary" disabled={props.busy} onClick={props.onSaveRunParams}>
                保存运行参数
              </button>
            </div>
          </>
        )}
      </div>

      {/* 高级参数：直接在页面上可见可调，默认折叠 */}
      <div className={`insp-section ${advancedFlash ? "adv-flash" : ""}`} ref={advancedRef}>
        <h3>
          <button className="section-toggle" onClick={() => props.onToggleAdvanced(!props.advancedOpen)}>
            {props.advancedOpen ? "▾" : "▸"} 高级参数
          </button>
          <span className="sub" style={{ fontWeight: 400, color: "var(--ink-muted)" }}>
            默认即推荐配置
          </span>
        </h3>
        {props.advancedOpen && (
          <>
            <div className="adv-subhead">线提取</div>
            <div className="field-grid">
              <label htmlFor="adv-engine">线提取引擎</label>
              <select
                id="adv-engine"
                value={form.line_engine}
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ line_engine: event.target.value as RunForm["line_engine"] })}
              >
                <option value="trace">trace（推荐）</option>
                <option value="hough">hough（快速直线）</option>
              </select>
              <label htmlFor="adv-repair">线修复</label>
              <select
                id="adv-repair"
                value={form.line_repair}
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ line_repair: event.target.value as RunForm["line_repair"] })}
              >
                <option value="conservative">conservative（推荐）</option>
                <option value="off">off</option>
              </select>
              <label htmlFor="adv-export">导出线层</label>
              <select
                id="adv-export"
                value={form.line_export_source}
                disabled={props.busy}
                onChange={(event) =>
                  props.onUpdateForm({ line_export_source: event.target.value as RunForm["line_export_source"] })
                }
              >
                <option value="repaired">repaired（推荐）</option>
                <option value="raw">raw</option>
                <option value="ai_enhanced">ai_enhanced</option>
              </select>
              <label htmlFor="adv-connect">线条连接</label>
              <select
                id="adv-connect"
                value={form.line_connect}
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ line_connect: event.target.value as RunForm["line_connect"] })}
              >
                <option value="standard">standard（默认）</option>
                <option value="aggressive">aggressive（更积极）</option>
                <option value="conservative">conservative（保守）</option>
              </select>
              <label htmlFor="adv-bridge">桥接断口 px</label>
              <input
                id="adv-bridge"
                type="text"
                value={form.line_bridge_gap_px}
                placeholder="留空=按档位"
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ line_bridge_gap_px: event.target.value })}
              />
              <div className="field-hint">
                断线自动接回的最大距离。留空=按连接档位（标准60/积极100）；填 0=完全不补；调大补得更多——只在图上有墨迹证据时才连，不会凭空画线。
              </div>
              <label htmlFor="adv-close">闭合缺口 px</label>
              <input
                id="adv-close"
                type="text"
                value={form.line_close_gap_px}
                placeholder="留空=按档位"
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ line_close_gap_px: event.target.value })}
              />
              <div className="field-hint">
                图例框/圈闭只差一个小口时，自动补上收口段的最大缺口。留空=按档位（标准12/积极20）；填 0=不收口；图例框仍有缺口就调大。
              </div>
            </div>
            <div className="checkbox-row">
              <input
                id="adv-ai-enhance"
                type="checkbox"
                checked={form.ai_enhance}
                disabled={props.busy}
                onChange={(event) => props.onUpdateForm({ ai_enhance: event.target.checked })}
              />
              <label htmlFor="adv-ai-enhance">启用 AI 增强（断线桥接提名 + OCR 纠错建议）</label>
            </div>
              <div className="field-hint" style={{ marginTop: -2 }}>
              需线修复开启，并先在“AI设置”里配好连接；AI 只提名、栅格证据验证，结果全部 checked=no。
            </div>
            <div className="adv-subhead">覆盖输入（专家）</div>
            <div className="field-grid">
              <label>文字候选</label>
              <div style={{ display: "flex", gap: 4 }}>
                <input
                  type="text"
                  style={{ flex: 1, minWidth: 0 }}
                  value={form.text_candidates}
                  placeholder="空=自动生成（正常用法）"
                  disabled={props.busy}
                  onChange={(event) => props.onUpdateForm({ text_candidates: event.target.value })}
                />
                <button
                  className="btn"
                  style={{ minHeight: 27, padding: "2px 8px" }}
                  disabled={props.busy}
                  onClick={() =>
                    void openDialog({
                      title: "选择文字候选 GeoJSON",
                      filters: [{ name: "GeoJSON", extensions: ["geojson", "json"] }],
                    }).then((picked) => {
                      if (typeof picked === "string" && picked) props.onUpdateForm({ text_candidates: picked });
                    })
                  }
                >
                  选择
                </button>
              </div>
            </div>
            <div className="hint">
              文字候选仅在使用人工整理层时选择，不要选旧运行的输出（新鲜运行规则）。
            </div>
            <div className="insp-actions" style={{ padding: "10px 0 0" }}>
              <button className="btn primary" disabled={props.busy} onClick={props.onSaveAdvancedParams}>
                保存高级参数
              </button>
            </div>
          </>
        )}
      </div>

      <div className={`insp-section ${aiFlash ? "adv-flash" : ""}`} ref={aiRef}>
        <h3>
          <button className="section-toggle" onClick={() => props.onToggleAi(!props.aiOpen)}>
            {props.aiOpen ? "▾" : "▸"} AI设置
          </button>
          <span className="sub" style={{ fontWeight: 400, color: "var(--ink-muted)" }}>
            可选增强
          </span>
        </h3>
        {props.aiOpen && (
          <AiPanel
            form={form}
            hasSavedKey={props.hasSavedKey}
            busy={props.aiBusy}
            onUpdateForm={props.onUpdateForm}
            onTest={props.onTestAi}
            onAnalyze={props.onAnalyzeAi}
            onSave={props.onSaveAiSettings}
          />
        )}
      </div>

      <div className={`insp-section ${batchFlash ? "adv-flash" : ""}`} ref={batchRef}>
        <h3>
          <button className="section-toggle" onClick={() => props.onToggleBatch(!props.batchOpen)}>
            {props.batchOpen ? "▾" : "▸"} 批量
          </button>
          <span className="sub" style={{ fontWeight: 400, color: "var(--ink-muted)" }}>
            使用当前运行参数
          </span>
        </h3>
        {props.batchOpen && (
          <>
            <div className="field-grid">
              <label>图源文件夹</label>
              <div style={{ display: "flex", gap: 4 }}>
                <input
                  type="text"
                  style={{ flex: 1, minWidth: 0 }}
                  value={batchDir}
                  placeholder="选择包含源 TIFF 的文件夹"
                  disabled={props.batchRunning}
                  onChange={(event) => setBatchDir(event.target.value)}
                />
                <button
                  className="btn"
                  style={{ minHeight: 27, padding: "2px 8px" }}
                  disabled={props.batchRunning}
                  onClick={() =>
                    void openDialog({ title: "选择包含源 TIFF 的文件夹", directory: true }).then((picked) => {
                      if (typeof picked === "string" && picked) setBatchDir(picked);
                    })
                  }
                >
                  选择
                </button>
              </div>
              <label htmlFor="batch-limit">数量上限</label>
              <input
                id="batch-limit"
                type="text"
                value={batchLimit}
                placeholder="全部"
                title="留空=整个文件夹全部处理；填数字=本次最多处理 N 张"
                disabled={props.batchRunning}
                onChange={(event) => setBatchLimit(event.target.value)}
              />
            </div>
            <div className="checkbox-row">
              <input
                id="batch-retry"
                type="checkbox"
                checked={retryIncomplete}
                disabled={props.batchRunning}
                onChange={(event) => setRetryIncomplete(event.target.checked)}
              />
              <label htmlFor="batch-retry">重跑不完整的图</label>
            </div>
            <div className="insp-actions" style={{ padding: "8px 0 0" }}>
              <button
                className="btn primary"
                disabled={props.batchRunning || !batchDir}
                onClick={() => props.onStartBatch(batchDir, batchLimit, retryIncomplete)}
              >
                开始批量
              </button>
              <button className="btn danger" disabled={!props.batchRunning} onClick={props.onStopBatch}>
                完成当前图后停止
              </button>
            </div>
            <div className="hint">批量一次处理一张图；已完成的图自动跳过，可断点续跑。</div>
          </>
        )}
      </div>
    </div>
  );
}
