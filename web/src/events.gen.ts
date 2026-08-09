// 由 scripts/gen_events_ts.py 从 dra.events.EventType 生成——手改无效。
// 改 events.py 后重跑：uv run python scripts/gen_events_ts.py
export const EVENT_TYPES = [
  "scope",
  "research_plan",
  "report_plan",
  "task_batch_dispatched",
  "ready_set_computed",
  "research_round_completed",
  "nodes_assessed",
  "subagent_start",
  "subagent_done",
  "step_done",
  "llm_call",
  "collect",
  "cross_worker_audit",
  "writing",
  "shape_gate",
  "report_md",
  "done",
  "error",
  "end",
  "json_retry",
  "subagent_tool_call",
  "cancelled",
] as const;
export type EventTypeName = (typeof EVENT_TYPES)[number];
