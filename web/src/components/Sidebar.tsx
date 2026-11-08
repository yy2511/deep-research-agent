import { useEffect, useState } from "react";
import { getJSON } from "../api";
import { Icon } from "./Icon";
import { runStatusView } from "./runStatusView";
import { relativeTime } from "../format";
import type { Showcase } from "../showcases";

export interface RunListItem {
  run_id: string; conversation_id?:string; turn_count?:number; latest_run_id?:string;
  query: string;
  status: string;
  started_at?: number;
  finished_at?: number | null;
  stats?: { n_evidence?: number } | null;
  event_schema_version?: number;
  replay_compatible?: boolean;
  replay_error?: string | null;
}

export function RunHistoryItem({
  run, currentRunId, onSelectLive, onSelectReplay, showReplay = false,
}: {
  run: RunListItem;
  currentRunId: string | null;
  onSelectLive: (runId: string) => void;
  onSelectReplay: (runId: string, mode: "instant" | "paced") => void;
  /** 开发者选项“历史回放”打开后才显示快放按钮。 */
  showReplay?: boolean;
}) {
  const status = runStatusView(run.status);
  const replayCompatible = run.replay_compatible === true;
  const running = run.status === "running";
  const done = run.status === "done";
  const tone = running ? "live" : done ? "" : run.status === "failed" ? "bad" : "warn";
  return (
    <div className={`history-item${run.run_id === currentRunId ? " history-item-active" : ""}`}>
      <button
        className="history-item-main"
        title={!replayCompatible ? "旧版本记录：可以查看结果，不能回放过程" : run.query}
        onClick={() => (running ? onSelectLive(run.run_id) : onSelectReplay(run.run_id, "instant"))}
      >
        <span className="history-item-query">{run.query}</span>
        <span className={`history-item-meta${tone ? ` is-${tone}` : ""}`}>
          {!done && <span className={`lamp ${status.lamp}`} title={status.label} />}
          {!done && <span>{status.label}</span>}
          {!done && run.started_at ? <span> · </span> : null}
          <span>{relativeTime(run.started_at)}</span>
          {run.turn_count && run.turn_count > 1 ? <span> · {run.turn_count} 轮</span> : null}
        </span>
      </button>
      {showReplay && !running && replayCompatible && (
        <button className="history-item-replay icon-btn" title="按时间顺序快放研究过程" aria-label="快放研究过程"
          onClick={() => onSelectReplay(run.run_id, "paced")}><Icon name="replay" size={14} /></button>
      )}
    </div>
  );
}

/** 示例分组（#47）：所有人只读可见。 */
export function ShowcaseList({ showcases, currentRunId, onSelect }: {
  showcases: Showcase[];
  currentRunId: string | null;
  onSelect: (showcase: Showcase) => void;
}) {
  if (showcases.length === 0) return null;
  return (
    <nav className="history-list showcase-list" aria-label="示例">
      <div className="history-title">示例 · 只读</div>
      {showcases.map((item) => (
        <div key={item.conversation_id}
          className={`history-item${currentRunId && item.run_ids.includes(currentRunId) ? " history-item-active" : ""}`}>
          <button className="history-item-main" title={item.query} disabled={!item.available}
            onClick={() => onSelect(item)}>
            <span className="history-item-query">{item.title}</span>
            <span className="history-item-meta">
              {item.available ? <span>{item.turn_count > 1 ? `${item.turn_count} 轮 · ` : ""}{relativeTime(item.finished_at ?? undefined)}</span>
                : <span>该示例已不可用</span>}
            </span>
          </button>
        </div>
      ))}
    </nav>
  );
}

export function Sidebar({
  refreshSignal, currentRunId, onSelectLive, onSelectReplay, showReplay = false,
}: {
  // 值变化即触发重拉——不关心具体内容，App.tsx 每次 run 收到 end 事件就传个新值进来。
  refreshSignal: unknown;
  currentRunId: string | null;
  onSelectLive: (runId: string) => void;
  onSelectReplay: (runId: string, mode: "instant" | "paced") => void;
  showReplay?: boolean;
}) {
  const [runs, setRuns] = useState<RunListItem[]>([]);

  useEffect(() => {
    getJSON<RunListItem[]>("/api/conversations").then(setRuns).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSignal,currentRunId]);

  return (
    <nav className="history-list" aria-label="最近的研究">
      <div className="history-title">最近</div>
      {runs.length === 0 && <p className="history-empty">还没有研究记录</p>}
      {runs.map((run) => (
        <RunHistoryItem
          key={run.run_id}
          run={run}
          currentRunId={currentRunId}
          onSelectLive={onSelectLive}
          onSelectReplay={onSelectReplay}
          showReplay={showReplay}
        />
      ))}
    </nav>
  );
}
