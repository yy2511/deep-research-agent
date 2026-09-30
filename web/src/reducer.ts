import { type EventTypeName } from "./events.gen";
import type { DelegationView,LeadMessage,LlmCall,ResearchInputSnapshot,ResearchScope,ResearchUserMessage,RunView,SimpleRunView,TimelineItem,ToolCall } from "./types";

/** HTTP 回执、SSE 与磁盘快照共享归并；迟到的 queued/open 不能覆盖较新状态。 */
export function mergeResearchInput(v: RunView, snapshot: ResearchInputSnapshot): RunView {
  const states = { open: 0, closing: 1, closed: 2 };
  const statuses = { queued: 0, included: 1, undelivered: 2 };
  const rows = new Map((v.userMessages ?? []).map(m => [m.message_id, m]));
  const timeline = [...v.timeline];
  for (const message of snapshot.messages ?? []) {
    const old = rows.get(message.message_id);
    if (old && statuses[old.status] > statuses[message.status]) continue;
    rows.set(message.message_id, { ...old, ...message });
    if (!timeline.some(i => i.t === 'user' && i.messageId === message.message_id)) {
      timeline.push({ t: 'user', messageId: message.message_id, atMs: message.at * 1000 });
    }
  }
  const inputState = v.inputState && states[v.inputState] > states[snapshot.state]
    ? v.inputState : snapshot.state;
  return { ...v, inputState, userMessages: [...rows.values()].sort((a, b) => a.revision - b.revision), timeline,
    ...(snapshot.error ? { inputError: snapshot.error } : {}) };
}

function closeResearchInput(v: RunView): RunView {
  return mergeResearchInput(v, { state: 'closed', messages: (v.userMessages ?? []).map(m =>
    m.status === 'queued' ? { ...m, status: 'undelivered', reason: '运行已结束，未记录消息进入主线请求' } : m) });
}

// ---------------------------------------------------------------------------
// 视图合并与导航判定：与 reduce 同属"事件/结果 → 视图"的纯逻辑层。
// ---------------------------------------------------------------------------

/** `/api/runs/{id}/simple` 的载荷里我们真正消费的字段（其余按需透传）。 */
export interface SimplePayload {
  artifacts?: SimpleRunView["artifacts"];
  artifact_errors?: SimpleRunView["artifactErrors"];
  execution_summary?: SimpleRunView["executionSummary"];
  query?: string;
  draft?: string;
  draft_rejected?: boolean;
  /** 被拒发布的原因，服务端写好的整句；没被拒就是空。前端不再自己编文案。 */
  draft_rejected_note?: string;
  markdown?: string;
  sources?: SimpleRunView["sources"];
  cards?: SimpleRunView["cards"];
  warnings?: string[];
  stop_reason?: string;
  delivery_status?: string;
  research_coverage?: Record<string, string>;
  coverage_reasons?: Record<string, string[]>;
  requirements?: Array<{ id: string; text: string; output_formats?: string[] }>;
  effective_query?: string;
  scope_history?: ResearchScope[];
  plan_sync_status?: 'reported' | 'missing' | null;
  gaps?: string[];
  usage?: SimpleRunView["usage"];
}

export interface RunMeta {
  status?: string;
  started_at?: number;
  finished_at?: number;
  report_md?: string;
  replay_compatible?: boolean;
  replay_error?: string | null;
  /** 该 run 的元数据是判定 simple/plan 的唯一依据，不能用"下次新建研究"的模式设置。 */
  config_summary?: { mode?: string } | null;
}

export function viewTimes(meta: RunMeta | null | undefined) {
  return {
    startedAtMs: meta?.started_at ? meta.started_at * 1000 : undefined,
    endedAtMs: meta?.finished_at ? meta.finished_at * 1000 : undefined,
  };
}

/** 结果状态 → 横幅；核查失败必须如实显示为失败，不能含糊成"受限"。 */
export function resultBanner(payload: SimplePayload): NonNullable<RunView["banner"]> {
  const delivery = payload.delivery_status;
  const kind = delivery === "delivered" ? "done" : delivery === "failed" ? "error" : "partial";
  return {
    kind,
    text: [
      `停止原因 ${payload.stop_reason || "未记录"}`,
      `交付 ${delivery || "未交付"}`,
    ].join(" · "),
  };
}

