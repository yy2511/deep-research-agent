// 由 scripts/gen_events_ts.py 从 dra.events.EventType 生成——手改无效。
// 改 events.py 后重跑：uv run python scripts/gen_events_ts.py
export const EVENT_TYPES = [
  "skill_loaded",
  "delegation_updated",
  "step_done",
  "llm_call",
  "llm_usage",
  "report_md",
  "error",
  "end",
  "json_retry",
  "subagent_tool_call",
  "cancelled",
  "context_compaction",
  "evidence_judged",
  "research_status",
  "lead_message",
  "agent_reasoning",
  "research_plan_updated",
  "research_input",
] as const;
export type EventTypeName = (typeof EVENT_TYPES)[number];
