import { useState } from "react";
import { Icon } from "./Icon";
import { LlmCallsList, llmCallsFromPlanTrace } from "./LlmCalls";

export interface ResearchTask { id: string; node_id: string; objective: string; search_query: string; boundaries: string | null }
export interface PlanNode {
  id: string;
  objective: string;
  kind: "research" | "decision";
  dependency_ids: string[];
  acceptance_criteria: string;
}
export interface PlanBudget {
  max_research_rounds: number;
  max_tasks_per_round: number;
  max_total_tasks: number;
  estimated_min_tasks?: number;
  recommended_tasks?: number;
  budget_tight?: boolean;
}
export interface PlanTrace {
  plan_id?: string | null;
  events?: any[];
}
export interface ResearchPlan {
  clarified_query: string;
  initial_tasks: ResearchTask[];
  plan_nodes: PlanNode[];
  budget?: PlanBudget;
  /** 调试用：规划/修订阶段 llm_call 等事件；开研究前会剥离，不进执行契约。 */
  trace?: PlanTrace;
}

export function PlanCard({
  plan, busy, hint, error, mode = "clean", onStart, onRevise, onCancel,
}: {
  plan: ResearchPlan;
  busy: boolean;
  hint: string | null;
  error: string | null;
  mode?: "debug" | "clean";
  onStart: () => void;
  onRevise: (feedback: string) => Promise<void>;
  onCancel: () => void;
}) {
  const planLlmCalls = mode === "debug" ? llmCallsFromPlanTrace(plan.trace) : [];
  const nodeNumber = new Map(plan.plan_nodes.map((node, index) => [node.id, index + 1]));
  const nodeById = new Map(plan.plan_nodes.map((node) => [node.id, node]));
  const nodeLabel = (id: string) => {
    const node = nodeById.get(id);
    const number = nodeNumber.get(id);
    return node && number ? `步骤 ${number} · ${node.objective}` : id;
  };
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
    <div className="plan-card">
      <div className="plan-query">{plan.clarified_query}</div>
      {plan.budget && (
        <div className="plan-budget num">
          <span className="plan-budget-title">计划预算</span>
          <span>最多 {plan.budget.max_research_rounds} 轮研究</span>
          <span>每轮最多 {plan.budget.max_tasks_per_round} 项任务</span>
          <span>任务总数上限 {plan.budget.max_total_tasks}</span>
          {typeof plan.budget.estimated_min_tasks === "number" && (
            <span>最低 ≈ {plan.budget.estimated_min_tasks}</span>
          )}
          {typeof plan.budget.recommended_tasks === "number" && (
            <span>建议 ≥ {plan.budget.recommended_tasks}</span>
          )}
          {plan.budget.budget_tight && (
            <span className="plan-budget-tight">预算偏紧：补查余量有限</span>
          )}
        </div>
      )}
      <div className="plan-list">
        <div className="plan-nodes">
          <div className="plan-group-title">研究步骤</div>
          {plan.plan_nodes.map((node, i) => (
              <div className="plan-item plan-node-item" key={node.id}>
                <div className="plan-node-head">
                  <span className={`plan-kind plan-kind-${node.kind}`}>
                    {node.kind === "research" ? "研究" : "决策"}
                  </span>
                  <span className="plan-objective">{i + 1}. {node.objective}</span>
                  {mode === "debug" && <span className="plan-node-id num">{node.id}</span>}
                </div>
                <div className="plan-dependency">
                  <Icon name="dependency" />{node.dependency_ids.length
                    ? `前置步骤：${node.dependency_ids.map(nodeLabel).join("；")}`
                    : "可直接开始"}
                </div>
                <div className="plan-acceptance"><Icon name="check-circle" />验收标准：{node.acceptance_criteria}</div>
              </div>
          ))}
        </div>
        <div className="plan-group-title plan-task-title">首轮研究任务</div>
        {plan.initial_tasks.map((task, i) => (
          <div className="plan-item" key={task.id}>
            <div className="plan-objective">{i + 1}. {task.objective}</div>
            <div className="plan-task-owner"><Icon name="dependency" />关联步骤：{nodeLabel(task.node_id)}</div>
            <div className="plan-query-line"><Icon name="search" />{task.search_query}</div>
            {task.boundaries && <div className="plan-boundaries"><Icon name="boundary" />{task.boundaries}</div>}
          </div>
        ))}
      </div>
      <div className="plan-revise">
        <input
          className="plan-revise-input"
          placeholder="修改意见（自然语言）…"
          value={feedback}
          disabled={busy}
          onChange={(e) => setFeedback(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") submitRevise(); }}
        />
        <button className="plan-revise-btn" disabled={busy || !feedback.trim()} onClick={submitRevise}>
          <Icon name="edit" size={14} />修订
        </button>
      </div>
      {hint && <div className="plan-hint">{hint}</div>}
      {reviseError && <div className="plan-error">{reviseError}</div>}
      {error && <div className="plan-error">{error}</div>}
      {mode === "debug" && (planLlmCalls.length > 0 || plan.trace?.plan_id) && (
        <div className="plan-debug-io orch-card orch-llm-calls" style={{ borderLeftColor: "var(--line)" }}>
          <div className="orch-kind">规划器调用（调试）</div>
          {plan.trace?.plan_id && (
            <div className="orch-body num">落盘 plans/{plan.trace.plan_id}</div>
          )}
          <LlmCallsList calls={planLlmCalls} />
        </div>
      )}
      <div className="plan-actions">
        <button className="plan-cancel-btn" disabled={busy} onClick={onCancel}><Icon name="close" size={14} />取消</button>
        <button className="plan-start-btn" disabled={busy} onClick={onStart}><Icon name="play" size={14} />开始研究</button>
      </div>
    </div>
  );
}