/**
 * 把最终结果**合并**进当前视图。
 *
 * 关键契约：不能重置整份视图。实时链路里过程消息/工具卡/时间线已经在视图里，
 * 加载 simple.json 只补充结果字段；用 initialView() 覆盖会让用户看到的过程在结束
 * 那一刻整体消失。
 */
export function mergeSimpleResult(
  base: RunView,
  payload: SimplePayload,
  meta: RunMeta | null,
  opts: { executedToolCalls?: number; preserveEndedAt?: boolean } = {},
): RunView {
  const times = viewTimes(meta);
  const simple: SimpleRunView = {
    ...base.simple,
    mode: "simple",
    artifacts: payload.artifacts ?? [],
    artifactErrors: payload.artifact_errors ?? [],
    executionSummary: payload.execution_summary ?? {},
    phase: "finished",
    stopReason: payload.stop_reason,
    delivery: payload.delivery_status,
    coverage: payload.research_coverage,
    coverageReasons: payload.coverage_reasons ?? base.simple?.coverageReasons,
    requirements: payload.requirements ?? base.simple?.requirements,
    effectiveQuery: payload.effective_query ?? base.simple?.effectiveQuery,
    scopeHistory: payload.scope_history ?? base.simple?.scopeHistory,
    planSyncStatus: payload.plan_sync_status ?? base.simple?.planSyncStatus,
    gaps: payload.gaps ?? base.simple?.gaps,
    warnings: payload.warnings ?? [],
    draft: payload.draft,
    draftRejected: payload.draft_rejected,
    draftRejectedNote: payload.draft_rejected_note,
    markdown: payload.markdown,
    sources: payload.sources ?? [],
    cards: payload.cards ?? [],
    cardCount: (payload.cards ?? []).length,
    query: payload.query,
    usage: payload.usage ?? {},
    toolCalls: opts.executedToolCalls,
  };
  const startedAtMs = base.startedAtMs ?? times.startedAtMs;
  const endedAtMs = opts.preserveEndedAt && base.endedAtMs !== undefined
    ? base.endedAtMs
    : times.endedAtMs ?? base.endedAtMs;
  return {
    ...base,
    stage: "done",
    startedAtMs,
    endedAtMs,
    simple,
    // 报告只渲染一份：正式报告用 meta/载荷里的 markdown，交付正文不另起一段。
    reportMd: meta?.report_md ?? payload.markdown,
    banner: resultBanner(payload),
  };
}

/** 事件流缺失时（旧运行没有 events.jsonl）的诚实视图：保留结果，明确说没有过程记录。 */
export function missingProcessNotice(v: RunView, detail: { reason: string }): RunView {
  return {
    ...v,
    processMissing: detail.reason,
    endedAtMs: v.endedAtMs ?? Date.now(),
  };
}

/** 一份运行该走哪条打开路径（刷新恢复用；判定只看 run 自己的元数据）。 */
export type OpenPlan =
  | { kind: "live"; runId: string }
  | { kind: "replay"; runId: string; mode: "instant" | "paced" }
  | { kind: "result-only"; runId: string; reason: string };

export function planOpen(
  runId: string,
  meta: RunMeta | null,
  opts: { hasEvents?: boolean } = {},
): OpenPlan {
  if (meta?.status === "running") return { kind: "live", runId };
  if (meta?.replay_compatible === false) {
    // 旧事件协议不可回放：仍然打开它的结果，并说清"没有过程记录"。
    return { kind: "result-only", runId, reason: meta?.replay_error || "旧事件协议不可回放" };
  }
  if (opts.hasEvents === false) {
    return { kind: "result-only", runId, reason: "本次运行没有过程事件记录" };
  }
  if (meta?.replay_compatible === true) return { kind: "replay", runId, mode: "instant" };
  return { kind: "result-only", runId, reason: "本次运行没有可回放的过程事件" };
}

