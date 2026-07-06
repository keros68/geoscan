import { BatchRow, HistoryRow } from "../types";

interface Props {
  history: HistoryRow[];
  selectedRoot: string;
  batchRows: BatchRow[];
  batchRunning: boolean;
  onSelect: (row: HistoryRow) => void;
}

function kindDot(kind?: string): string {
  if (kind === "ok") return "ok";
  if (kind === "warning") return "warn";
  if (kind === "error") return "missing";
  return "pending";
}

export default function ProjectPanel(props: Props) {
  return (
    <div className="panel project-panel">
      <div className="panel-title">
        项目
        <span className="sub">{props.history.length ? `${props.history.length} 次运行` : ""}</span>
      </div>
      <div className="tree">
        {props.history.length === 0 ? (
          <div className="tree-empty">
            当前输出目录下还没有运行记录。
            <br />
            选择输入图片并开始运行后，每张图的 <b>*_P</b> 输出会出现在这里。
          </div>
        ) : (
          props.history.map((row) => (
            <button
              key={row.output_root}
              className={`tree-row ${props.selectedRoot === row.output_root ? "selected" : ""}`}
              onClick={() => props.onSelect(row)}
              title={row.output_root}
            >
              <span className={`dot ${kindDot(row.kind)}`} />
              {row.name}
              <span className="meta">
                {row.line_candidates ?? "--"}L/{row.text_candidates ?? "--"}T
              </span>
            </button>
          ))
        )}
      </div>
      <div className="queue-section">
        <div className="panel-title">
          运行队列
          <span className="sub">{props.batchRunning ? "批量进行中" : ""}</span>
        </div>
        {props.batchRows.length === 0 ? (
          <div className="queue-row">
            <span className="q-status">{props.batchRunning ? "等待第一张图完成…" : "无批量任务"}</span>
          </div>
        ) : (
          props.batchRows.slice(-8).map((row, index) => (
            <div key={index} className="queue-row">
              {row.map_id ?? "?"}
              <span className="q-status">{row.status ?? ""}</span>
            </div>
          ))
        )}
      </div>
      <div className="panel-note">候选不会自动标记 checked=yes；一切以人工 MapGIS 验收为准。</div>
    </div>
  );
}
