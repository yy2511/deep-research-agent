export type Stage = "plan" | "research" | "done";
export interface LlmRetry {
  reason: string;
  attempt: number;
  exhausted?: boolean;
  rawHead?: string;
}
export interface LlmCall { detailSeq?: number; callId?: string; delegationId?: string; role?: string; tools?: unknown[]; step: string; model?: string; ms: number; inTok?: number; outTok?: number; input?: string; output?: string; ioComplete?: boolean; sid?: string; retries?: LlmRetry[] }
export interface ToolCall { detailSeq?: number; argsFull?: string; resultFull?: string; ioComplete?: boolean; callNo: number; tool: string; ok: boolean; args: string; result: string; atMs: number;
  /** 后端附带的结构化来源；缺失时可从当前工具结果摘要中提取可确认的信息。 */
  links?: { title: string; url: string }[]; url?: string;
  /** simple 工具卡的生命周期：running=已开始执行，done=已结束。 */
  phase?: "running" | "done";
  /** simple：终态语义，区分成功 / 失败 / 被拒收 / 取消。 */
  status?: string;
  /** 与 turn 一起唯一定位一次调用（后端 call_id）。 */
  callId?: string;
  /** 非空表示这条是**子 Agent** 的动作（sub1/sub2…）：主线流里不画它，它归自己的视图。 */
  delegationId?: string;
  role?: string;
  parentCallId?: string;
  turn?: number;
  /** 后端从**完整回执**截取的有界结构化展示字段（旧事件没有这个字段）。 */
  display?: Record<string, any> }
export interface OrchCard { kind: string; atMs: number; [k: string]: unknown }
/** 按到达顺序保存过程锚点；消息与工具后续更新不改变位置。 */
export type TimelineItem =
  | { t: "note"; atMs: number; card: OrchCard }
  | { t: "llm"; call: LlmCall; atMs: number }
  | { t: "msg"; msgId: string; atMs: number }
  | { t: "user"; messageId: string; atMs: number }
  | { t: "tool"; key: string; atMs: number };
/** 单 Lead 最小闭环（simple）的四状态与材料；零 Worker，不借用 Worker 指标。 */
/** 运行期自报路线的一条；与开跑前确认的那份同形状（EXPERIMENT_PLAN §7 第 1 项）。 */
export interface PlanItemView {
  id: string;
  text: string;
  status: "todo" | "doing" | "done" | "dropped";
}

/** 服务端算出的"这次改了什么"——不靠模型自述。 */
export interface PlanDiffView {
  added: string[];
  changed: string[];
  removed: string[];
}

/** 一次派发的子 Agent：主线里显示成一行引用，完整轨迹在它自己的视图里（§7 第 2 项）。 */
export interface DelegationView {
  messages?: {id:string; sender:string; target:string; text:string; atMs:number}[];
  result?: {findings:string; gaps:string[]; cardIds?:string[]; summaryAvailable?:boolean;
    error?:{stage:string; type:string; message:string; call_id?:string}|null};
  /** `sub1` / `sub2`…，与后端 `DelegationManager` 的编号一致。 */
  id: string;
  /** 子任务目标，来自 delegation_updated 生命周期事件。 */
  goal: string;
  /** 子任务状态（queued / running / completed / …），来自生命周期事件。 */
  status?: string | null;
  /** 发起这次派发的主线调用 id。 */
  parentCallId: string;
  startedAtMs?: number;
  endedAtMs?: number;
  /** 它自己的工具调用（只记终态，避免每步出现两次）。 */
  steps: { key?: string; atMs: number; tool: string; ok: boolean; summary: string; cardIds?: string[] }[];
  cardCount: number;
  gaps: number;
}