/**
 * 迟到响应守卫：任何跨 await 的导航动作都先领一个批次号，提交前核对。
 *
 * 为什么不能只比较 run id：A→B→A 时，A 的旧响应与新的 A 选择 run id 完全相同，
 * 但视图基底（B 的过程）不该被旧响应覆盖。批次号能区分"同一条 run 的第几次选择"。
 */
export class BatchGuard {
  private batch = 0;
  private currentRun: string | null = null;

  /** 开始一次新的导航/加载，作废此前所有未提交的动作。 */
  begin(runId: string | null): number {
    this.batch += 1;
    this.currentRun = runId;
    return this.batch;
  }

  /**
   * 拿当前批次（同一次导航里的第二步及以后用这个）。
   *
   * 为什么需要：一次历史打开既有"读 meta"也有"读结果"，它们是同一次导航的两段；
   * 第二段再 begin() 会把第一段回放派发出的批次作废。
   */
  currentOrBegin(runId: string | null): number {
    if (this.currentRun !== runId) this.batch += 1;
    this.currentRun = runId;
    return this.batch;
  }

  /** 该批次是否仍是最新一次导航，且目标 run 未被换走。 */
  isCurrent(batch: number, runId?: string): boolean {
    if (batch !== this.batch) return false;
    return runId === undefined || runId === this.currentRun;
  }

  get runId(): string | null {
    return this.currentRun;
  }
}

type Handler = (v: RunView, e: any, atMs: number) => RunView;

export function initialView(): RunView {
  return {
    stage: "plan",
    timeline: [],
    lastSeq: -1,
  };
}

/** 编排叙事进编年流;事件级元字段剥掉,业务载荷原样入卡供渲染。 */
function note(v: RunView, kind: string, atMs: number, e?: any): RunView {
  const { type: _type, ts: _ts, seq: _seq, run_id: _rid, ...payload } = e ?? {};
  // 条目标自身的 atMs 也要带上：轨迹的时间轴靠它算相对时间，只放进 card 里读不到。
  return {
    ...v,
    timeline: [...v.timeline, { t: "note", atMs, card: { kind, atMs, ...payload } }],
  };
}

function mergeLeadMessage(v: RunView, e: any, at: number): RunView {
  const msgId = typeof e.msg_id === "string" ? e.msg_id : "";
  if (!msgId) return v;
  const list = v.leadMessages ?? [];
  const anchor: TimelineItem = { t: "msg", msgId, atMs: at };
  const idx = list.findIndex((m) => m.msgId === msgId);
  const prev = idx >= 0 ? list[idx] : undefined;
  const text = typeof e.text === "string" ? e.text : "";
  const partSeq = Number.isInteger(e.part_seq) ? (e.part_seq as number) : -1;
  let next: LeadMessage;
  if (prev) {
    if (e.mode === "replace") {
      // 权威全文：内容以它为准，此前因断流留下的缺口与"分片不全"都被它修复。
      next = { ...prev, text, complete: e.complete === true, lastPartSeq: -1, gap: false };
    } else if (e.mode === "complete") {
      // 结束标记自带 part_seq = 已发出的分片总数：据此判断是否**缺了尾部**。
      // 只按 mid-stream 跳号判断不够——最后一片丢了，之后没有更大的序号来暴露它。
      const expected = partSeq >= 0 ? partSeq : null;
      const short = expected !== null && !prev.replaced && prev.lastPartSeq + 1 < expected;
      next = { ...prev, complete: e.interrupted !== true, gap: prev.gap || short,
        missing: short ? expected - (prev.lastPartSeq + 1) : prev.missing };
    } else if (text) {
      // 首个分片就不是 0，或与上一个分片之间跳号：事件流本身缺片，如实标记。
      const gap = partSeq >= 0 && (prev.lastPartSeq < 0 ? partSeq > 0 : partSeq > prev.lastPartSeq + 1);
      const lastPartSeq = partSeq >= 0 ? Math.max(prev.lastPartSeq, partSeq) : prev.lastPartSeq;
      next = { ...prev, text: prev.text + text, lastPartSeq, gap: prev.gap || gap };
    } else return v;
  } else {
    if (e.mode === "complete") return v;
    if (e.mode === "replace") {
      next = { msgId, turn: typeof e.turn === "number" ? e.turn : 0, text,
        complete: e.complete === true, role: e.role ?? "lead", kind: e.kind ?? "note",
        label: e.label ?? "", atMs: at, lastPartSeq: -1, gap: false, replaced: true };
    } else {
      if (!text) return v;
      next = { msgId, turn: typeof e.turn === "number" ? e.turn : 0, text,
        complete: false, role: e.role ?? "lead", kind: e.kind ?? "note",
        label: e.label ?? "", atMs: at, lastPartSeq: partSeq,
        // 首个到达的分片不是 0 就是已经缺了开头
        gap: partSeq > 0, replaced: false };
    }
  }
  next = { ...next, callId: e.call_id ?? prev?.callId, interrupted: e.interrupted ?? prev?.interrupted };
  const leadMessages = idx >= 0 ? list.map((m, i) => (i === idx ? next : m)) : [...list, next];
  // 锚点只在首次出现时插入：之后同一 msg_id 的增量原地更新正文，不改变它在流里的位置。
  const timeline = idx >= 0 ? v.timeline : [...v.timeline, anchor];
  return { ...v, leadMessages, timeline };
}

