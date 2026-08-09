import { type EventTypeName } from "./events.gen";
import type { EvidenceCardView, LlmCall, RunView, Subagent, SubagentOutcome, ToolCall, WorkerStopReason } from "./types";
import { displayRound, toolLabel } from "./displayLabels";

type Handler = (v: RunView, e: any, atMs: number) => RunView;

export function initialView(): RunView {
  return {
    stage: "plan",
    planned: [],
    subagents: {},
    order: [],
    researchTaskNodeIds: {},
    timeline: [],
    phaseText: "",
    lastSeq: -1,
    stats: { llm: {}, steps: {}, tools: {} },
  };
}

const stageOrder = ["plan", "dispatch", "research", "reflect", "write", "check", "done"] as const;
function bumpStage(v: RunView, s: RunView["stage"]): RunView {
  return stageOrder.indexOf(s) > stageOrder.indexOf(v.stage) ? { ...v, stage: s } : v;
}
function inferredNodeId(v: RunView, e: any): string | undefined {
  if (typeof e.node_id === "string") return e.node_id;
  if (typeof v.researchTaskNodeIds[e.sid] === "string") return v.researchTaskNodeIds[e.sid];
  // schema v2 早期轨迹的动态 Worker 未带 node_id。只有当最近验收后恰好剩一个
  // research node 时才可确定性回填；多节点时宁可不显示，绝不靠文案相似度猜归属。
  let unresolvedIds: string[] = [];
  let foundAssessment = false;
  let planNodes: Array<{ id?: string; kind?: string }> = [];
  for (let i = v.timeline.length - 1; i >= 0; i--) {
    const item = v.timeline[i];
    if (item.t !== "note") continue;
    if (!foundAssessment && item.card.kind === "nodes_assessed") {
      foundAssessment = true;
      unresolvedIds = Array.isArray(item.card.unresolved_node_ids)
        ? item.card.unresolved_node_ids.filter((id): id is string => typeof id === "string")
        : [];
    }
    if (item.card.kind === "research_plan") {
      planNodes = Array.isArray(item.card.plan_nodes) ? item.card.plan_nodes : [];
      break;
    }
  }
  const unresolvedResearch = planNodes
    .filter((node) => node.kind === "research" && node.id && unresolvedIds.includes(node.id))
    .map((node) => node.id as string);
  return unresolvedResearch.length === 1 ? unresolvedResearch[0] : undefined;
}

function agent(v: RunView, e: any): Subagent {
  return v.subagents[e.sid] ?? { sid: e.sid, objective: e.objective ?? "", status: "pending",
    outcome: "unknown", nodeId: inferredNodeId(v, e), roundIndex: e.round_index,
    mode: "unknown", calls: [], llmCalls: [], cards: [], evidenceTotal: 0 };
}
function putAgent(v: RunView, s: Subagent): RunView {
  const order = v.order.includes(s.sid) ? v.order : [...v.order, s.sid];
  return { ...v, order, subagents: { ...v.subagents, [s.sid]: s } };
}
/** 编排叙事进编年流;事件级元字段剥掉,业务载荷原样入卡供渲染。 */
function note(v: RunView, kind: string, atMs: number, e?: any): RunView {
  const { type: _type, ts: _ts, seq: _seq, run_id: _rid, ...payload } = e ?? {};
  return { ...v, timeline: [...v.timeline, { t: "note", card: { kind, atMs, ...payload } }] };
}
/** 子代理首现挂锚(卡片在流中的位置);已有锚则原样返回。
 *  tool_call/done 也兜底建锚:回放丢 start 时卡片不至于凭空消失。 */
function withSubAnchor(v: RunView, sid: string, atMs: number): RunView {
  if (v.timeline.some((it) => it.t === "sub" && it.sid === sid)) return v;
  return { ...v, timeline: [...v.timeline, { t: "sub", sid, atMs }] };
}
function accLlm(v: RunView, e: any): RunView {
  const key = `${e.step ?? "?"}|${e.model ?? "?"}`;
  const prev = v.stats.llm[key] ?? { step: e.step ?? "?", model: e.model ?? "?", calls: 0, totalMs: 0, inTok: 0, outTok: 0 };
  const next = { ...prev, calls: prev.calls + 1, totalMs: prev.totalMs + (e.ms ?? 0),
    inTok: prev.inTok + (e.in_tok ?? 0), outTok: prev.outTok + (e.out_tok ?? 0) };
  return { ...v, stats: { ...v.stats, llm: { ...v.stats.llm, [key]: next } } };
}
function accStep(v: RunView, e: any): RunView {
  const key = e.step ?? "?";
  const prev = v.stats.steps[key] ?? { calls: 0, totalMs: 0 };
  const next = { calls: prev.calls + 1, totalMs: prev.totalMs + (e.ms ?? 0) };
  return { ...v, stats: { ...v.stats, steps: { ...v.stats.steps, [key]: next } } };
}
function accTool(v: RunView, tool: string, ok: boolean, accepted: number, rejected: number): RunView {
  const prev = v.stats.tools[tool] ?? { calls: 0, ok: 0, accepted: 0, rejected: 0 };
  const next = { ...prev, calls: prev.calls + 1, ok: prev.ok + (ok ? 1 : 0),
    accepted: prev.accepted + accepted, rejected: prev.rejected + rejected };
  return { ...v, stats: { ...v.stats, tools: { ...v.stats.tools, [tool]: next } } };
}

