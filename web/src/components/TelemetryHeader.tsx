import { Fragment, useEffect, useState } from "react";
import type { RunView } from "../types";
import { Icon } from "./Icon";
import { SettingsModal, type ConfigPayload } from "./SettingsModal";

export type { ConfigPayload };

const STAGES: Array<{ key: RunView["stage"]; label: string }> = [
  { key: "plan", label: "规划" },
  { key: "dispatch", label: "任务准备" },
  { key: "research", label: "研究执行" },
  { key: "reflect", label: "报告规划" },
  { key: "write", label: "写作" },
  { key: "check", label: "结构检查" },
  { key: "done", label: "完成" },
];
const STAGE_ORDER = STAGES.map((s) => s.key);
type StageVisualState = "done" | "current" | "pending" | "warning" | "cancelled" | "failed";

function stageVisualState(view: RunView, index: number, currentIndex: number): StageVisualState {
  if (index < currentIndex) return "done";
  if (index > currentIndex) return "pending";
  if (view.endedAtMs === undefined) return "current";
  switch (view.banner?.kind) {
    case "done": return "done";
    case "partial": return "warning";
    case "error": return "failed";
    case "cancelled": return "cancelled";
    default: return "pending";
  }
}

function fmt(ms: number): string {
  const totalSec = Math.max(0, Math.floor(ms / 1000));
  const mm = Math.floor(totalSec / 60).toString().padStart(2, "0");
  const ss = (totalSec % 60).toString().padStart(2, "0");
  return `T+${mm}:${ss}`;
}

export function TelemetryHeader({
  view, nowMs, mode, onMode, onCancel, canCancel, config, onConfigChange,
}: {
  view: RunView;
  nowMs: number;
  mode: "debug" | "clean";
  onMode: (m: "debug" | "clean") => void;
  onCancel: () => void;
  canCancel: boolean;
  config: ConfigPayload | null;
  onConfigChange: (c: ConfigPayload) => void;
}) {
  const [showSettings, setShowSettings] = useState(false);
  // 取消二段确认(替换原生 window.confirm——全站唯一风格断裂处):
  // 首次点亮红待确认,3 秒或不可取消时自动回弹。
  const [cancelArmed, setCancelArmed] = useState(false);
  useEffect(() => {
    if (!cancelArmed) return;
    const t = setTimeout(() => setCancelArmed(false), 3000);
    return () => clearTimeout(t);
  }, [cancelArmed]);
  useEffect(() => {
    if (!canCancel) setCancelArmed(false);
  }, [canCancel]);
  const curIdx = STAGE_ORDER.indexOf(view.stage);
  const terminal = view.endedAtMs !== undefined;
  const subagents = Object.values(view.subagents);
  const doneCount = subagents.filter((s) => s.status === "done").length;

  const elapsed = view.startedAtMs !== undefined
    ? fmt((view.endedAtMs ?? nowMs) - view.startedAtMs)
    : "T+00:00";

  return (
    <div className="theader">
      <div className="stages">
        {STAGES.map((s, i) => {
          const state = stageVisualState(view, i, curIdx);
          // 段间连线：已完成段填绿，进入当前节点的那段走绿→蓝渐变，其余中性——
          // 让七段进度读成一条连贯的遥测轴，而不是七个孤立的灯。
          const seg = i < curIdx
            ? (i === curIdx - 1 && !terminal ? " stage-seg-active" : " stage-seg-filled")
            : "";
          return (
            <Fragment key={s.key}>
              <span className={`stage stage-${state}`}>
                <span className={`lamp lamp-${state}`} />
                {s.label}
                {s.key === "research" && subagents.length > 0 && (
                  <span className="num stage-count">{doneCount}/{subagents.length}</span>
                )}
              </span>
              {i < STAGES.length - 1 && <span className={`stage-seg${seg}`} />}
            </Fragment>
          );
        })}
      </div>
      <span className="num elapsed">{elapsed}</span>
      <button
        className={`cancel-btn${cancelArmed ? " cancel-armed" : ""}`}
        disabled={!canCancel}
        onClick={() => {
          if (cancelArmed) { setCancelArmed(false); onCancel(); }
          else setCancelArmed(true);
        }}
      >
        {cancelArmed ? "确认取消？" : "取消"}
      </button>
      <button className="mode-toggle" onClick={() => onMode(mode === "debug" ? "clean" : "debug")}>
        {mode === "debug" ? "调试" : "简洁"}
      </button>
      <span className="model-chip" onClick={() => setShowSettings(true)}>
        <Icon name="settings" size={14} />{config ? config.subagent.model : "…"}
      </span>
      {showSettings && (
        <SettingsModal
          onClose={() => setShowSettings(false)}
          onSaved={(c) => { onConfigChange(c); setShowSettings(false); }}
        />
      )}
    </div>
  );
}
