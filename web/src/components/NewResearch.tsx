import { useState } from "react";
import type { ConfigPayload } from "./SettingsModal";
import type { PlanTrace } from "./PlanCard";
import { LlmCallsList, llmCallsFromPlanTrace } from "./LlmCalls";

// 示例问题：点一下填进输入框。取材于历史/demo 的真实题面，让空闲页一眼知道"能问什么"。
const EXAMPLES = [
  "美股存储板块近期为何大跌？",
  "AI 产业的上下游格局",
  "分析 2010 年至今的黄金走势",
  "普通人如何实现财富自由",
];

export function NewResearch({
  busy, error, config, mode = "clean", planTrace = null, onSubmit,
}: {
  busy: boolean;
  error: string | null;
  config: ConfigPayload | null;
  mode?: "debug" | "clean";
  /** 规划失败时带回的 trace，调试档展示与过程页同款 llm I/O。 */
  planTrace?: PlanTrace | null;
  onSubmit: (query: string) => void;
}) {
  const [query, setQuery] = useState("");
  const failCalls = mode === "debug" ? llmCallsFromPlanTrace(planTrace) : [];

  function submit() {
    const q = query.trim();
    if (!q || busy) return;
    onSubmit(q);
  }

  return (
    <div className="landing">
      <div className="landing-head">
        <div className="landing-eyebrow">规划 · 并行研究 · 证据综合</div>
        <h1 className="landing-title">开始一次深度研究</h1>
        <p className="landing-sub">
          多个研究任务并行执行 · 证据驱动 · 全程可回放。输入问题后，系统会先生成研究计划，再开始检索与分析。
        </p>
      </div>

      <div className="new-research">
        <textarea
          className="new-research-input"
          placeholder="想研究什么？例如：美股存储板块近期为何大跌，背后的宏观与产业链信号是什么…"
          value={query}
          disabled={busy}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }}
        />
        <div className="new-research-row">
          <span className="new-research-spacer" />
          <button className="new-research-submit" disabled={busy || !query.trim()} onClick={submit}>
            {busy ? "规划中…" : "开始规划 →"}
          </button>
        </div>
        {error && <div className="new-research-error">{error}</div>}
        {mode === "debug" && (failCalls.length > 0 || planTrace?.plan_id) && (
          <div className="plan-debug-io orch-card orch-llm-calls" style={{ borderLeftColor: "var(--line)", marginTop: 12 }}>
            <div className="orch-kind">规划器调用（调试 · 本次失败）</div>
            {planTrace?.plan_id && (
              <div className="orch-body num">落盘 plans/{planTrace.plan_id}</div>
            )}
            <LlmCallsList calls={failCalls} />
          </div>
        )}
      </div>

      <div className="examples">
        <div className="examples-label">试试这些</div>
        <div className="examples-row">
          {EXAMPLES.map((ex) => (
            <button key={ex} className="example-chip" disabled={busy} onClick={() => setQuery(ex)}>
              {ex}
            </button>
          ))}
        </div>
      </div>

      <div className="landing-status num">
        <span className="landing-status-s"><span className="landing-status-dot" />就绪</span>
        {config && (
          <>
            <span className="landing-status-s">规划 {config.planner.model}</span>
            <span className="landing-status-s">检索 {config.subagent.model}</span>
          </>
        )}
      </div>
    </div>
  );
}
