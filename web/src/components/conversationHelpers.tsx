import { type ReactElement,type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import { toolCallLabel } from "../displayLabels";
import type { RunView,SimpleCard,SimpleSource,ToolCall } from "../types";

const STATUS_LABEL: Record<string, string> = {
  running: "进行中",
  ok: "成功",
  failed: "失败",
  rejected: "被拒收",
  cancelled: "已取消",
  timeout: "超时",
};

/** 工具卡终态展示：running 与 done 是同一张卡的两个阶段。 */
export function toolStatus(call: ToolCall): { label: string; tone: "run" | "ok" | "bad" | "warn" } {
  if (call.phase === "running") return { label: STATUS_LABEL.running, tone: "run" };
  const status = call.status ?? (call.ok ? "ok" : "failed");
  if (status === "ok" && pendingSelections(call) > 0) return {label: "待选出处", tone: "warn"};
  const tone = status === "ok" ? "ok"
    : status === "rejected" || status === "cancelled" ? "warn"
    : "bad";
  return { label: STATUS_LABEL[status] ?? status, tone };
}

function parseJSON(text: string | undefined): any {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function pendingSelections(call: ToolCall): number {
  if (call.tool !== "save_evidence") return 0;
  const rows = parseJSON(call.result)?.results;
  return call.display?.pending_selection ?? (Array.isArray(rows)
    ? rows.filter((row: any) => row?.status === "needs_selection").length : 0);
}

function isHttp(url: unknown): url is string {
  return typeof url === "string" && /^https?:\/\//.test(url);
}

/** 当前 run 的来源索引：ref→来源。工具卡里的 URL 只信这个 run 自己的记录。 */
interface RunIndex {
  sources: Map<string, SimpleSource>;
  cards: Map<string, SimpleCard>;
}

export function buildRunIndex(view: RunView): RunIndex {
  const sources = new Map<string, SimpleSource>();
  for (const source of view.simple?.sources ?? []) sources.set(source.ref, source);
  const cards = new Map<string, SimpleCard>();
  for (const card of view.simple?.cards ?? []) cards.set(card.card_id, card);
  return { sources, cards };
}

interface ToolFact {
  main: string;
  mainHref?: string;
  /** 参数 JSON 被截断时如实标注，绝不猜造缺失字段。 */
  truncated: boolean;
  lines: { text: string; href?: string; note?: string }[];
  counters: string[];
  warn?: string;
}

/**
 * 把真实字段摘成可读事实。
 *
 * 数据来源优先级：
 * 1. ``call.display``——后端从**完整回执**截取的有界结构化字段（新事件）；
 * 2. 参数侧真实字段（query / source_ref / find / 提交条数）——任何版本都有；
 * 3. 旧事件没有 display，``result_summary`` 又常被 600 字符截断而无法解析：
 *    此时如实说明"结果摘要不完整"，**不从半截 JSON 猜字段、不造 URL**。
 */
export function toolFacts(call: ToolCall, index: RunIndex): ToolFact {
  const argsRaw = call.args ?? "";
  const resultRaw = call.result ?? "";
  const args = parseJSON(argsRaw);
  const result = parseJSON(resultRaw);
  const display = (call.display ?? undefined) as any;
  const truncatedArgs = !args && argsRaw.trim().length > 0;
  const lines: ToolFact["lines"] = [];
  const counters: string[] = [];
  let main = "";
  let mainHref: string | undefined;
  let warn: string | undefined;

  const sourceOf = (ref: unknown): SimpleSource | undefined =>
    typeof ref === "string" ? index.sources.get(ref) : undefined;
  /** 只信本 run 已登记的来源或回执里明确给出的 URL。 */
  const safeUrl = (rowUrl: unknown, ref: unknown): string | undefined => {
    if (isHttp(rowUrl)) return rowUrl;
    const source = sourceOf(ref);
    return isHttp(source?.url) ? source!.url : undefined;
  };

  if (call.tool === 'load_skill' || call.tool === 'read_skill_reference') {
    const data=display??result;
    main=data?.title??args?.name??'技能';
    if(data?.path??args?.path)lines.push({text:data?.path??args.path});
    if(data?.hash)counters.push(`版本 ${data.hash.slice(0,12)}`);
    if(data?.changed===false)counters.push('已加载，不重复注入');
    warn=data?.error;
  } else if (call.tool.startsWith('mcp_')) {
    const mcp=display?.mcp??result?.mcp;
    main=mcp?.server_name?`${mcp.server_name} · ${mcp.tool}`:'外部 MCP 资料';
    const rows=display?.sources??result?.sources??[];
    for(const row of rows)lines.push({text:`${row.source_ref} · ${row.title}`});
    if(typeof display?.total==='number')counters.push(`${display.total} 份资料`);
    if(typeof display?.elapsed_ms==='number')counters.push(`${display.elapsed_ms} ms`);
    if(mcp?.result_mode==='discovery')counters.push('检索线索');
    warn=display?.error??result?.error;
  } else if (call.tool === "search_sources") {
    main = typeof args?.query === "string" ? args.query : (truncatedArgs ? "（参数已截断）" : "检索");
    const rows = Array.isArray(display?.results) ? display.results
      : Array.isArray(result?.results) ? result.results : [];
    for (const row of rows.slice(0, 4)) {
      const url = safeUrl(row?.url, row?.source_ref);
      const ref = sourceOf(row?.source_ref);
      if (!ref && !url && !row?.document) continue;
      lines.push({
        text: row?.title || ref?.title || url || String(row?.source_ref ?? ""),
        href: url,
        note: (row?.document ? `本地资料 · ${row.document.file_name} · ` : '') + (typeof row?.snippet === "string" ? row.snippet : ''),
      });
    }
    const total = typeof display?.total === "number" ? display.total : rows.length;
    if (total > 0) counters.push(`${total} 条来源`);
    if (total > rows.length && rows.length > 0) counters.push(`只列出前 ${rows.length} 条`);
    if (rows.length === 0 && call.ok && call.phase !== "running") {
      warn = resultRaw && !result ? "结果摘要不完整（本次运行未带结构化展示字段）"
        : "本次检索没有新来源";
    }
    if (Array.isArray(display?.warnings) && display.warnings.length) warn=display.warnings.join('；');
  } else if (call.tool === "read_source") {
    const ref = sourceOf(args?.source_ref) ?? sourceOf(display?.source_ref) ?? sourceOf(result?.source_ref);
    main = display?.title || result?.title || ref?.title ||
      (typeof args?.source_ref === "string" ? args.source_ref : "读取来源");
    mainHref = safeUrl(display?.url ?? result?.url, args?.source_ref);
    const shownRange = display?.shown_range ?? result?.shown_range;
    const totalChars = display?.total_chars ?? result?.total_chars;
    const hasMore = display?.has_more ?? result?.has_more;
    if (typeof shownRange === "string") counters.push(shownRange);
    if (typeof totalChars === "number") counters.push(`共 ${totalChars} 字`);
    if (hasMore === true) counters.push("还有后文");
    else if (hasMore === false) counters.push("已读完");
    if (typeof args?.find === "string") counters.push(`定位「${args.find}」`);
    const excerpts = display?.excerpts;
    if (Array.isArray(excerpts) && excerpts[0]?.quote) {
      lines.push({ text: excerpts[0].quote,
        note: excerpts[0].location ? `原文片段 ${excerpts[0].location}` : "原文片段" });
    } else if (call.ok && call.phase !== "running" && !shownRange) {
      warn = "结果摘要不完整（本次运行未带结构化展示字段）";
    }
  } else if (call.tool === "read_research_history") {
    const turn = display?.turn ?? args?.turn;
    const kind = display?.view ?? args?.view;
    const labels: Record<string,string> = {turns:'历史轮次',answer:'历史答案',messages:'用户消息',sources:'来源目录',cards:'证据卡'};
    const imported = display?.card_ids;
    main = `${typeof turn==='number'?`第 ${turn} 轮 · `:''}${Array.isArray(imported)&&imported.length?`复用 ${imported.length} 张证据`:labels[kind]??'研究历史'}`;
  } else if (call.tool === "save_evidence") {
    main = "收录证据卡";
    const accepted = display?.accepted ?? (Array.isArray(result?.results)
      ? result.results.filter((row: any) => row?.accepted).length : undefined);
    const rejected = display?.rejected ?? (Array.isArray(result?.results)
      ? result.results.filter((row: any) => !row?.accepted && row?.status !== "needs_selection").length : undefined);
    const pending = pendingSelections(call);
    const submitted = display?.submitted ??
      (Array.isArray(args?.cards) ? args.cards.length : undefined);
    if (typeof accepted === "number") counters.push(`收录 ${accepted}`);
    if (typeof rejected === "number" && rejected > 0) counters.push(`拒收 ${rejected}`);
    if (pending > 0) counters.push(`待选出处 ${pending}`);
    if (typeof submitted === "number") counters.push(`提交 ${submitted} 条`);
    if (typeof display?.saved_total === "number") counters.push(`累计 ${display.saved_total} 张`);
    for (const row of (display?.rejections ?? []).slice(0, 2)) {
      if (typeof row?.reason === "string") lines.push({ text: row.reason, note: "拒收原因" });
    }
    if (accepted === undefined && call.ok && call.phase !== "running") {
      warn = "结果摘要不完整（本次运行未带结构化展示字段）";
    }
  } else if (["bash","read_file","write_file","edit_file","export_research_materials"].includes(call.tool)) {
    main = display?.command || display?.path || args?.command || args?.path || toolCallLabel(call);
    if (typeof display?.exit_code === "number") counters.push(`退出码 ${display.exit_code}`);
    if (typeof display?.elapsed_ms === "number") counters.push(`${(display.elapsed_ms/1000).toFixed(2)}s`);
    if (typeof display?.size === "number") counters.push(`${display.size} bytes`);
    if (display?.workspace_available === false) warn = "工作区已不可用";
    if (display?.error_code) warn = display.message || display.error_code;
  } else if (call.tool === "finish_research") {
    main = "提交答案";
    const chars = typeof args?.draft === "string" ? args.draft.length : undefined;
    if (chars !== undefined) counters.push(`正文 ${chars} 字`);
    const coverage = args?.coverage;
    if (Array.isArray(coverage) && coverage.length) counters.push(`${coverage.length} 条覆盖声明`);
  } else if (call.tool === "update_research_plan") {
    main = display?.note || result?.note || args?.note || "更新当前研究路线";
    const items = display?.items ?? result?.items ?? args?.items;
    if (Array.isArray(items)) counters.push(`${items.length} 条路线`);
    else if (typeof items === "number") counters.push(`${items} 条路线`);
  } else if (call.tool === "delegate_research") {
    main = "派发子任务";
    if (Array.isArray(display?.tasks)) counters.push(`${display.tasks.length} 个子 Agent`);
  } else if (call.tool === "send_research_message") {
    main = typeof args?.target === "string" ? `发送给 ${args.target === "parent" ? "主 Agent" : args.target}` : "发送协作消息";
    if (typeof args?.message === "string") lines.push({text:args.message});
  } else if (call.tool === "wait_research") {
    main = "等待子任务进展或结束";
  } else if (call.tool === "cancel_research") {
    main = typeof args?.delegation_id === "string" ? `取消 ${args.delegation_id}` : "取消子任务";
    if(typeof args?.reason === "string") lines.push({text:args.reason});
  } else if (call.tool === "inspect_research") {
    main = toolCallLabel(call);
    if (args?.view === "cards" && typeof args?.card_id === "string") {
      main += "：读取已保存的原文与来源";
    }
  } else {
    main = truncatedArgs ? "（参数已截断）" : call.tool;
  }

  if (truncatedArgs) warn = warn ?? "参数摘要被截断，只显示可确认的部分";
  if (!call.ok && call.status !== "running" && call.status !== "ok" && resultRaw && !result) {
    // 失败回执通常是给人看的短句（拒收原因 / 余额不足），原样展示
    warn = warn ?? resultRaw.slice(0, 300);
  }
  return { main, mainHref, truncated: truncatedArgs, lines, counters, warn };
}

/**
 * [cN] 行内引用：**行内只放一个按钮**（<p> 里不能塞 <details>，浏览器会报
 * "In HTML, <details> cannot be a descendant of <p>" 并打断排版）。
 * 点开的卡片正文渲染在答案区下方的独立位置上，行内保持合法结构。
 */
function CitationChip({
  id, known, active, onToggle,
}: {
  id: string; known: boolean; active: boolean; onToggle: () => void;
}) {
  if (!known) {
    return <span className="cite cite-missing" title="本次运行的证据卡里没有这个编号">[{id}]</span>;
  }
  return (
    <button
      type="button"
      className={`cite cite-btn${active ? " cite-active" : ""}`}
      data-cite={id}
      aria-expanded={active}
      aria-label={`查看证据 ${id}`}
      onClick={onToggle}
    >
      {id.replace(/^c/, "")}
    </button>
  );
}

/** 把正文里的 [cN] 变成可点定位；其余按 Markdown 渲染。 */
export function answerWithCitations(
  markdown: string,
  index: RunIndex,
  active: string | null = null,
  onToggle: (id: string | null) => void = () => {},
): ReactNode {
  const render = (children: ReactNode) => replaceCitations(children, index, active, onToggle);
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkBreaks]}
      components={{
        p: ({ children }) => <p>{render(children)}</p>,
        li: ({ children }) => <li>{render(children)}</li>,
        td: ({ children }) => <td>{render(children)}</td>,
      }}
    >
      {markdown}
    </ReactMarkdown>
  );
}