/** simple 工具卡身份：`turn:call_id`（后端保证同一批 tool_calls 内 id 唯一）。 */
function toolKey(e: any): string {
  const callId = typeof e.call_id === "string" ? e.call_id : "";
  const turn = typeof e.turn === "number" ? e.turn : 0;
  if (callId) return e.delegation_id ? `${e.parent_call_id ?? ""}:${e.delegation_id}:${turn}:${callId}` : `${turn}:${callId}`;
  // 旧协议没有 call_id：退到本回合内的调用序号，仍然是稳定身份（同一调用重复到达不会新建卡）。
  return `${turn}:#${typeof e.call_no === "number" ? e.call_no : 0}`;
}

/** 生命周期由 delegation_updated 提供；工具回执只补该子任务自己的轨迹与证据数。 */
function mergeDelegation(v: RunView, e: any, at: number): RunView {
  const simple = v.simple;
  if (!simple || e.phase !== "done") return v;
  const list = [...(simple.delegations ?? [])];
  const index = list.findIndex(d => d.id === e.delegation_id && d.parentCallId === e.parent_call_id);
  if (index < 0) return v;
  const base = list[index];
  const key = toolKey(e);
  const cardIds = Array.isArray(e.display?.card_ids)
    ? (e.display.card_ids as unknown[]).filter((x): x is string => typeof x === "string")
    : [];
  list[index] = {
    ...base,
    cardCount: base.cardCount + (base.steps.some(s => s.key === key) ? 0 : cardIds.length),
    steps: [...base.steps.filter(s => s.key !== key), {
      key, atMs: at,
      tool: typeof e.tool === "string" ? e.tool : "",
      ok: e.ok === true,
      summary: typeof e.args_summary === "string" ? e.args_summary : "",
      ...(cardIds.length ? { cardIds } : {}),
    }],
  };
  return { ...v, simple: { ...simple, delegations: list } };
}

function mergeDelegationEvent(v: RunView, e: any, at: number): RunView {
  const t=e.task;
  if (!t || typeof t.id!=='string' || typeof t.parent_call_id!=='string') return v;
  const simple=v.simple ?? {mode:'simple' as const,phase:'research'};
  const list=[...(simple.delegations ?? [])];
  const i=list.findIndex(d=>d.id===t.id && d.parentCallId===t.parent_call_id);
  const base:DelegationView=i>=0 ? list[i] : {id:t.id,parentCallId:t.parent_call_id,goal:t.goal ?? '',steps:[],cardCount:0,gaps:0};
  let next=base;
  if(e.kind==='state' || e.kind==='result') {
    next={...next,goal:t.goal ?? base.goal,status:t.status,
      startedAtMs:typeof t.started_at==='number' ? t.started_at*1000 : base.startedAtMs,
      endedAtMs:typeof t.ended_at==='number' ? t.ended_at*1000 : base.endedAtMs};
    const result=t.result ?? e.result;
    if(result) {
      const cardIds=Array.isArray(result.card_ids) ? [...new Set<string>(result.card_ids.filter((id:unknown)=>typeof id==='string'))] : undefined;
      next={...next,result:{findings:result.findings ?? '',gaps:result.gaps ?? [],
        cardIds,summaryAvailable:result.summary_available,error:result.error},
        cardCount:cardIds ? cardIds.length : base.cardCount,gaps:(result.gaps ?? []).length};
    }
  }
  if(e.kind==='message' && typeof e.message_id==='string' && !(base.messages ?? []).some(m=>m.id===e.message_id)) {
    next={...next,messages:[...(base.messages ?? []),{id:e.message_id,sender:e.sender,target:e.target,text:e.message ?? '',atMs:at}]};
  }
  if(i>=0) list[i]=next; else list.push(next);
  return {...v,simple:{...simple,delegations:list}};
}

