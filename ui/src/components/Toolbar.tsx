import { RunForm } from "../types";

interface Props {
  form: RunForm;
  busy: boolean;
  engineReady: boolean;
  onOpenImage: () => void;
  onChooseOutputParent: () => void;
  onPreflight: () => void;
  onStart: () => void;
  onStop: () => void;
  onUpdateForm: (patch: Partial<RunForm>) => void;
}

function middleTruncate(path: string, max = 46): string {
  if (path.length <= max) return path;
  const head = Math.floor((max - 1) * 0.35);
  return `${path.slice(0, head)}…${path.slice(path.length - (max - 1 - head))}`;
}

export default function Toolbar(props: Props) {
  const { form } = props;
  return (
    <div className="toolbar">
      <button className="btn" onClick={props.onOpenImage} disabled={props.busy}>
        打开影像
      </button>
      <button className="btn" onClick={props.onChooseOutputParent} disabled={props.busy}>
        输出目录
      </button>
      <button className="btn" onClick={props.onPreflight}>
        预检
      </button>
      <div className="tool-sep" />
      <button className="btn primary" onClick={props.onStart} disabled={props.busy || !props.engineReady}>
        ▶ 开始
      </button>
      <button className="btn danger" onClick={props.onStop} disabled={!props.busy}>
        ■ 停止
      </button>
      <div className="tool-sep" />
      <button
        className={`btn ${form.enhanced_preview !== "none" ? "toggle-on" : ""}`}
        title="额外生成锐化底图（人工看图用，几何与矢量对齐）"
        onClick={() => props.onUpdateForm({ enhanced_preview: form.enhanced_preview === "none" ? "standard" : "none" })}
        disabled={props.busy}
      >
        增强预览
      </button>
      <button
        className={`btn ${form.export_dxf ? "toggle-on" : ""}`}
        title="导出 DXF/SHP 交换文件"
        onClick={() => props.onUpdateForm({ export_dxf: !form.export_dxf })}
        disabled={props.busy}
      >
        导出 DXF
      </button>
      <button
        className={`btn ${form.conversion_mode === "cli" ? "toggle-on" : ""}`}
        title="cli=自动转出 WT/WL（需要本机 MapGIS + 密码狗）；关闭后只生成候选和 DXF"
        onClick={() => props.onUpdateForm({ conversion_mode: form.conversion_mode === "cli" ? "none" : "cli" })}
        disabled={props.busy}
      >
        MapGIS 转换
      </button>
      <div className="spring" />
      <span className="workspace" title={form.output_parent || "尚未选择工作目录"}>
        {form.output_parent ? `workspace: ${middleTruncate(form.output_parent)}` : "workspace: —"}
      </span>
    </div>
  );
}
