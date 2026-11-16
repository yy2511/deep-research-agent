import { useState, type ReactNode } from "react";
import type { PlanTrace } from "../types";
import { Icon } from "./Icon";
import { LlmCallsList, llmCallsFromPlanTrace } from "./LlmCalls";

// 示例问题：点一下填进输入框，让空白页一眼知道“能问什么”。
const EXAMPLES = [
  { topic: "技术查证", title: "理解技术边界", query: "Python 3.12 的 asyncio.wait_for 超时后会不会取消任务？" },
  { topic: "方案比较", title: "看清方案取舍", query: "Redis 的 AOF 和 RDB，哪个更适合对丢数据敏感的场景？" },
  { topic: "版本研究", title: "找到可靠出处", query: "sqlite3_rsync 能不能同步同一台机器上的两个数据库文件？" },
];

export function NewResearch({
  busy, error, mode = "clean", planTrace = null, onSubmit, controls, canSubmit = true, blockedHint, initialQuery = "", onDraftChange,
  progress, notice,
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
  /** 输入框下的说明，例如访客今天还能问几次（#50）。 */
  notice?: ReactNode;
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
        <p className="landing-eyebrow"><span aria-hidden="true" />EviSee · 深度研究</p>
        <h1 className="landing-title">想弄清什么问题？</h1>
        <p className="landing-sub">深入研究，理清脉络。让答案背后的证据与出处，<br className="landing-desktop-break" />都能看得见、查得到。</p>
        <div className="landing-principles"><span><Icon name="search" size={14} />多源检索</span><span><Icon name="layers" size={14} />证据卡片</span><span><Icon name="book" size={14} />原文回查</span></div>
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
          <span className="composer-hint">Ctrl / ⌘ Enter</span>
          <button className="btn btn-primary" disabled={!ready} onClick={submit}>
            {busy ? <><Icon name="loading" className="icon-spin" size={15} />准备中…</> : <>开始研究<Icon name="arrow-right" size={15} /></>}
          </button>
        </div>
      </div>
      {notice}
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
        <div className="examples-heading"><div className="examples-label">从一个问题开始</div><span>也可以先试试这些方向</span></div>
        <div className="examples-grid">
        {EXAMPLES.map((ex) => (
          <button key={ex.query} className="example-row" disabled={busy} onClick={() => fill(ex.query)}>
            <span className="example-topic">{ex.topic}</span>
            <strong className="example-title">{ex.title}</strong>
            <span className="example-query">{ex.query}</span>
            <Icon name="arrow-right" size={16} />
          </button>
        ))}
        </div>
      </div>}
      <p className="landing-brandline">See the evidence. Check the source.</p>
    </div>
  );
}