function mergeSimpleTool(v: RunView, e: any, at: number): RunView {
  const key = toolKey(e);
  const prev = v.simpleTools?.[key];
  const phase = e.phase === "running" ? "running" : "done";
  const call: ToolCall = {
    callNo: typeof e.call_no === "number" ? e.call_no : prev?.callNo ?? 0,
    tool: typeof e.tool === "string" ? e.tool : prev?.tool ?? "",
    // 未结束就不是成功：running 期间 ok 恒为 false，只有 done 才按事件取值。
    ok: phase === "done" ? e.ok === true : false,
    args: typeof e.args_summary === "string" ? e.args_summary : prev?.args ?? "",
    result: typeof e.result_summary === "string" ? e.result_summary : prev?.result ?? "",
    // 时间戳保持首次出现（开始时）——卡片标签显示"什么时候开始做的"。
    atMs: prev?.atMs ?? at,
    links: e.links ?? prev?.links,
    url: e.url ?? prev?.url,
    phase,
    status: typeof e.status === "string" ? e.status : prev?.status,
    callId: typeof e.call_id === "string" ? e.call_id : prev?.callId,
    delegationId: typeof e.delegation_id === "string" && e.delegation_id
      ? e.delegation_id
      : prev?.delegationId,
    parentCallId: typeof e.parent_call_id === "string" ? e.parent_call_id : prev?.parentCallId,
    role: typeof e.role === "string" ? e.role : prev?.role,
    turn: typeof e.turn === "number" ? e.turn : prev?.turn,
    detailSeq: e.detail_seq ?? prev?.detailSeq, argsFull: e.arguments_raw ?? prev?.argsFull, resultFull: e.result_full ?? prev?.resultFull, ioComplete: e.io_complete ?? prev?.ioComplete,
    display: (e.display && typeof e.display === "object" ? e.display : undefined) ?? prev?.display,
  };
  const simpleTools = { ...(v.simpleTools ?? {}), [key]: call };
  const timeline = prev ? v.timeline : [...v.timeline, { t: "tool" as const, key, atMs: at }];
  return { ...v, simpleTools, timeline };
}

