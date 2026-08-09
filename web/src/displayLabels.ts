export const STEP_LABELS: Record<string, string> = {
  cross_worker_audit: "跨任务一致性与覆盖检查",
  write_report: "撰写研究报告",
  build_research_plan: "生成研究计划",
  revise_research_plan: "修订研究计划",
  build_report_plan: "生成报告规划",
  assess_nodes: "验收研究步骤",
  assess_research_nodes: "验收研究步骤",
  resolve_decisions: "生成决策结果",
  compile_ready_tasks: "生成可执行任务",
  condense: "压缩研究上下文",
  summarize: "生成文档摘要",
  tool_loop: "研究工具决策",
  "dispatch_task_batch(并发)": "并发执行首轮研究",
  "write_report(shape_gate)": "修正报告结构",
  dedup: "证据去重",
};

export const TOOL_LABELS: Record<string, string> = {
  search: "搜索网页",
  fetch_page: "读取页面",
  save_evidence: "收录证据",
  finish: "结束任务",
};

export function stepLabel(step: string): string {
  const researchRound = /^dispatch_task_batch\(Round (\d+)\)$/.exec(step);
  if (researchRound) return `并发执行第 ${Number(researchRound[1]) + 1} 轮研究`;
  if (/^dispatch_task_batch\(Final Research Pass, Round \d+\)$/.test(step)) {
    return "并发执行最终补查";
  }
  return STEP_LABELS[step] ?? step;
}

export function toolLabel(tool: string): string {
  return TOOL_LABELS[tool] ?? tool;
}

/** 协议内 round_index 从 0 起；界面轮次从 1 起。 */
export function displayRound(raw: unknown): string {
  return typeof raw === "number" && Number.isFinite(raw) ? String(raw + 1) : "?";
}