export interface ArtifactFile {
  artifact_id: string; title: string; filename: string; format: string; mime: string; size: number; sha256: string; kind: string; requirement_ids: string[];
}
export interface SimpleRunView {
  artifacts?: ArtifactFile[];
  artifactErrors?: Array<{message:string;requirement_id?:string;format?:string}>;
  executionSummary?: Record<string,unknown>;
  mode: "simple";
  phase: string;
  toolCalls?: number;
  cardCount?: number;
  evidenceRevision?: number;
  stopReason?: string | null;
  delivery?: string | null;
  coverage?: Record<string, string> | null;
  coverageReasons?: Record<string, string[]>;
  requirements?: Array<{ id: string; text: string; output_formats?: string[] }>;
  effectiveQuery?: string;
  scopeHistory?: ResearchScope[];
  /** 主线交稿是否显式提交最终路线；旧记录缺失时不推断。 */
  planSyncStatus?: 'reported' | 'missing' | null;
  gaps?: string[];
  /** 当前路线（**最新快照**，不是时序事件）：它只用于展示进度与方向，不参与完成度。 */
  researchPlan?: { items: PlanItemView[]; note: string; diff: PlanDiffView | null } | null;
  /** 本run 派发过的子 Agent（按 delegation_id 归组）。 */
  delegations?: DelegationView[];
  planHistory?: { atMs: number; items: PlanItemView[]; previous: PlanItemView[]; note: string }[];
  warnings?: string[];
  draft?: string;
  draftRejected?: boolean;
  /** 被拒发布的原因，服务端写好的整句（引用门 / 核查是两件不同的事）。 */
  draftRejectedNote?: string;
  markdown?: string;
  sources?: SimpleSource[];
  cards?: SimpleCard[];
  query?: string;
  usage?: Record<string, { input?: number; output?: number; responses?: number; missing_usage?: number }>;
}
export interface SimpleSource { origin?: {run_id:string;turn_index:number;source_ref?:string}; revalidated_at?:number|null; fetched_at?:number;
  computation?: {run_id:string;execution_id:string;tool_call_id:string;command:string;cwd:string;exit_code:number;
    declared_source_refs:string[];stdout_truncated:boolean;stderr:string;
    inputs:Array<{run_id:string;source_ref:string;title:string;url:string;path:string;sha256:string;size:number;download?:unknown;document?:unknown;computation?:boolean}>;
    workspace_steps:Array<{tool:string;arguments:Record<string,unknown>;ok:boolean}>} | null;
  mcp?: {server_id:string;server_name:string;tool:string;receipt_id:string;run_id?:string;format?:string;uri?:string;tool_call_id?:string}|null;
  ref: string; url: string; title: string; published_at?: string | null;
  snapshot_kind: string; raw_chars: number; truncated?: boolean; content_sha256?: string;
  download?: { filename:string; encoding:string; original_sha256:string; size:number; final_url:string } | null;
  document?: { document_id: string; version_id: string; file_name: string; suffix: string;
    pages: Array<{page:number;start:number;end:number}>; warnings?: string[] } | null;
}
export interface EvidenceJudgmentData {
  attempts?: Array<{number:number;status:string;error_code?:string|null;elapsed_ms?:number;http_status?:number|null;retryable?:boolean;retry_after_s?:number|null}>;
  queue_ms?: number; retry_stop_reason?: string|null;
  status: 'pending' | 'complete' | 'unavailable' | 'error' | 'timeout' | 'cancelled';
  relation?: 'supported' | 'contradicted' | 'insufficient' | null;
  model?: string | null; requested_model?: string; prompt_version?: string;
  confidence?: number | null; probabilities?: Record<string,number> | null;
  error_code?: string | null; elapsed_ms?: number; checked_at?: number | null;
  attempted?: boolean; usage?: {input_tokens:number;output_tokens:number} | null; usage_unknown?: boolean;
}
export interface EvidenceQuote {
  source_kind?: string;
  quote: string; source_ref: string; source_url?: string;
  source_title?: string | null; location?: string; published_at?: string | null;
  /** 去原网页并高亮这段引用的链接（浏览器 text fragment）；生成不出来就是空串。 */
  deep_link?: string;
  submitted_quote?: string | null;
  context?: Array<{text:string;location:string;kind:'nearby'|'structure'}>;
}
export interface SimpleCard extends EvidenceQuote { origin?: {run_id:string;turn_index:number;card_id:string};
  judgment?: EvidenceJudgmentData | null;
  card_id: string; claim: string; quotes?: EvidenceQuote[];
}
export interface UsageAttempt {
 request_id:string; call_id?:string; delegation_id?:string; usage_role?:string; model?:string; provider?:string;
 attempt?:number; input_tokens?:number|null; output_tokens?:number|null; cache_read_tokens?:number|null;
 cache_miss_tokens?:number|null; cache_status?:string; first_token_ms?:number|null; first_content_ms?:number|null;
 duration_ms?:number; balance_charge_usd?:number|null; outcome?:string; error_type?:string|null;
 request_parameters?:Record<string,unknown>; request_messages?:Array<Record<string,unknown>>;
 request_tools?:unknown[]; io_complete?:boolean; raw_usage?:unknown; [key:string]:unknown;
}
export interface RunView { usageAttempts?:UsageAttempt[]; stage: Stage;
  replayOnly?: boolean;
  /** 自主研究的进度与结果状态。 */
  simple?: SimpleRunView; startedAtMs?: number; endedAtMs?: number;
  timeline: TimelineItem[]; reportMd?: string;
  /** simple 路径的过程消息：Lead 在执行当时的普通公开说明（阶段 2 渲染，阶段 1 先如实入流）。 */
  leadMessages?: LeadMessage[];
  userMessages?: ResearchUserMessage[];
  inputState?: ResearchInputState;
  inputError?: string;
  /** simple 路径的工具卡，按 `turn:call_id` 归并。 */
  simpleTools?: Record<string, ToolCall>;
  /** 该运行没有可用的过程事件（旧运行只有结果）：如实说明，不伪造过程。 */
  processMissing?: string;
  banner?: { kind: "done" | "partial" | "error" | "cancelled" | "unknown"; text: string };
  lastSeq: number }

/** 一条 Lead 过程消息：msg_id 是稳定身份，text 由 append 增量拼接 / replace 覆盖。
 *
 * ``lastPartSeq`` 是已收到的最大 part_seq（消息内序号，非全局 seq）；``gap`` 表示
 * 检测到分片缺口——渲染时必须显示不完整，不能把断流拼出的正文当成完整说明。
 */
export type ResearchInputState = 'open' | 'closing' | 'closed';
export interface ResearchUserMessage {
  message_id: string; text: string; revision: number; at: number;
  status: 'queued' | 'included' | 'undelivered'; call_id?: string; reason?: string;
}
export interface ResearchInputSnapshot { state: ResearchInputState; messages: ResearchUserMessage[]; error?: string }
export interface ResearchScope {
  revision: number; goal: string; requirements: { id: string; text: string; kind?: string }[];
  user_message_ids: string[]; reason: string; at: number;
  previous?: { goal: string; requirements: { id: string; text: string }[] };
}

export interface LeadMessage { callId?:string; interrupted?:boolean;
  msgId: string; turn: number; text: string; complete: boolean;
  role: string; kind: string; label: string; atMs: number;
  lastPartSeq: number;
  /** 已确认的分片缺口（中途跳号、首片非 0、结束标记显示尾部缺失、或权威全文未到）。 */
  gap: boolean;
  /** 结束时仍缺的分片数（有权威 replace 覆盖后清零）。 */
  missing?: number;
  /** 该消息收到过权威全文（replace）：此后不再按分片序号推断完整性。 */
  replaced?: boolean;
}

export interface PlanTrace { plan_id?: string | null; events?: any[]; }
