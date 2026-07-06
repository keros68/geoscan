import { RunForm } from "../types";

interface Props {
  form: RunForm;
  busy: boolean;
  engineReady: boolean;
  inspectorVisible: boolean;
  onOpenImage: () => void;
  onChooseOutputParent: () => void;
  onPreflight: () => void;
  onRunParams: () => void;
  onAdvanced: () => void;
  onBatch: () => void;
  onAiSettings: () => void;
  onToggleInspector: () => void;
  onSettings: () => void;
  onCheckUpdate: () => void;
  onHelp: () => void;
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
      <button className="btn" onClick={props.onRunParams}>
        运行参数
      </button>
      <button className="btn" onClick={props.onAdvanced}>
        高级参数
      </button>
      <button className="btn" onClick={props.onAiSettings}>
        AI设置
      </button>
      <button className="btn" onClick={props.onBatch}>
        批量
      </button>
      <button className="btn" onClick={props.onToggleInspector}>
        {props.inspectorVisible ? "隐藏右栏" : "显示右栏"}
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
      <div className="spring" />
      <span className="workspace" title={form.output_parent || "尚未选择工作目录"}>
        {form.output_parent ? `workspace: ${middleTruncate(form.output_parent)}` : "workspace: —"}
      </span>
      <div className="tool-sep" />
      <button className="btn" onClick={props.onSettings}>
        本机设置
      </button>
      <button className="btn" onClick={props.onCheckUpdate}>
        检查更新
      </button>
      <button className="btn" onClick={props.onHelp}>
        帮助
      </button>
    </div>
  );
}
