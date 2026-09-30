import { useEffect, useState } from "react";
import type { RunView } from "../types";
import { Icon } from "./Icon";
import { runOutcome } from "./runStatusView";
import { durationText } from "../format";

/**
 * 研究页右上角的状态：进行中显示用时和“停止”，结束后显示结果。
 * 停止要点两次确认，3 秒内没确认会自动复原。
 */
export function RunStatus({ view, nowMs, onCancel, canCancel, cancelling = false }: {
  view: RunView;
  nowMs: number;
  onCancel: () => void;
  canCancel: boolean;
  /** 已发出停止、后台还没退出：按钮显示“停止中…”并禁用。 */
  cancelling?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 3000);
    return () => clearTimeout(t);
  }, [armed]);
  useEffect(() => { if (!canCancel) setArmed(false); }, [canCancel]);

  if (view.startedAtMs === undefined && view.endedAtMs === undefined) return null;
  const outcome = runOutcome(view);
  const running = outcome.tone === "live";
  const elapsed = view.startedAtMs !== undefined
    ? durationText((view.endedAtMs ?? nowMs) - view.startedAtMs) : null;

  return (
    <div className="run-status">
      <span className={`run-state run-state-${outcome.tone}`}>
        {running ? <Icon name="loading" className="icon-spin" size={15} />
          : outcome.tone === "ok" ? <Icon name="check" size={15} />
          : outcome.tone === "stopped" ? <Icon name="stop" size={12} fill="currentColor" />
          : outcome.tone === "neutral" ? null : <Icon name="warning" size={15} />}
        {outcome.label}{running && elapsed ? ` · ${elapsed}` : ""}
      </span>
      {(canCancel || cancelling) && (
        <button
          type="button"
          className={`btn btn-sm${armed ? " btn-danger" : ""}`}
          disabled={!canCancel || cancelling}
          title={cancelling ? "已请求停止，正在等待后台请求结束" : undefined}
          onClick={() => {
            if (armed) { setArmed(false); onCancel(); }
            else setArmed(true);
          }}
        >
          {!cancelling && !armed && <Icon name="stop" size={11} fill="currentColor" />}
          {cancelling ? "停止中…" : armed ? "确认停止？" : "停止"}
        </button>
      )}
    </div>
  );
}
