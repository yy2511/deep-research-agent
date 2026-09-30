import { useState } from "react";
import type { PlanTrace } from "../types";
import { Icon } from "./Icon";
import { LlmCallsList, llmCallsFromPlanTrace } from "./LlmCalls";

export interface BriefRequirement {
  output_formats?: string[];
  id: string;
  text: string;
  kind: "goal" | "constraint";
}

export interface BriefRouteItem {
  id: string;
  text: string;
}

/**
 * 开始研究前的计划确认卡：要回答什么、必须满足什么、打算怎么查。
 * 路线不是硬约束，主 Agent 研究中会自己调整。模型判断不需要确认时，这张卡不出现。
 */
export interface ResearchBrief {
  original_query: string;
  objective: string;
  requirements: BriefRequirement[];
  route: BriefRouteItem[];
  /** 用户跳过澄清后，模型不得不先假设的前提——必须让用户看见。 */
  assumptions?: string[];
  needs_confirmation: boolean;
  confirmation_reason: string;
  /** 调试用：规划阶段的 llm_call 事件；开研究前会剥离，不进执行契约。 */
  trace?: PlanTrace;
}

export function BriefCard({
  brief, busy, hint, error, mode = "clean", onStart, onRevise, onCancel,
}: {
  brief: ResearchBrief;
  busy: boolean;
  hint: string | null;
  error: string | null;
  mode?: "debug" | "clean";
  onStart: () => void;
  onRevise: (feedback: string) => Promise<void>;
  onCancel: () => void;
}) {
  const briefLlmCalls = mode === "debug" ? llmCallsFromPlanTrace(brief.trace) : [];
  const [feedback, setFeedback] = useState("");
  const [reviseError, setReviseError] = useState<string | null>(null);

  async function submitRevise() {
    const text = feedback.trim();
    if (!text || busy) return;
    setReviseError(null);
    try {
      await onRevise(text);
      setFeedback("");   // 只在修订成功后清空——失败要留着原文本，不逼用户重打
    } catch (e) {
      setReviseError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <section className="card prerun-card" aria-label="研究计划">
      <div className="prerun-head">
        <small>研究计划</small>
        <h2>{brief.objective}</h2>
      </div>

      {brief.requirements.length > 0 && <div className="brief-section" aria-label="要做到">
        <h3>要做到</h3>
        {brief.requirements.map((r) => (
          <div key={r.id} className="brief-requirement">
            <span className={`tag${r.kind === "constraint" ? " tag-accent" : ""}`}>{r.kind === "constraint" ? "必须" : "目标"}</span>
            <span>{r.text}{r.output_formats?.length ? `（交付格式：${r.output_formats.join(" / ")}）` : ""}</span>
          </div>
        ))}
      </div>}

      {brief.route.length > 0 && <div className="brief-section">
        <h3>打算怎么查 <small>研究中会按实际情况调整</small></h3>
        <ol className="brief-route">
          {brief.route.map((item) => <li key={item.id}>{item.text}</li>)}
        </ol>
      </div>}

      {(brief.assumptions?.length ?? 0) > 0 && (
        <div className="callout callout-warn" aria-label="未经确认的前提">
          <Icon name="info" size={16} />
          <div>
            <strong>你没有回答的部分，会按这些假设进行：</strong>
            <ul>{(brief.assumptions ?? []).map((text) => <li key={text}>{text}</li>)}</ul>
          </div>
        </div>
      )}

      <div className="inline-form">
        <input
          className="input"
          aria-label="修改计划"
          placeholder="想改哪里？直接说，例如：顺便比较一下混合方案"
          value={feedback}
          disabled={busy}
          onChange={(e) => setFeedback(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void submitRevise(); }}
        />
        <button className="btn" disabled={busy || !feedback.trim()} onClick={submitRevise}>修改计划</button>
      </div>
      {hint && <p className="field-hint">{hint}</p>}
      {reviseError && <p className="field-hint is-bad" role="alert">{reviseError}</p>}
      {error && <p className="field-hint is-bad" role="alert">{error}</p>}
      {mode === "debug" && (briefLlmCalls.length > 0 || brief.trace?.plan_id) && (
        <details className="disclosure">
          <summary>规划器调用（调试）</summary>
          {brief.trace?.plan_id && <p className="field-hint mono">已保存到 plans/{brief.trace.plan_id}</p>}
          <LlmCallsList calls={briefLlmCalls} />
        </details>
      )}

      <div className="card-actions card-actions-divided">
        {brief.confirmation_reason && <span className="field-hint">需要你确认：{brief.confirmation_reason}</span>}
        <span className="spacer" />
        <button className="btn btn-ghost" disabled={busy} onClick={onCancel}>取消</button>
        <button className="btn btn-primary" disabled={busy} onClick={onStart}>
          {busy ? <><Icon name="loading" className="icon-spin" size={15} />处理中…</> : <>开始研究<Icon name="arrow-right" size={15} /></>}
        </button>
      </div>
    </section>
  );
}
