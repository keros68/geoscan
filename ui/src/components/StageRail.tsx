import { STAGE_LABELS, STAGE_ORDER, StageKey, StageState } from "../types";

const STATE_TEXT: Record<StageState, string> = {
  pending: "等待",
  running: "进行中",
  completed: "完成",
  warning: "有警告",
  failed: "失败",
  skipped: "已跳过",
  blocked: "被阻断",
  cancelled: "已取消",
};

export default function StageRail({ stages }: { stages: Record<StageKey, StageState> }) {
  return (
    <div className="stage-rail">
      {STAGE_ORDER.map((key, index) => {
        const state = stages[key] ?? "pending";
        return (
          <div
            key={key}
            className={`stage-chip ${state}`}
            tabIndex={0}
            title={`输出文件夹: ${key} — ${STATE_TEXT[state]}`}
          >
            <span className="stage-status">
              <span className={`dot ${state}`} />
              {index + 1}. {STAGE_LABELS[key]}
            </span>
            <span className="stage-sub">{STATE_TEXT[state]}</span>
          </div>
        );
      })}
    </div>
  );
}
