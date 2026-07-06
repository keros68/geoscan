import { useState } from "react";
import { RunForm } from "../types";

interface Props {
  form: RunForm;
  hasSavedKey: boolean;
  busy: boolean;
  onUpdateForm: (patch: Partial<RunForm>) => void;
  onTest: () => void;
  onAnalyze: () => void;
  onSave: (saveKey: boolean) => void;
}

export default function AiPanel(props: Props) {
  const { form } = props;
  const [saveKey, setSaveKey] = useState(props.hasSavedKey);

  return (
    <div className="ai-panel">
      <div className="ai-form">
        <div className="field-grid" style={{ gridTemplateColumns: "76px 1fr" }}>
          <label htmlFor="ai-provider">Provider</label>
          <select
            id="ai-provider"
            value={form.ai_provider}
            disabled={props.busy}
            onChange={(event) => props.onUpdateForm({ ai_provider: event.target.value as RunForm["ai_provider"] })}
          >
            <option value="none">none（不使用 AI）</option>
            <option value="openai-compatible">openai-compatible</option>
            <option value="qwen">qwen</option>
            <option value="custom">custom</option>
          </select>
          <label htmlFor="ai-url">Base URL</label>
          <input
            id="ai-url"
            type="text"
            value={form.ai_base_url}
            disabled={props.busy}
            onChange={(event) => props.onUpdateForm({ ai_base_url: event.target.value })}
          />
          <label htmlFor="ai-model">Model</label>
          <input
            id="ai-model"
            type="text"
            value={form.ai_model}
            disabled={props.busy}
            onChange={(event) => props.onUpdateForm({ ai_model: event.target.value })}
          />
          <label htmlFor="ai-key">API Key</label>
          <input
            id="ai-key"
            type="password"
            value={form.ai_api_key}
            placeholder={props.hasSavedKey ? "已加密保存（留空即使用；输入则替换）" : "输入 API Key"}
            disabled={props.busy}
            onChange={(event) => props.onUpdateForm({ ai_api_key: event.target.value })}
          />
        </div>
        <div className="checkbox-row">
          <input
            id="ai-savekey"
            type="checkbox"
            checked={saveKey}
            disabled={props.busy}
            onChange={(event) => setSaveKey(event.target.checked)}
          />
          <label htmlFor="ai-savekey">在本机加密保存 API Key（Windows 账户级加密，拷给别人解不出来）</label>
        </div>
        <div className="ai-actions">
          <button className="btn" onClick={() => props.onSave(saveKey)} disabled={props.busy}>
            保存 AI 设置
          </button>
          <button className="btn" onClick={props.onTest} disabled={props.busy || form.ai_provider === "none"}>
            测试连接
          </button>
          <button className="btn" onClick={props.onAnalyze} disabled={props.busy || form.ai_provider === "none"}>
            AI 看图描述（仅诊断）
          </button>
        </div>
      </div>
      <div className="ai-notes">
        <p>此页只配置连接；“启用 AI 增强”开关在右侧“高级参数 → 线提取”里（属运行参数）。</p>
        <p>AI 只提名，程序算坐标并用栅格证据验证；结果写入全新增强层，raw/repaired 永不改动。</p>
        <p>AI 只写复核建议到 AI_VISUAL_REVIEW；不写最终坐标、不写 checked=yes。</p>
        <p>API Key 只在本机使用：不进日志、不进事件、不随设置文件同步。</p>
      </div>
    </div>
  );
}
