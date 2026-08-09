import { useEffect, useState } from "react";
import { getJSON } from "../api";
import { Icon } from "./Icon";
import { runStatusView } from "./runStatusView";

export interface RunListItem {
  run_id: string;
  query: string;
  status: string;
  started_at?: number;
  finished_at?: number | null;
  stats?: { n_evidence?: number } | null;
  event_schema_version?: number;
  replay_compatible?: boolean;
  replay_error?: string | null;
}

function fmtTime(ts?: number): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} `
    + `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function RunHistoryItem({
  run, currentRunId, onSelectLive, onSelectReplay,
}: {
  run: RunListItem;
  currentRunId: string | null;
  onSelectLive: (runId: string) => void;
  onSelectReplay: (runId: string, mode: "instant" | "paced") => void;
}) {
  const status = runStatusView(run.status);
  const replayCompatible = run.replay_compatible === true;
  const running = run.status === "running";
  const mainDisabled = !replayCompatible;
  return (
    <div className={`history-item ${run.run_id === currentRunId ? "history-item-active" : ""}`}>
      <div className="history-item-row">
        <button
          className="history-item-main"
          disabled={mainDisabled}
          title={mainDisabled ? (run.replay_error ?? "旧版本、不可回放") : undefined}
          onClick={() => (running ? onSelectLive(run.run_id) : onSelectReplay(run.run_id, "instant"))}
        >
          <span className={`lamp ${status.lamp}`} title={status.label} />
          <span className="history-item-query">{run.query}</span>
        </button>
        {!running && replayCompatible && (
          <button className="history-item-replay" title="按时序快放" aria-label="按时序快放"
            onClick={() => onSelectReplay(run.run_id, "paced")}><Icon name="replay" size={14} /></button>
        )}
      </div>
      <div className="history-item-meta num">
        {fmtTime(run.started_at)}
        {run.stats?.n_evidence !== undefined && <> · 证据 {run.stats.n_evidence}</>}
        {!replayCompatible && <span className="history-item-incompatible"> · 旧版本、不可回放</span>}
      </div>
    </div>
  );
}

export function Sidebar({
  refreshSignal, currentRunId, onSelectLive, onSelectReplay,
}: {
  // 值变化即触发重拉——不关心具体内容，App.tsx 每次 run 收到 end 事件就传个新值进来。
  refreshSignal: unknown;
  currentRunId: string | null;
  onSelectLive: (runId: string) => void;
  onSelectReplay: (runId: string, mode: "instant" | "paced") => void;
}) {
  const [runs, setRuns] = useState<RunListItem[]>([]);

  useEffect(() => {
    getJSON<RunListItem[]>("/api/runs").then(setRuns).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSignal]);

  if (runs.length === 0) return null;

  return (
    <div className="history-list">
      <div className="history-title">历史</div>
      {runs.map((run) => (
        <RunHistoryItem
          key={run.run_id}
          run={run}
          currentRunId={currentRunId}
          onSelectLive={onSelectLive}
          onSelectReplay={onSelectReplay}
        />
      ))}
    </div>
  );
}