/** 递归替换文本节点里的 `[cN]`；只在文本层替换，不碰链接/代码。 */
function replaceCitations(
  children: ReactNode,
  index: RunIndex,
  active: string | null,
  onToggle: (id: string | null) => void,
): ReactNode {
  // 不进入链接与代码：在 <a>/<code> 里插按钮会破坏它们自己的语义（a 里不能嵌套交互元素，
  // 代码块里的 [cN] 是字面内容）。
  const OPAQUE = new Set(["a", "code", "pre"]);
  let key = 0;

  const substitute = (node: ReactNode): ReactNode => {
    if (typeof node === "string") return substituteText(node);
    if (Array.isArray(node)) return node.map(substitute);
    // React 元素要继续往里走：Markdown 的 **粗体**、*斜体*、表格单元格都把文本包在元素里，
    // 只处理顶层文本节点会让这些位置的 [cN] 变成不可点的纯文本。
    if (node && typeof node === "object") {
      const element = node as ReactElement<{ children?: ReactNode }>;
      if (!element.props) return node;
      if (typeof element.type === "string" && OPAQUE.has(element.type)) return node;
      return {
        ...element,
        props: { ...element.props, children: substitute(element.props.children) },
      };
    }
    return node;
  };

  const substituteText = (text: string): ReactNode => {
    const re = /\[(c\d+)\]/g;
    const parts: ReactNode[] = [];
    let last = 0;
    let match: RegExpExecArray | null;
    while ((match = re.exec(text))) {
      if (match.index > last) parts.push(text.slice(last, match.index));
      const id = match[1];
      parts.push(
        <CitationChip
          id={id}
          known={index.cards.has(id)}
          active={active === id}
          onToggle={() => onToggle(active === id ? null : id)}
          key={`cite-${key++}`}
        />,
      );
      last = match.index + match[0].length;
    }
    if (last < text.length) parts.push(text.slice(last));
    return parts.length === 1 ? parts[0] : parts;
  };

  return substitute(children);
}