function subagentOutcome(raw: unknown): SubagentOutcome {
  return raw === "ok" || raw === "empty" || raw === "timeout"
    || raw === "failed" || raw === "hard_error" ? raw : "unknown";
}

function workerStopReason(raw: unknown): WorkerStopReason | undefined {
  return raw === "sufficient" || raw === "no_progress"
    || raw === "timeout" || raw === "tool_budget" ? raw : undefined;
}

function evidenceCards(raw: unknown): EvidenceCardView[] {
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((card): EvidenceCardView[] => {
    if (!card || typeof card !== "object") return [];
    const value = card as Record<string, unknown>;
    if (!Number.isInteger(value.card_no) || (value.card_no as number) < 1
        || typeof value.claim !== "string" || typeof value.support_quote !== "string") return [];
    return [{
      cardNo: value.card_no as number,
      claim: value.claim,
      supportQuote: value.support_quote,
      quoteTruncated: value.quote_truncated === true,
      sourceTitle: typeof value.source_title === "string" ? value.source_title : null,
      sourceUrl: typeof value.source_url === "string" ? value.source_url : null,
      publishedAt: typeof value.published_at === "string" ? value.published_at : null,
    }];
  });
}

const STATUS_REASON_LABELS = new Map<string, string>([
  ["unresolved_plan_nodes", "仍有研究步骤未完成"],
  ["report_empty", "报告内容为空"],
  ["recovered_worker_failure", "部分研究任务失败后已恢复"],
  ["cross_worker_audit_findings", "跨任务检查发现覆盖不足或冲突"],
  ["cross_worker_audit_skipped", "跨任务一致性与覆盖检查未完成"],
  ["shape_gate_failed", "报告结构校验未通过"],
  ["deadline_exhausted", "运行时限已用完"],
  ["research_round_budget_exhausted", "研究轮次预算已用完"],
  ["task_budget_exhausted", "任务预算已耗尽"],
  ["final_research_pass_stalled", "最终补查未取得进展"],
  ["final_research_pass_unactionable", "最终补查没有可执行任务"],
  ["plan_nodes_closed_retry", "部分研究步骤已达到重试上限"],
  ["assessment_contract_error", "节点验收回执格式错误"],
  ["writer_timeout", "报告成稿超时"],
  ["writer_failed", "报告成稿失败"],
  ["writer_timeout_fallback", "已降级为证据摘要（成稿超时）"],
  ["writer_failed_fallback", "已降级为证据摘要（成稿失败）"],
  ["writer_rewrite_timeout", "格式重写超时（保留首稿）"],
  ["writer_rewrite_failed", "格式重写失败（保留首稿）"],
]);

function reasonLabels(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const codes = raw.filter((value): value is string => typeof value === "string" && value.length > 0);
  return [...new Set(codes.map((code) => STATUS_REASON_LABELS.get(code) ?? code))];
}

function terminalReasonLabel(reason: string): string {
  if (reason.startsWith("assessment_contract_error:")) {
    return `验收回执格式错误：${reason.slice("assessment_contract_error:".length)}`;
  }
  if (reason.startsWith("blocked_by_dependencies:")) {
    return `等待上游 ${reason.slice("blocked_by_dependencies:".length)}`;
  }
  const labels = new Map<string, string>([
    ["closed_partial_retry_limit", "达到节点重试上限"],
    ["final_research_pass_unactionable", "最终补查无法生成任务"],
    ["deadline_exhausted", "运行时限已用完"],
    ["research_round_budget_exhausted", "研究轮次预算已用完"],
    ["task_budget_exhausted", "任务预算已用完"],
    ["unassessed", "尚未验收"],
    ["unresolved_partial", "仍为部分完成"],
    ["unresolved_blocked", "仍被阻塞"],
  ]);
  return labels.get(reason) ?? reason;
}