// 全部 EventTypeName 都在场（Record 强制编译期穷尽——漏一个键编不过）。
const HANDLERS: Record<EventTypeName, Handler> = {
  skill_loaded: (v, e, at) => note(v, "skill_loaded", at, e),
  subagent_tool_call: (v, e, at) => mergeDelegation(mergeSimpleTool(v, e, at), e, at),
  step_done: (v) => v,
  // 每次 HTTP 尝试的观测随事件持久化，汇总从 run.meta.stats.cache 读取；不重复累计调用。
  llm_usage: (v, e) => ({...v, usageAttempts: [...(v.usageAttempts ?? []).filter(r=>r.request_id!==e.request_id), e]}),
  llm_call: (v, e, at) => {
    const call: LlmCall = { detailSeq:e.detail_seq, callId:e.call_id, delegationId:e.delegation_id, role:e.usage_role, tools:e.tools, step:e.step, model:e.model, ms:e.ms, inTok:e.in_tok, outTok:e.out_tok, input:e.input, output:e.output, ioComplete:e.io_complete, sid:e.sid };
    return {...v, timeline:[...v.timeline,{t:"llm",call,atMs:at}]};
  },
  report_md: (v, e) => ({ ...v, reportMd: e.markdown }),
  error: (v, e, at) => note({ ...v, endedAtMs: at, banner: { kind: "error", text: e.message ?? "未知错误" } }, "error", at, e),
  cancelled: (v, e, at) => note({ ...v, endedAtMs: at, banner: { kind: "cancelled", text: "已取消（部分过程与证据已保留）" } }, "cancelled", at, e),
  end: (v, _e, at) => closeResearchInput({ ...v, stage: "done", endedAtMs: v.endedAtMs ?? at }),
  json_retry: (v) => v,
  // 单 Lead 最小闭环：只展示它自己的进度与四状态，不派生任何 Worker 叙事。
  context_compaction: (v, e, at) => {
    const existing=v.timeline.findIndex(item=>item.t==='note' && item.card.kind==='context_compaction' && item.card.checkpoint_id===e.checkpoint_id);
    if(existing<0) return note(v,"context_compaction",at,e);
    return {...v,timeline:v.timeline.map((item,i)=>i===existing && item.t==='note'?{...item,card:{...item.card,...e}}:item)};
  },
  evidence_judged: (v) => ({...v,simple:{mode:'simple',phase:'research',...v.simple,
    evidenceRevision:(v.simple?.evidenceRevision??0)+1}}),
  research_status: (v, e, at) => {
    const prev = v.simple;
    const running = e.phase !== "finished";
    const next = {
      ...prev,
      mode: "simple" as const,
      phase: e.phase ?? prev?.phase ?? "starting",
      toolCalls: e.tool_calls ?? prev?.toolCalls,
      cardCount: e.cards ?? prev?.cardCount,
      stopReason: e.stop_reason ?? prev?.stopReason ?? null,
      delivery: e.delivery ?? prev?.delivery ?? null,
      coverage: e.coverage ?? prev?.coverage ?? null,
      coverageReasons: e.coverage_reasons ?? prev?.coverageReasons,
      requirements: e.requirements ?? prev?.requirements,
      effectiveQuery: e.effective_query ?? prev?.effectiveQuery,
      scopeHistory: e.scope_history ?? prev?.scopeHistory,
      planSyncStatus: e.plan_sync_status ?? prev?.planSyncStatus,
      gaps: e.gaps ?? prev?.gaps,
      warnings: e.warnings ?? prev?.warnings,
    };
    // 起始/终态事件是过程流里的一条状态行；中途的进度只更新顶栏油表，不再往流里塞行。
    if (!running || !prev) {
      return note({ ...v, simple: next }, "research_status", at, e);
    }
    return { ...v, simple: next };
  },
  // Lead 的真实过程说明：只按 msg_id 归并增量，不派生任何文案。
  delegation_updated: (v, e, at) => mergeDelegationEvent(v,e,at),
  lead_message: (v, e, at) => mergeLeadMessage(v, e, at),
  agent_reasoning: (v,e,at) => mergeLeadMessage(v,{...e,kind:"reasoning"},at),
  research_input: (v, e) => mergeResearchInput(v, { state: e.state ?? v.inputState ?? 'open',
    messages: e.message ? [e.message as ResearchUserMessage] : e.messages ?? [] }),
  // 运行期自报路线：**全量快照**，直接替换（diff 只用于说明"这次改了什么"，不参与展示计算）。
  research_plan_updated: (v, e, at) => {
    const items = Array.isArray(e.items) ? e.items : [];
    const note = typeof e.note === "string" ? e.note : "";
    const previous = v.simple?.researchPlan?.items ?? [];
    return { ...v, simple: {
      ...(v.simple ?? { mode: "simple" as const, phase: "research" }),
      researchPlan: { items, note, diff: e.diff ?? null },
      effectiveQuery: e.scope?.goal ?? v.simple?.effectiveQuery,
      requirements: e.scope?.requirements ?? v.simple?.requirements,
      scopeHistory: e.scope ? [...(v.simple?.scopeHistory ?? []).filter(s => s.revision !== e.scope.revision), e.scope]
        : v.simple?.scopeHistory,
      planHistory: [...(v.simple?.planHistory ?? []), { atMs: at, items, previous, note }],
    }};
  },
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