/** 活动状态：只描述**已经发生**的事实，不编造百分比，不把等核查说成还在搜索。 */
export function activityState(view: RunView): { text: string; live: boolean } | null {
  const ended = view.endedAtMs !== undefined;
  const tools = Object.values(view.simpleTools ?? {}).filter(t =>
    t.role !== "subagent" && !t.delegationId && !t.parentCallId);
  const runningTool = tools.find((t) => t.phase === "running");
  if (ended) return null;
  if (view.simple?.phase === "finished") {
    // 研究阶段已收工、结果还没落盘：真话是"正在核查/整理结果"。
    return { text: "研究已收工，正在整理结果…", live: true };
  }
  if (runningTool) {
    return { text: `正在执行：${toolCallLabel(runningTool)}`, live: true };
  }
  // 仅成功交稿才进入保存阶段；新输入或其他校验拒收后，主线仍会继续工作。
  const submitted = tools.some((t) => t.tool === "finish_research" && t.phase === "done" && t.ok);
  if (submitted) return { text: "已提交答案，正在保存结果…", live: true };
  const pending = [...(view.leadMessages ?? [])].reverse().find((m) => m.role === "lead" && !m.complete);
  if (pending) return { text: "模型正在输出说明…", live: true };
  if ((view.timeline.length ?? 0) === 0) return { text: "已连接，等待模型开始…", live: true };
  return { text: "等待模型下一步动作…", live: true };
}
