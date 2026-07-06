import { useEffect, useRef, useState } from "react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";
import { BatchRow, LogEntry, Preflight, RunForm, RunSummary } from "../types";
import AiPanel from "./AiPanel";

export type DockTab = "log" | "files" | "batch" | "ai" | "diag";

interface Props {
  tab: DockTab;
  onTab: (tab: DockTab) => void;
  logs: LogEntry[];
  summary: RunSummary | null;
  batchRows: BatchRow[];
  batchRunning: boolean;
  preflight: Preflight | null;
  stderrLines: string[];
  form: RunForm;
  hasSavedKey: boolean;
  aiBusy: boolean;
  onUpdateForm: (patch: Partial<RunForm>) => void;
  onTestAi: () => void;
  onAnalyzeAi: () => void;
  onSaveAiSettings: (saveKey: boolean) => void;
  onStartBatch: (sourceDir: string, limit: string, retryIncomplete: boolean) => void;
  onStopBatch: () => void;
  onOpenPath: (path: string) => void;
}

export default function BottomDock(props: Props) {
  const logRef = useRef<HTMLDivElement>(null);
  const [batchDir, setBatchDir] = useState("");
  const [batchLimit, setBatchLimit] = useState("");
  const [retryIncomplete, setRetryIncomplete] = useState(false);

  useEffect(() => {
    if (props.tab === "log" && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [props.logs, props.tab]);

  const tabs: { key: DockTab; label: string }[] = [
    { key: "log", label: "日志" },
    { key: "files", label: "输出文件" },
    { key: "batch", label: "批量" },
    { key: "ai", label: "AI（可选）" },
    { key: "diag", label: "诊断" },
  ];

  return (
    <div className="bottom-dock">
      <div className="dock-tabs">
        {tabs.map((tab) => (
          <button key={tab.key} className={props.tab === tab.key ? "active" : ""} onClick={() => props.onTab(tab.key)}>
            {tab.label}
            {tab.key === "batch" && props.batchRows.length > 0 ? ` (${props.batchRows.length})` : ""}
          </button>
        ))}
      </div>
      <div className="dock-body" ref={props.tab === "log" ? logRef : undefined}>
        {props.tab === "log" && (
          <div className="log-view">
            {props.logs.length === 0 ? (
              <span style={{ color: "var(--ink-muted)" }}>运行日志会出现在这里。</span>
            ) : (
              props.logs.map((entry, index) => (
                <div key={index} className={`log-line ${entry.level}`}>
                  <span className="t">[{entry.time}] </span>
                  {entry.message}
                </div>
              ))
            )}
          </div>
        )}

        {props.tab === "files" &&
          (props.summary?.has_report && props.summary.ready_files && props.summary.ready_files.length > 0 ? (
            <table className="file-table">
              <thead>
                <tr>
                  <th>MAPGIS_LOAD_READY 文件</th>
                  <th style={{ width: 120 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td className="mono" title={props.summary.load_folder}>
                    {props.summary.load_folder}
                  </td>
                  <td>
                    <button className="btn" onClick={() => props.onOpenPath(props.summary!.load_folder!)}>
                      打开文件夹
                    </button>
                  </td>
                </tr>
                {props.summary.ready_files.map((file) => (
                  <tr key={file}>
                    <td className="mono">{file}</td>
                    <td />
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="dock-empty">
              运行成功后，交付包（像素单位 TIFF + WL/WT + DXF）会列在这里。
              {props.summary?.has_report ? " 本次运行还没有生成完整交付包。" : ""}
            </div>
          ))}

        {props.tab === "batch" && (
          <>
            <div className="batch-form">
              <button
                className="btn"
                disabled={props.batchRunning}
                onClick={() =>
                  void openDialog({ title: "选择包含源 TIFF 的文件夹", directory: true }).then((picked) => {
                    if (typeof picked === "string" && picked) setBatchDir(picked);
                  })
                }
              >
                图源文件夹…
              </button>
              <span style={{ fontFamily: "var(--mono)", fontSize: 12, color: "var(--ink-muted)" }}>
                {batchDir || "未选择"}
              </span>
              <label style={{ fontSize: 12.5 }}>
                数量上限
                <input
                  type="text"
                  value={batchLimit}
                  onChange={(event) => setBatchLimit(event.target.value)}
                  style={{ width: 64, marginLeft: 4 }}
                  placeholder="全部"
                  title="留空=整个文件夹全部处理；填数字=本次最多处理 N 张"
                  disabled={props.batchRunning}
                />
              </label>
              <label className="checkbox-row" style={{ padding: 0 }}>
                <input
                  type="checkbox"
                  checked={retryIncomplete}
                  disabled={props.batchRunning}
                  onChange={(event) => setRetryIncomplete(event.target.checked)}
                />
                重跑不完整的图
              </label>
              <span style={{ flex: 1 }} />
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
            {props.batchRows.length === 0 ? (
              <div className="dock-empty">
                批量使用当前运行参数；一次一张图；已完成的图自动跳过（可断点续跑）。
              </div>
            ) : (
              <table className="batch-table">
                <thead>
                  <tr>
                    <th>Map ID</th>
                    <th>状态</th>
                    <th>线候选</th>
                    <th>文字候选</th>
                    <th>转换</th>
                    <th>错误</th>
                  </tr>
                </thead>
                <tbody>
                  {props.batchRows.map((row, index) => (
                    <tr key={index}>
                      <td className="mono">{row.map_id ?? ""}</td>
                      <td>{row.status ?? ""}</td>
                      <td className="mono">{row.line_candidates ?? ""}</td>
                      <td className="mono">{row.text_candidates ?? ""}</td>
                      <td>{row.conversion_status ?? ""}</td>
                      <td>{row.error ?? ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}

        {props.tab === "ai" && (
          <AiPanel
            form={props.form}
            hasSavedKey={props.hasSavedKey}
            busy={props.aiBusy}
            onUpdateForm={props.onUpdateForm}
            onTest={props.onTestAi}
            onAnalyze={props.onAnalyzeAi}
            onSave={props.onSaveAiSettings}
          />
        )}

        {props.tab === "diag" && (
          <div className="log-view">
            {props.preflight &&
              props.preflight.checks.map((check) => (
                <div key={check.key} className={`log-line ${check.state === "missing" ? "error" : check.state === "warn" ? "warn" : ""}`}>
                  [{check.state}] {check.label}: {check.detail}
                </div>
              ))}
            {props.stderrLines.length > 0 && (
              <>
                <div className="log-line warn">—— 引擎 stderr ——</div>
                {props.stderrLines.map((line, index) => (
                  <div key={index} className="log-line warn">
                    {line}
                  </div>
                ))}
              </>
            )}
            {!props.preflight && props.stderrLines.length === 0 && (
              <span style={{ color: "var(--ink-muted)" }}>暂无诊断信息。</span>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
