import type {ToolCall} from './types';

export const STEP_LABELS: Record<string, string> = {
  research_brief: "生成研究路线",
  research_brief_gaps: "澄清研究目标",
  research_brief_revise: "修订研究路线",
  condense: "压缩研究上下文",
  lead_research: "主 Agent 研究",
  lead_wrapup: "主 Agent 收尾",
  subagent_research: "子 Agent 研究",
  subagent_wrapup: "子 Agent 收尾",
};

export const TOOL_LABELS: Record<string, string> = {
  save_evidence: "收录证据",
  // 直接研究（simple）Lead 的工具
  load_skill: "加载技能",
  read_skill_reference: "读取技能参考",
  search_sources: "搜索来源",
  bash: "运行代码",
  read_file: "读取文件",
  write_file: "写入文件",
  edit_file: "编辑文件",
  export_research_materials: "导出研究材料",
  download_source: "下载原始资料",
  read_source: "精读原文",
  view_image: "读取图片与图表",
  browser: "操作浏览器",
  read_research_history: "读取研究历史",
  inspect_research: "查看研究状态",
  finish_research: "提交答案",
  update_research_plan: "更新研究路线",
  send_research_message: "发送协作消息",
  wait_research: "等待子任务",
  cancel_research: "取消子任务",
  delegate_research: "派发子任务",
};

export function stepLabel(step: string): string {
  return STEP_LABELS[step] ?? step;
}

export function toolLabel(tool: string): string {
  return TOOL_LABELS[tool] ?? tool;
}

/** 截断/未知参数保留通用名，不从半段 JSON 猜卡号。 */
export function toolCallLabel(call: Pick<ToolCall, 'tool' | 'args' | 'display'>): string {
  if (call.tool.startsWith('mcp_')) {
    const mcp=call.display?.mcp;
    return mcp?.server_name&&mcp?.tool?`${mcp.server_name} · ${mcp.tool}`:'调用外部资料';
  }
  if (call.tool !== 'inspect_research') return toolLabel(call.tool);
  let args: Record<string, unknown> | null;
  try { args = JSON.parse(call.args ?? '{}'); } catch { return toolLabel(call.tool); }
  if (args?.view === 'cards') {
    return typeof args.card_id === 'string' && /^c\d+$/.test(args.card_id)
      ? `回读证据 ${args.card_id}` : '查看已保存证据';
  }
  return ({sources:'查看研究来源', delegations:'查看子任务进展', summary:'查看研究概况'} as Record<string,string>)
    [String(args?.view)] ?? toolLabel(call.tool);
}

export const STOP_LABEL: Record<string, { text: string; tone: "ok" | "warn" | "muted" }> = {
  finished: { text: "正常结束", tone: "ok" },
  "tool_budget+wrapup": { text: "工具调用次数用完，提前收尾", tone: "warn" },
  "call_budget+wrapup": { text: "模型调用次数用完，提前收尾", tone: "warn" },
  "no_progress+wrapup": { text: "连续几次没有进展，提前收尾", tone: "warn" },
  cancelled: { text: "你停止了研究", tone: "muted" },
  deadline: { text: "到了时间上限", tone: "warn" },
  failed: { text: "运行出错", tone: "muted" },
  "failed+partial_delivery": { text: "附件修正中断，已保留可用成果", tone: "warn" },
  context_limit: { text: "上下文装不下了，已保留现有成果", tone: "muted" },
  request_timeout: { text: "模型请求超时", tone: "muted" },
  no_draft: { text: "没有写出正文", tone: "muted" },
};

export const COVERAGE_LABEL: Record<string, string> = {
  answered: "已回答",
  partial: "部分回答",
  unanswered: "未回答",
  unsupported: "无依据",
  unavailable: "资料不可得",
};