function terminalReasonDetails(raw: unknown): string[] {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  return Object.entries(raw as Record<string, unknown>).flatMap(([nodeId, reason]) =>
    typeof reason === "string" && reason.length > 0
      ? [`${nodeId}：${terminalReasonLabel(reason)}`]
      : []
  );
}

function doneBanner(e: any): NonNullable<RunView["banner"]> {
  const nEvidence = typeof e.n_evidence === "number" ? e.n_evidence : 0;
  const blockers = reasonLabels(e.completion_blockers);
  const warnings = reasonLabels(e.warnings);
  const unresolvedIds = Array.isArray(e.unresolved_node_ids)
    ? e.unresolved_node_ids.filter((v: unknown): v is string => typeof v === "string")
    : [];
  const terminalReasons = terminalReasonDetails(e.node_terminal_reasons);

  if (e.status === "done") {
    const warn = warnings.length > 0 ? ` · ${warnings.length} 条警告` : "";
    const warnDetail = warnings.length > 0 ? `（${warnings.join("、")}）` : "";
    return {
      kind: "done",
      text: `研究完成${warn}${warnDetail} · 证据 ${nEvidence} 条`,
    };
  }
  if (e.status === "partial") {
    const blockerDetail = blockers.length > 0
      ? `原因：${blockers.join("、")}`
      : "存在未覆盖或失败任务";
    const pending = unresolvedIds.length > 0 ? ` · 尚有 ${unresolvedIds.length} 个研究步骤未完成` : "";
    const terminal = terminalReasons.length > 0 ? ` · ${terminalReasons.join("；")}` : "";
    const warn = warnings.length > 0 ? ` · 另有 ${warnings.length} 条警告` : "";
    return {
      kind: "partial",
      text: `部分完成${pending} · ${blockerDetail}${terminal}${warn} · 证据 ${nEvidence} 条`,
    };
  }
  const rawStatus = typeof e.status === "string" && e.status.length > 0 ? `（${e.status}）` : "";
  return { kind: "unknown", text: `研究已结束 · 结果状态未知${rawStatus} · 证据 ${nEvidence} 条` };
}

