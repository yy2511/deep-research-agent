import { useState, type ReactNode } from "react";
import type { PlanTrace } from "../types";
import { Icon } from "./Icon";
import { LlmCallsList, llmCallsFromPlanTrace } from "./LlmCalls";

// 示例问题：点一下填进输入框，让空白页一眼知道“能问什么”。
const EXAMPLES = [
  "Python 3.12 的 asyncio.wait_for 超时后会不会取消任务？",
  "Redis 的 AOF 和 RDB，哪个更适合对丢数据敏感的场景？",
  "sqlite3_rsync 能不能同步同一台机器上的两个数据库文件？",
];

export function NewResearch({
  busy, error, mode = "clean", planTrace = null, onSubmit, controls, canSubmit = true, blockedHint, initialQuery = "", onDraftChange,
  progress,
}: {
  initialQuery?: string; onDraftChange?: (value: string) => void;
  busy: boolean;
  error: string | null;
  mode?: "debug" | "clean";
  /** 规划失败时带回的 trace，调试档展示 LLM 输入输出。 */
  planTrace?: PlanTrace | null;
  onSubmit: (query: string) => void;
  /** 输入框底部左侧的按钮（来源 / 我的资料 / 技能）。 */
  controls?: ReactNode;
  canSubmit?: boolean;
  /** 不能提交时给出的原因。 */
  blockedHint?: string;
  /** 规划进行中的进度框；有它时取代下方的示例问题。 */
  progress?: ReactNode;
}) {
  const [query, setQuery] = useState(initialQuery);
  const failCalls = mode === "debug" ? llmCallsFromPlanTrace(planTrace) : [];
  const ready = !!query.trim() && !busy && canSubmit;

  function submit() {
    const q = query.trim();
    if (!q || busy || !canSubmit) return;
    onSubmit(q);
  }
  function fill(text: string) { setQuery(text); onDraftChange?.(text); }

  return (
    <div className="landing">
      <div className="landing-head">
        <h1 className="landing-title">想弄清什么问题？</h1>
        <p className="landing-sub">我会先和你确认目标，再去检索、阅读、保存证据，最后给出一份每个结论都能点开查看原文的回答。</p>
      </div>

      <div className="composer composer-large">
        <label className="sr-only" htmlFor="new-research-input">研究问题</label>
        <textarea
          id="new-research-input"
          className="composer-input"
          placeholder="例如：美股存储板块近期为何大跌？背后的宏观与产业链信号是什么？"
          value={query}
          disabled={busy}
          onChange={(e) => fill(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }}
        />
        <div className="composer-bar">
          <div className="composer-controls">{controls}</div>
          <span className="composer-hint">⌘ Enter 发送</span>
          <button className="btn btn-primary" disabled={!ready} onClick={submit}>
            {busy ? <><Icon name="loading" className="icon-spin" size={15} />准备中…</> : <>开始研究<Icon name="arrow-right" size={15} /></>}
          </button>
        </div>
      </div>
      {!canSubmit && blockedHint && <p className="field-hint is-warn">{blockedHint}</p>}
      {error && <p className="field-hint is-bad" role="alert">{error}</p>}
      {mode === "debug" && (failCalls.length > 0 || planTrace?.plan_id) && (
        <details className="disclosure" open>
          <summary>规划器调用（调试 · 本次失败）</summary>
          {planTrace?.plan_id && <p className="field-hint mono">已保存到 plans/{planTrace.plan_id}</p>}
          <LlmCallsList calls={failCalls} />
        </details>
      )}

      {progress ?? <div className="examples">
        <div className="examples-label">可以这样问</div>
        {EXAMPLES.map((ex) => (
          <button key={ex} className="example-row" disabled={busy} onClick={() => fill(ex)}>
            <span>{ex}</span><Icon name="arrow-right" size={15} />
          </button>
        ))}
      </div>}
    </div>
  );
}
