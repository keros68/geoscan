interface Props {
  statusLabel: string;
  busy: boolean;
  engineState: "starting" | "ok" | "dead";
  overlayActive: boolean;
  version: string;
  onRestartEngine: () => void;
}

export default function StatusBar(props: Props) {
  const engineText =
    props.engineState === "ok" ? "引擎: 已连接" : props.engineState === "starting" ? "引擎: 启动中…" : "引擎: 已断开";
  return (
    <div className="statusbar">
      <span className={`cell ${props.busy ? "state-running" : ""}`}>
        {props.busy ? `● ${props.statusLabel || "运行中"}` : props.statusLabel || "就绪"}
      </span>
      <span className="cell">CRS: pixel unit</span>
      <span className="cell">overlay: {props.overlayActive ? "raster + line + text" : "—"}</span>
      <span className="cell">AI: review-only</span>
      <span className="spring" />
      <span className="cell">
        {engineText}
        {props.engineState === "dead" && (
          <button
            className="btn"
            style={{ minHeight: 20, padding: "0 8px", marginLeft: 8, fontSize: 11.5 }}
            onClick={props.onRestartEngine}
          >
            重启引擎
          </button>
        )}
      </span>
      {props.version && <span className="cell">geoscan v{props.version}</span>}
    </div>
  );
}
