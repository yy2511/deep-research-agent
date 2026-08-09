import { useEffect, useState } from "react";
import type { RunView, Subagent } from "../types";
import { displayRound } from "../displayLabels";

interface RailStageEntry { kind: "stage"; id: string; label: string; atMs: number }
interface RailWorkerEntry {
  kind: "worker";
  id: string;
  label: string;
  atMs: number;
  stepNumber?: number;
  lampClass: string;
}
type RailEntry = RailStageEntry | RailWorkerEntry;

function fmtMMSS(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const mm = Math.floor(total / 60).toString().padStart(2, "0");
  const ss = (total % 60).toString().padStart(2, "0");
  return `${mm}:${ss}`;
}

function workerLamp(subagent: Subagent): string {
  if (subagent.status === "running") return "lamp-current";
  if (subagent.status !== "done" || subagent.outcome === "unknown") return "lamp-pending";
  if (subagent.outcome === "ok") return "lamp-done";
  if (subagent.outcome === "empty" || subagent.outcome === "timeout") return "lamp-warning";
  return "lamp-failed";
}

/** timeline 主要 note + 全部 Worker → 锚点条目；次要编排事件仍不占位。 */
function railEntries(view: RunView): RailEntry[] {
  const out: RailEntry[] = [];
  const plan = view.timeline.find((item) => item.t === "note" && item.card.kind === "research_plan");
  const planNodes = plan?.t === "note" && Array.isArray(plan.card.plan_nodes)
    ? plan.card.plan_nodes as Array<{ id?: string }>
    : [];
  const nodeNumbers = new Map(planNodes.flatMap((node, index) =>
    typeof node.id === "string" ? [[node.id, index + 1] as const] : []
  ));

  view.timeline.forEach((it, i) => {
    if (it.t === "sub") {
      const subagent = view.subagents[it.sid];
      if (subagent) {
        out.push({
          kind: "worker",
          id: `tl-${i}`,
          label: subagent.objective || "未命名研究任务",
          atMs: it.atMs,
          stepNumber: subagent.nodeId ? nodeNumbers.get(subagent.nodeId) : undefined,
          lampClass: workerLamp(subagent),
        });
      }
      return;
    }
    if (it.t !== "note") return;
    const c = it.card;
    const push = (label: string) => out.push({ kind: "stage", id: `tl-${i}`, label, atMs: c.atMs });
    switch (c.kind) {
      case "scope": push("研究问题"); break;
      case "research_plan": push("研究规划"); break;
      case "task_batch_dispatched":
        if (c.phase === "initial" || c.round_index === 0) push(`首轮研究 ×${String(c.count ?? "?")}`);
        break;
      case "nodes_assessed": push("步骤验收"); break;
      case "ready_set_computed":
        push(c.pending === 0 || c.n_tasks === 0
          ? "研究步骤已完成"
          : c.phase === "final_research_pass"
            ? "最终补查准备"
            : `第 ${displayRound(c.round_index)} 轮任务准备`);
        break;
      case "collect": push("证据汇总"); break;
      case "report_plan": push("报告规划"); break;
      case "cross_worker_audit": push("跨任务一致性与覆盖检查"); break;
      case "writing": push("综合写作"); break;
      case "done": case "error": case "cancelled":
        out.push({ kind: "stage", id: view.reportMd ? "tl-report" : `tl-${i}`, label: view.reportMd ? "研究报告" : "结束", atMs: c.atMs });
        break;
    }
  });
  return out;
}

export function FlowRail({ view, nowMs: _nowMs }: { view: RunView; nowMs: number }) {
  const entries = railEntries(view);
  const [activeId, setActiveId] = useState<string | null>(null);
  const running = view.startedAtMs !== undefined && view.endedAtMs === undefined;
  const entryIdsKey = entries.map((entry) => entry.id).join("\u0000");

  // 滚动跟踪当前小节：以内层主滚动区顶部为准。Worker 折叠后只有一行，若继续用
  // window 40% 线，点击锚点会同时越过后面数张短卡，最终高亮到错误条目。
  useEffect(() => {
    const ids = entryIdsKey ? entryIdsKey.split("\u0000") : [];
    const onScroll = () => {
      const main = document.querySelector<HTMLElement>(".stage-main");
      const cut = (main?.getBoundingClientRect().top ?? 0) + 24;
      let current: string | null = null;
      for (const id of ids) {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top <= cut) current = id;
      }
      setActiveId(current);
    };
    onScroll();
    window.addEventListener("scroll", onScroll, true);
    return () => window.removeEventListener("scroll", onScroll, true);
  }, [entryIdsKey]);

  if (entries.length === 0) return null;

  const subs = Object.values(view.subagents);
  // Worker 之间可能收录重复证据；结束/汇总事件给的是全局去重口径，应优先展示。
  const evidence = view.evidenceCount ?? subs.reduce((sum, s) => sum + (s.evidenceTotal || 0), 0);
  const llmCalls = Object.values(view.stats.llm).reduce((sum, r) => sum + r.calls, 0);
  const toolCalls = Object.values(view.stats.tools).reduce((sum, r) => sum + r.calls, 0);

  return (
    <nav className="flow-rail">
      <div className="rail-title">研究流</div>
      {entries.map((e, i) => {
        const isLast = i === entries.length - 1;
        const rel = view.startedAtMs !== undefined ? fmtMMSS(e.atMs - view.startedAtMs) : null;
        const worker = e.kind === "worker";
        return (
          <button
            className={`rail-item${worker ? " rail-worker-item" : ""}${e.id === activeId ? " rail-on" : ""}`}
            key={e.id}
            aria-label={worker ? `${e.stepNumber ? `步骤 ${e.stepNumber}` : "研究任务"}：${e.label}` : e.label}
            title={worker ? e.label : undefined}
            onClick={() => document.getElementById(e.id)?.scrollIntoView({ block: "start", behavior: "smooth" })}
          >
            {/* 阶段灯表达运行位置；Worker 灯与卡片状态语义一致；阅读位置只由 rail-on 表达。 */}
            <span className={`lamp ${worker ? e.lampClass : isLast && running ? "lamp-current" : "lamp-done"}`} />
            {worker && <span className="num rail-worker-step">#{e.stepNumber ?? "?"}</span>}
            <span className="rail-label">{e.label}</span>
            {!worker && rel && <span className="num rail-ts">{rel}</span>}
          </button>
        );
      })}
      <div className="rail-stats">
        <div className="rail-stat"><span>证据</span><b className="num">{evidence}</b></div>
        <div className="rail-stat"><span>模型调用</span><b className="num">{llmCalls}</b></div>
        <div className="rail-stat"><span>工具调用</span><b className="num">{toolCalls}</b></div>
      </div>
    </nav>
  );
}