// 全部 EventTypeName 都在场（Record 强制编译期穷尽——漏一个键编不过）。
const HANDLERS: Record<EventTypeName, Handler> = {
  scope: (v, e, at) => note({ ...bumpStage(v, "plan"), phaseText: `已收到：${e.query}` }, "scope", at, e),
  research_plan: (v, e, at) => {
    const initialTasks = Array.isArray(e.initial_tasks) ? e.initial_tasks : [];
    const researchTaskNodeIds = Object.fromEntries(initialTasks.flatMap((task: any) =>
      typeof task?.id === "string" && typeof task?.node_id === "string"
        ? [[task.id, task.node_id]] : []));
    return note({ ...bumpStage(v, "dispatch"), researchTaskNodeIds,
      planned: initialTasks.map((task: { objective?: unknown }) => task.objective)
        .filter((value: unknown): value is string => typeof value === "string") }, "research_plan", at, e);
  },
  report_plan: (v, e, at) => note(bumpStage(v, "reflect"), "report_plan", at, e),
  task_batch_dispatched: (v, e, at) => note({ ...bumpStage(v, "research"),
    phaseText: e.phase === "final_research_pass"
      ? `最终补查中 · ${e.count} 个任务并发执行`
      : `${e.phase === "initial" ? "首轮研究" : `第 ${displayRound(e.round_index)} 轮研究`} · ${e.count} 个任务并发执行中` }, "task_batch_dispatched", at, e),
  ready_set_computed: (v, e, at) => note({ ...v,
    phaseText: e.pending === 0 || e.n_tasks === 0
      ? "所有研究步骤已完成"
      : e.phase === "final_research_pass"
        ? "最终补查任务已准备"
        : `第 ${displayRound(e.round_index)} 轮任务已准备` }, "ready_set_computed", at, e),
  research_round_completed: (v, e, at) => note(v, "research_round_completed", at, e),
  nodes_assessed: (v, e, at) => note(v, "nodes_assessed", at, e),
  subagent_start: (v, e, at) => putAgent(withSubAnchor(v, e.sid, at), { ...agent(v, e),
    nodeId: inferredNodeId(v, e), roundIndex: e.round_index,
    status: "running", currentLabel: "启动", currentSinceMs: at }),
  subagent_tool_call: (v, e, at) => {
    const v1 = withSubAnchor(v, e.sid, at);
    const s = agent(v1, e);
    const call: ToolCall = { callNo: e.call_no, tool: e.tool, ok: e.ok, args: e.args_summary,
      result: e.result_summary, accepted: e.accepted, rejected: e.rejected,
      rejectReasons: e.reject_reasons, evidenceTotal: e.evidence_total, atMs: at,
      links: e.links, url: e.url, nExcerpts: e.n_excerpts };
    const seenCardNos = new Set(s.cards.map((card) => card.cardNo));
    const newCards = evidenceCards(e.saved_cards).filter((card) => {
      if (seenCardNos.has(card.cardNo)) return false;
      seenCardNos.add(card.cardNo);
      return true;
    });
    const v2 = putAgent(v1, { ...s, nodeId: e.node_id ?? s.nodeId, mode: "loop", status: "running",
      calls: [...s.calls, call], cards: [...s.cards, ...newCards], evidenceTotal: e.evidence_total,
      currentLabel: `第 ${e.call_no} 次 · ${toolLabel(e.tool)}`, currentSinceMs: at });
    return accTool(v2, e.tool, e.ok, e.accepted ?? 0, e.rejected ?? 0);
  },
  subagent_done: (v, e, at) => {
    const v1 = withSubAnchor(v, e.sid, at);
    const s = agent(v1, e);
    return putAgent(v1, { ...s, nodeId: e.node_id ?? s.nodeId, status: "done", toolCallsDone: e.tool_calls,
      outcome: subagentOutcome(e.status), evidenceTotal: e.evidence_count,
      stopReason: workerStopReason(e.stop_reason),
      summary: typeof e.summary === "string" ? e.summary : s.summary,
      currentLabel: undefined });
  },
  step_done: (v, e) => accStep(v, e),
  llm_call: (v, e, at) => {
    const v2 = accLlm(v, e);
    const call: LlmCall = { step: e.step, model: e.model, ms: e.ms, inTok: e.in_tok,
      outTok: e.out_tok, input: e.input, output: e.output, ioComplete: e.io_complete,
      sid: e.sid, workerIteration: e.worker_iteration };
    if (e.sid) {
      const s = agent(v2, e);
      return putAgent(v2, { ...s, llmCalls: [...s.llmCalls, call] });
    }
    // 编排级调用直接编入研究流:调试档在对应阶段原位展开,不再屯到页尾
    return { ...v2, timeline: [...v2.timeline, { t: "llm", call, atMs: at }] };
  },
  collect: (v, e, at) => note({ ...bumpStage(v, "reflect"),
    evidenceCount: typeof e.after === "number" ? e.after : v.evidenceCount }, "collect", at, e),
  cross_worker_audit: (v, e, at) => note(bumpStage(v, "reflect"), "cross_worker_audit", at, e),
  writing: (v, e, at) => note({ ...bumpStage(v, "write"),
    evidenceCount: typeof e.n_evidence === "number" ? e.n_evidence : v.evidenceCount,
    phaseText: `综合 ${e.n_evidence} 条证据写报告` }, "writing", at, e),
  shape_gate: (v, e, at) => note(bumpStage(v, "check"), "shape_gate", at, e),
  report_md: (v, e) => ({ ...v, reportMd: e.markdown, savedPath: e.saved_path }),
  done: (v, e, at) => note({ ...bumpStage(v, "done"), endedAtMs: at,
    evidenceCount: typeof e.n_evidence === "number" ? e.n_evidence : v.evidenceCount,
    banner: doneBanner(e) }, "done", at, e),
  error: (v, e, at) => note({ ...v, endedAtMs: at, banner: { kind: "error", text: e.message ?? "未知错误" } }, "error", at, e),
  cancelled: (v, e, at) => note({ ...v, endedAtMs: at, banner: { kind: "cancelled", text: "已取消（部分过程与证据已保留）" } }, "cancelled", at, e),
  end: (v) => v,
  json_retry: (v, e) => ({ ...v, phaseText: `结构化输出重试（${e.reason} 第 ${e.attempt} 次）` }),
};

export function reduce(v: RunView, evt: any, atMs: number): RunView {
  if (typeof evt.seq === "number") {
    if (evt.seq <= v.lastSeq) return v;
    v = { ...v, lastSeq: evt.seq };
  }
  if (v.startedAtMs === undefined) v = { ...v, startedAtMs: atMs };
  const h = (HANDLERS as Record<string, Handler>)[evt.type];
  return h ? h(v, evt, atMs) : v;
}
