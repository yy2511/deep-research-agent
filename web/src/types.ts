export type Stage = "plan" | "dispatch" | "research" | "reflect" | "write" | "check" | "done";
export interface LlmRetry {
  reason: string;
  attempt: number;
  exhausted?: boolean;
  rawHead?: string;
}
export interface LlmCall { step: string; model?: string; ms: number; inTok?: number; outTok?: number; input?: string; output?: string; ioComplete?: boolean; sid?: string; workerIteration?: number; retries?: LlmRetry[] }
export interface ToolCall { callNo: number; tool: string; ok: boolean; args: string; result: string; accepted?: number; rejected?: number; rejectReasons?: string[]; evidenceTotal: number; atMs: number;
  /** 后端附带的结构化来源；缺失时可从当前工具结果摘要中提取可确认的信息。 */
  links?: { title: string; url: string }[]; url?: string; nExcerpts?: number }
export interface EvidenceCardView {
  cardNo: number;
  claim: string;
  supportQuote: string;
  quoteTruncated: boolean;
  sourceTitle: string | null;
  sourceUrl: string | null;
  publishedAt: string | null;
}
export type SubagentLifecycle = "pending" | "running" | "done";
export type SubagentOutcome = "ok" | "empty" | "timeout" | "failed" | "hard_error" | "unknown";
export type WorkerStopReason = "sufficient" | "no_progress" | "timeout" | "tool_budget";
export interface Subagent { sid: string; objective: string; status: SubagentLifecycle; outcome: SubagentOutcome;
  nodeId?: string; roundIndex?: number; mode: "loop" | "unknown"; calls: ToolCall[]; llmCalls: LlmCall[];
  cards: EvidenceCardView[]; evidenceTotal: number; toolCallsDone?: number; stopReason?: WorkerStopReason; summary?: string;
  currentLabel?: string; currentSinceMs?: number }
export interface OrchCard { kind: string; atMs: number; [k: string]: unknown }
/** 编年研究流:编排叙事(note)、子代理卡锚点(sub)、编排级 LLM I/O(llm)按事件到达序排列。
 *  sub 只在首现时挂一次锚,后续 tool_call/done 原位更新 subagents[sid],卡片位置稳定。 */
export type TimelineItem =
  | { t: "note"; card: OrchCard }
  | { t: "sub"; sid: string; atMs: number }
  | { t: "llm"; call: LlmCall; atMs: number };
export interface StatsAccum { llm: Record<string, { step: string; model: string; calls: number; totalMs: number; inTok: number; outTok: number }>;
  steps: Record<string, { calls: number; totalMs: number }>;
  tools: Record<string, { calls: number; ok: number; accepted: number; rejected: number }> }
export interface RunView { stage: Stage; startedAtMs?: number; endedAtMs?: number;
  planned: string[]; subagents: Record<string, Subagent>; order: string[];
  /** task id→计划节点；让缺少新增 node_id 字段的同版早期轨迹至少能还原首轮归属。 */
  researchTaskNodeIds: Record<string, string>;
  timeline: TimelineItem[]; reportMd?: string; savedPath?: string;
  /** 全局去重后的证据数；不能用各 Worker 的局部计数相加代替。 */
  evidenceCount?: number;
  banner?: { kind: "done" | "partial" | "error" | "cancelled" | "unknown"; text: string };
  phaseText: string; stats: StatsAccum; lastSeq: number }
