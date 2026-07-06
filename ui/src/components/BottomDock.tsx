import { useEffect, useRef } from "react";
import { LogEntry, Preflight, RunSummary } from "../types";

export type DockTab = "summary" | "log" | "files" | "diag";

interface Props {
  tab: DockTab;
  onTab: (tab: DockTab) => void;
  logs: LogEntry[];
  summary: RunSummary | null;
  preflight: Preflight | null;
  stderrLines: string[];
  onOpenOutput: () => void;
  onCopyDiagnostics: () => void;
  onOpenPath: (path: string) => void;
}

export default function BottomDock(props: Props) {
  const logRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (props.tab === "log" && logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [props.logs, props.tab]);

  const tabs: { key: DockTab; label: string }[] = [
    { key: "summary", label: "摘要" },
    { key: "log", label: "日志" },
    { key: "files", label: "输出文件" },
    { key: "diag", label: "诊断" },
  ];

  return (
    <div className="bottom-dock">
      <div className="dock-tabs">
        {tabs.map((tab) => (
          <button key={tab.key} className={props.tab === tab.key ? "active" : ""} onClick={() => props.onTab(tab.key)}>
            {tab.label}
          </button>
        ))}
      </div>
      <div className="dock-body" ref={props.tab === "log" ? logRef : undefined}>
        {props.tab === "summary" && (
          <div className="summary-panel">
            <div className="summary-stats">
              <div>
                <span>line_candidates</span>
                <b>{props.summary?.line_candidates ?? "--"}</b>
              </div>
              <div>
                <span>text_candidates</span>
                <b>{props.summary?.text_candidates ?? "--"}</b>
              </div>
              <div>
                <span>checked=yes</span>
                <b
                  style={(props.summary?.checked_yes ?? 0) > 0 ? { color: "var(--red)" } : undefined}
                  title={(props.summary?.checked_yes ?? 0) > 0 ? "违规：候选不允许自动标记 checked=yes，请检查外部候选文件" : undefined}
                >
                  {props.summary?.has_report ? props.summary.checked_yes ?? 0 : "--"}
                </b>
              </div>
              <div>
                <span>ready_files</span>
                <b>{props.summary?.ready_files ? props.summary.ready_files.length : "--"}</b>
              </div>
            </div>
            <div className="summary-actions">
              <button className="btn" onClick={props.onOpenOutput}>
                打开输出目录
              </button>
              <button className="btn" onClick={props.onCopyDiagnostics}>
                复制诊断
              </button>
            </div>
            {(props.summary?.checked_yes ?? 0) > 0 && (
              <div className="dock-empty" style={{ color: "var(--red)" }}>
                检测到 {props.summary!.checked_yes} 个候选被标记为已确认——这违反产品规则，请检查是否误用了外部候选文件。
              </div>
            )}
            {props.summary?.conversion_status && (
              <div className="dock-empty">转换状态：{props.summary.conversion_status}</div>
            )}
          </div>
        )}

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
              运行成功后，交付包（源 dpi TIFF + .tfw + GeoJSON/DXF + 可选 WL/WT）会列在这里。
              {props.summary?.has_report ? " 本次运行还没有生成完整交付包。" : ""}
            </div>
          ))}

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
