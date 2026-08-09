import { useEffect, useRef, useState, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";
import type { EvidenceCardView, OrchCard, RunView, Subagent, SubagentOutcome, ToolCall, WorkerStopReason } from "../types";
import { displayRound, toolLabel } from "../displayLabels";
import { Icon, type IconName } from "./Icon";
import { LlmCallsList } from "./LlmCalls";
import { StatsView } from "./ResultPane";

// save_reserve_calls 后端默认值——/api/config 目前不暴露这个字段（只给 max_tool_calls），
// 油表末段"降落保留区"视觉纯是提示性质，硬编码匹配当前后端默认即可，不为这一个数字扩 API。
const SAVE_RESERVE_CALLS = 2;

const STOP_REASON_LABELS: Record<WorkerStopReason, string> = {
  sufficient: "当前任务的证据已足够",
  no_progress: "没有新增进展",
  timeout: "达到运行时限",
  tool_budget: "工具调用额度用尽",
};

function fmtMMSS(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const mm = Math.floor(total / 60).toString().padStart(2, "0");
  const ss = (total % 60).toString().padStart(2, "0");
  return `${mm}:${ss}`;
}

/** 相对 run 起点的 mm:ss;起点未知时不显示 */
function relTs(view: RunView, atMs: number): string | null {
  if (view.startedAtMs === undefined) return null;
  return fmtMMSS(atMs - view.startedAtMs);
}

function parseMaybeJson(s: string | undefined): any {
  if (!s) return null;
  try { return JSON.parse(s); } catch { return null; }
}

interface SourceLink { title: string; url?: string }
interface PlanNodeView {
  id?: string;
  objective?: string;
  kind?: string;
  dependency_ids?: string[];
}

function planNodesFromView(view: RunView): PlanNodeView[] {
  const plan = view.timeline.find((item) => item.t === "note" && item.card.kind === "research_plan");
  return plan?.t === "note"
    ? ((plan.card.plan_nodes as PlanNodeView[] | undefined) ?? [])
    : [];
}

/** 相对跳转链(DDG /goto 等)不当外链渲染。 */
function httpUrl(u: string | undefined): string | undefined {
  return u && /^https?:\/\//.test(u) ? u : undefined;
}

function domainOf(u: string): string {
  try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return ""; }
}

/** URL 比对键：忽略页内锚点，其余部分保持原样，避免把不同查询参数的页面误合并。 */
function urlKey(u: string): string {
  try {
    const parsed = new URL(u);
    parsed.hash = "";
    return parsed.toString();
  } catch {
    return u;
  }
}

/** search 来源清单：优先读结构化 links（全量且可得真实计数）；
 *  缺失时从可能被截断的 result JSON 中提取 title（及紧随的完整 URL），
 *  URL 未闭合就只留标题,绝不当真半截链接。 */
function linksFrom(call: ToolCall): SourceLink[] {
  if (call.links?.length) return call.links;
  const out: SourceLink[] = [];
  if (!call.result) return out;
  const re = /"title":\s*"((?:[^"\\]|\\.)*)"(?:,\s*"url":\s*"((?:[^"\\]|\\.)*)")?/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(call.result))) {
    try {
      out.push({ title: JSON.parse(`"${m[1]}"`),
        url: m[2] !== undefined ? JSON.parse(`"${m[2]}"`) : undefined });
    } catch { /* 截断残句,跳过 */ }
  }
  return out;
}

/** doc_id→title 映射:从该子代理全部 search 结果的可见片段里收集,供 fetch_page 行显示标题。 */
function docTitleMap(calls: ToolCall[]): Map<string, string> {
  const map = new Map<string, string>();
  const re = /"doc_id":\s*"([^"]+)",\s*"title":\s*"((?:[^"\\]|\\.)*)"/g;
  for (const c of calls) {
    if (c.tool !== "search" || !c.result) continue;
    let m: RegExpExecArray | null;
    while ((m = re.exec(c.result))) {
      try { map.set(m[1], JSON.parse(`"${m[2]}"`)); } catch { /* skip */ }
    }
  }
  return map;
}

/** fetch_page 结果头部约定为「【标题】(url)」；URL 本身可能包含成对括号。 */
function fetchedSource(call: ToolCall): SourceLink | undefined {
  const raw = call.result ?? "";
  const own = /^【\*{0,2}(.+?)\*{0,2}】(?:\((.+))?/.exec(raw);
  const title = own?.[1]?.trim();
  if (!title) return undefined;
  let rawUrl: string | undefined;
  if (own?.[2]) {
    let depth = 1;
    let end = 0;
    for (let i = 0; i < own[2].length; i++) {
      if (own[2][i] === "(") depth++;
      else if (own[2][i] === ")") {
        depth--;
        if (depth === 0) { end = i; break; }
      }
    }
    rawUrl = end > 0 ? own[2].slice(0, end) : undefined;
  }
  return { title, url: httpUrl(call.url) ?? httpUrl(rawUrl) };
}

/** 报告引用优先显示已检索/抓取到的文章标题，而不是把超长裸 URL 铺进正文。 */
function reportSourceTitles(view: RunView): Map<string, string> {
  const titles = new Map<string, string>();
  const remember = (source: SourceLink | undefined) => {
    const url = httpUrl(source?.url);
    const title = source?.title.trim();
    if (!url || !title) return;
    const key = urlKey(url);
    const current = titles.get(key);
    // result_summary 可能把 fetch 页头截成半句；结构化 search title 往往更完整。
    if (!current || title.length > current.length) titles.set(key, title);
  };
  for (const subagent of Object.values(view.subagents)) {
    for (const call of subagent.calls) {
      for (const source of linksFrom(call)) remember(source);
      if (call.tool === "fetch_page") remember(fetchedSource(call));
    }
  }
  return titles;
}

function textFromNode(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textFromNode).join("");
  return "";
}

/** 一手牌 → 参考稿式动作行:图标 + 工具名 + 真实主文本;右侧只放真实计量。 */
function ActRow({
  call, titles, mode, showLegacySaveSummary,
}: {
  call: ToolCall; titles: Map<string, string>; mode: "debug" | "clean";
  showLegacySaveSummary: boolean;
}) {
  const args = parseMaybeJson(call.args);
  let main = "";
  let ico: IconName = "search";
  let fetchUrl: string | undefined;
  if (call.tool === "search") {
    main = args?.query ?? call.args ?? "";
  } else if (call.tool === "fetch_page") {
    ico = "page";
    const docId = args?.doc_id ?? "";
    // fetch 结果头部自带标题与 url:【**标题】(url)——优先用它,其次查同 agent 的
    // search 片段映射,都没有才落 doc id(诚实兜底)
    const own = fetchedSource(call);
    main = own?.title ?? titles.get(docId) ?? (docId ? `doc ${docId}` : call.args ?? "");
    fetchUrl = own?.url;
  } else if (call.tool === "save_evidence") {
    ico = "archive";
    const first = showLegacySaveSummary ? args?.cards?.[0]?.claim : undefined;
    const n = showLegacySaveSummary && Array.isArray(args?.cards) ? args.cards.length : undefined;
    main = first ? (n && n > 1 ? `${first} 等 ${n} 条` : first) : "收录证据";
  } else if (call.tool === "finish") {
    ico = "complete";
    main = args?.summary ?? "结束任务";
  } else {
    main = call.args ?? "";
  }
  const sources = call.tool === "search" && call.ok ? linksFrom(call) : [];
  const shown = sources.slice(0, 3);
  const fetchMeta = call.tool === "fetch_page" && call.ok
    ? [call.nExcerpts !== undefined ? `片段 ${call.nExcerpts}` : null,
       fetchUrl ? domainOf(fetchUrl) : null].filter(Boolean).join(" · ")
    : "";
  // 非 JSON 的 result(拒收提示/无新文档/检索失败)是给人看的短句,原样当注释行展示
  const resultNote = !call.ok || (call.tool === "search" && call.result && !call.result.startsWith("{"))
    ? call.result : null;
  const hasDetail = mode === "debug" && !!(call.args || call.result || (call.rejectReasons?.length ?? 0) > 0);
  return (
    <div className={`act${call.ok ? "" : " act-fail"} act-t-${call.tool}`}>
      <div className="act-row">
        <Icon name={ico} className="act-ico" size={16} />
        <span className="act-label">{toolLabel(call.tool)}</span>
        <span className="act-sep" />
        <span className="act-q" title={main}>
          {fetchUrl
            ? <a className="act-link" href={fetchUrl} target="_blank" rel="noopener noreferrer">{main}</a>
            : main}
        </span>
        {/* 计数只来自结构化字段；摘要可能截断，真实总数不可知时不显示。 */}
        {call.tool === "search" && (call.links?.length ?? 0) > 0 && (
          <span className="num act-cnt">{call.links!.length} 结果</span>
        )}
        {fetchMeta && <span className="num act-cnt">{fetchMeta}</span>}
        {call.tool === "save_evidence" && ((call.accepted ?? 0) > 0 || (call.rejected ?? 0) > 0) && (
          <span className="num act-yield">
            {(call.accepted ?? 0) > 0 && <>收录 {call.accepted}</>}
            {(call.rejected ?? 0) > 0 && (
              <span className="act-reject">{(call.accepted ?? 0) > 0 ? " · " : ""}拒收 {call.rejected}</span>
            )}
          </span>
        )}
        <span className="num act-no">#{call.callNo}</span>
      </div>
      {shown.map((s, i) => {
        const url = httpUrl(s.url);
        return (
          <div className="act-note" key={i} title={s.title}>
            {url
              ? <a className="act-link" href={url} target="_blank" rel="noopener noreferrer">{s.title}</a>
              : <span className="act-plain">{s.title}</span>}
            {url && <span className="num act-domain">{domainOf(url)}</span>}
          </div>
        );
      })}
      {sources.length > shown.length && (
        <div className="num act-tail">…共 {sources.length} 条</div>
      )}
      {resultNote && <div className="act-note act-note-warn">{resultNote}</div>}
      {(call.rejectReasons?.length ?? 0) > 0 && mode === "clean" && (
        <div className="act-note act-note-warn">{call.rejectReasons!.join("；")}</div>
      )}
      {hasDetail && (
        <details className="hand-detail">
          <summary><Icon name="chevron-right" className="summary-chevron" size={13} />原始 args · result</summary>
          <div className="hand-detail-io">
            {call.args && (
              <div className="hand-io-block"><span className="hand-io-tag">args</span><pre>{call.args}</pre></div>
            )}
            {call.result && (
              <div className="hand-io-block"><span className="hand-io-tag">result</span><pre>{call.result}</pre></div>
            )}
            {(call.rejectReasons?.length ?? 0) > 0 && (
              <ul className="reject-reasons">
                {call.rejectReasons!.map((r, j) => <li key={j}>{r}</li>)}
              </ul>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

function FuelGauge({ calls, maxToolCalls }: { calls: ToolCall[]; maxToolCalls: number }) {
  // 每格 = 一个预算槽位（callNo，从 1 起）。降落保留区拒收不自增 call_no（不耗预算），
  // 按 callNo 归槽、同槽保留信息量更高的那条，计数恒 ≤ maxToolCalls（见 DEVLOG 13/12 修）。
  const bySlot = new Map<number, ToolCall>();
  let used = 0;
  for (const c of calls) {
    used = Math.max(used, c.callNo);
    const prev = bySlot.get(c.callNo);
    if (!prev || (!prev.ok && c.ok)) bySlot.set(c.callNo, c);
  }
  const cells = Array.from({ length: maxToolCalls }, (_, i) => {
    const call = bySlot.get(i + 1);
    const inReserve = i >= maxToolCalls - SAVE_RESERVE_CALLS;
    const classes = ["fuel-cell"];
    if (inReserve) classes.push("fuel-reserve-zone");
    // 悬停释义（用户实测反馈：四种格子状态不易自行解码，现场演示尤甚）
    let tip: string | undefined;
    if (call) {
      if (!call.ok) classes.push("fuel-fail");
      else if (call.tool === "save_evidence" && (call.accepted ?? 0) > 0) classes.push("fuel-accepted");
      else classes.push("fuel-ok");
      tip = `#${i + 1} · ${toolLabel(call.tool)} · ${call.ok ? "成功" : "失败"}`;
      if (call.tool === "save_evidence" && (call.accepted ?? 0) > 0) tip += ` · 收录 ${call.accepted}`;
    } else if (inReserve) {
      tip = "预留额度：用于保存证据或结束任务";
    }
    return <span className={classes.join(" ")} title={tip} key={i} />;
  });
  return (
    <div className="fuel-gauge">
      <span className="fuel-label">工具额度</span>
      <div className="fuel-cells">{cells}</div>
      <span className="num fuel-count">{Math.min(used, maxToolCalls)}/{maxToolCalls}</span>
    </div>
  );
}

const OUTCOME_VIEW: Record<SubagentOutcome, { lamp: string; label: string }> = {
  ok: { lamp: "lamp-done", label: "完成" },
  empty: { lamp: "lamp-warning", label: "无证据" },
  timeout: { lamp: "lamp-warning", label: "超时" },
  failed: { lamp: "lamp-failed", label: "失败" },
  hard_error: { lamp: "lamp-failed", label: "系统错误" },
  unknown: { lamp: "lamp-pending", label: "结果未知" },
};

function EvidenceSection({ cards }: { cards: EvidenceCardView[] }) {
  return (
    <section className="evidence-section" aria-label={`证据 ${cards.length} 条`}>
      <div className="evidence-head">证据 <span className="num">· {cards.length}</span></div>
      <div className="evidence-list">
        {cards.map((card) => {
          const url = httpUrl(card.sourceUrl ?? undefined);
          const source = card.sourceTitle?.trim() || (url ? domainOf(url) : "") || "来源未标注";
          return (
            <article className="evidence-card" key={card.cardNo}>
              <div className="evidence-main">
                <span className="num evidence-no">[{card.cardNo}]</span>
                <strong className="evidence-claim">{card.claim}</strong>
              </div>
              <div className="evidence-source">
                {url
                  ? <a href={url} target="_blank" rel="noopener noreferrer">{source}</a>
                  : <span>{source}</span>}
                {card.publishedAt && (
                  <time className="num" dateTime={card.publishedAt}>{card.publishedAt.slice(0, 10)}</time>
                )}
              </div>
              <details className="evidence-quote">
                <summary><Icon name="chevron-right" className="summary-chevron" size={13} />查看原文摘录</summary>
                <blockquote>
                  {card.supportQuote}
                  {card.quoteTruncated && <span className="quote-truncated">…（已截断）</span>}
                </blockquote>
              </details>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function SubCard({
  subagent, mode, nowMs, maxToolCalls, nodeNumber, nodeObjective, open, onOpenChange,
}: {
  subagent: Subagent; mode: "debug" | "clean"; nowMs: number; maxToolCalls: number;
  nodeNumber?: number; nodeObjective?: string;
  open: boolean; onOpenChange: (open: boolean) => void;
}) {
  const outcome = subagent.outcome ?? "unknown";
  const finished = OUTCOME_VIEW[outcome];
  const lampClass = subagent.status === "done" ? finished.lamp
    : subagent.status === "running" ? "lamp-current" : "lamp-pending";
  const titles = docTitleMap(subagent.calls);
  const userTogglePendingRef = useRef(false);
  return (
    <details
      className="sub-block sub-card"
      open={open}
      onToggle={(event) => {
        // Chromium 会把 React 写入 open 属性触发的 toggle 也标成 trusted，不能靠 isTrusted
        // 区分用户操作。只接受 summary 点击后紧随的 toggle，避免默认展开被误存成 override。
        if (!userTogglePendingRef.current) return;
        userTogglePendingRef.current = false;
        onOpenChange(event.currentTarget.open);
      }}
    >
      <summary className="sub-head" onClick={() => { userTogglePendingRef.current = true; }}>
        <Icon name="chevron-right" className="summary-chevron" size={15} />
        <span className="sub-head-row">
          <span className={`lamp ${lampClass}`} />
          {nodeNumber !== undefined && (
            <span className="sub-node" title={nodeObjective}>步骤 {nodeNumber}</span>
          )}
          {mode === "debug" && subagent.roundIndex !== undefined && (
            <span className="round-badge">第 {displayRound(subagent.roundIndex)} 轮</span>
          )}
          {mode === "debug" && subagent.nodeId && <span className="sub-node-id">{subagent.nodeId}</span>}
          <span className="sub-obj">{subagent.objective}</span>
          <span className="sub-meta">
            {subagent.status === "done" && (
              <span className={`num outcome-label outcome-${outcome}`}>{finished.label}</span>
            )}
            <span className="num sub-ev">证据 {subagent.evidenceTotal}</span>
          </span>
        </span>
        {!open && subagent.summary && <span className="sub-summary-compact">{subagent.summary}</span>}
      </summary>
      <div className="sub-body">
        {subagent.summary && <p className="sub-summary-full">{subagent.summary}</p>}
        {subagent.status === "running" && subagent.currentLabel && subagent.currentSinceMs !== undefined && (
          <div className="active-line num">
            {subagent.currentLabel} · {fmtMMSS(nowMs - subagent.currentSinceMs)}
          </div>
        )}
        {subagent.cards.length > 0 && <EvidenceSection cards={subagent.cards} />}
        {subagent.mode === "loop" && <FuelGauge calls={subagent.calls} maxToolCalls={maxToolCalls} />}
        {subagent.calls.length > 0 && (
          <div className="acts">
            {subagent.calls.map((c, i) => (
              <ActRow
                call={c}
                titles={titles}
                mode={mode}
                showLegacySaveSummary={subagent.cards.length === 0}
                key={i}
              />
            ))}
          </div>
        )}
        {subagent.status === "done" && subagent.stopReason && (
          <div className="stop-reason">停止继续检索：{STOP_REASON_LABELS[subagent.stopReason]}</div>
        )}
        {mode === "debug" && <LlmCallsList calls={subagent.llmCalls} />}
      </div>
    </details>
  );
}

/** 报告一键带走（用户实测反馈：现场演示想复制全文只能去翻 runs/ 文件）。 */
function CopyMdButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="copy-md"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 2000);
        }).catch(() => { /* 剪贴板被拒绝时保持原文案,不装成功 */ });
      }}
    >
      <Icon name={copied ? "check" : "copy"} size={14} />
      {copied ? "已复制" : "复制 Markdown"}
    </button>
  );
}

const NODE_STATUS: Record<string, { label: string; cls: string }> = {
  complete: { label: "完成", cls: "st-ok" },
  partial: { label: "部分完成", cls: "st-part" },
  blocked: { label: "阻塞", cls: "st-bad" },
};

function assessmentText(text: string): string {
  return text
    .replaceAll("acceptance_criteria", "验收标准")
    .replace(/\bcomplete\b/g, "完成")
    .replace(/\bpartial\b/g, "部分完成")
    .replace(/\bblocked\b/g, "阻塞");
}

/** 编排叙事卡 → 文档流里的一段。lead=推进叙事;verdict=裁决/审计;line=次要过程行。 */
function NoteBlock({ card, view, mode }: { card: OrchCard; view: RunView; mode: "debug" | "clean" }) {
  const ts = relTs(view, card.atMs);
  const T = ts && <span className="lead-ts num">{ts}</span>;
  const knownNodes = planNodesFromView(view);
  const nodeIndex = new Map(knownNodes.map((node, index) => [node.id, index + 1]));
  const nodeObjective = new Map(knownNodes.map((node) => [node.id, node.objective]));
  const nodeLabel = (id: string) => {
    const number = nodeIndex.get(id);
    const objective = nodeObjective.get(id);
    return number ? `步骤 ${number}${objective ? ` · ${objective}` : ""}` : id;
  };
  // 验收行的主信息是状态与摘要；完整目标已在上方计划树展示。这里只保留稳定的
  // “步骤 N”，把完整目标放进 title，避免长目标把摘要挤成逐字竖排。
  const assessmentNodeLabel = (id: string) => {
    const number = nodeIndex.get(id);
    if (!number) return id;
    return mode === "debug" ? `步骤 ${number} · ${id}` : `步骤 ${number}`;
  };
  switch (card.kind) {
    case "scope":
      return (
        <div className="q-block">
          <div className="q-eyebrow">Deep Research</div>
          <div className="q-title">{String(card.query ?? "")}</div>
        </div>
      );
    case "research_plan": {
      const initialTasks = (card.initial_tasks as Array<{ objective?: string; node_id?: string }> | undefined) ?? [];
      const planNodes = (card.plan_nodes as PlanNodeView[] | undefined) ?? [];
      const byNode = new Map<string, string[]>();
      for (const t of initialTasks) {
        if (!t.node_id || !t.objective) continue;
        byNode.set(t.node_id, [...(byNode.get(t.node_id) ?? []), t.objective]);
      }
      const flat = initialTasks
        .filter((t) => !t.node_id || !planNodes.some((node) => node.id === t.node_id))
        .map((t) => t.objective ?? "");
      return (
        <div className="lead">
          <b>研究规划：</b>{T}{planNodes.length} 个研究步骤 · 首轮 {initialTasks.length} 个研究任务。
          <div className="plan-tree">
            {planNodes.map((node, ni) => {
              // 锁定判定用结构（有依赖=非根）：首轮任务只会派给根 research 节点。
              const locked = (node.dependency_ids?.length ?? 0) > 0;
              const own = byNode.get(node.id ?? "") ?? [];
              const state = locked ? "等待前置" : own.length > 0 ? "首轮可执行" : "待执行";
              return (
                <div className={`pms${locked ? " pms-locked" : ""}`} key={node.id ?? ni}>
                  <div className="pms-head">
                    <span className="pms-step">步骤 {ni + 1}</span>
                    <span className={`plan-kind plan-kind-${node.kind ?? "research"}`}>
                      {node.kind === "decision" ? "决策" : "研究"}
                    </span>
                    <span className={`pms-state${locked ? " pms-state-wait" : ""}`}>{state}</span>
                    {mode === "debug" && <span className="num pms-id">{node.id}</span>}
                  </div>
                  <div className="pms-obj">{node.objective}</div>
                  {locked && (
                    <div className="pms-deps">
                      <span>前置步骤</span>
                      {node.dependency_ids!.map((id) => (
                        <span className="pms-dep" title={nodeLabel(id)} key={id}>
                          步骤 {nodeIndex.get(id) ?? id}
                        </span>
                      ))}
                    </div>
                  )}
                  {own.length > 0 && own.every((objective) => objective === node.objective)
                    ? <div className="pms-task">首轮执行 · {own.length} 个任务</div>
                    : own.map((o, i) => <div className="pms-task" key={i}>首轮任务：{o}</div>)}
                  {locked && (
                    <div className="pms-wait">
                      {node.kind === "decision"
                        ? "等待前置步骤完成；随后根据已收录证据做出选择"
                        : "等待前置步骤完成；随后生成并执行研究任务"}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {flat.length > 0 && (
            <ol className="plan-ol">{flat.map((q, i) => <li key={i}>{q}</li>)}</ol>
          )}
        </div>
      );
    }
    case "ready_set_computed":
      if (card.pending === 0 || card.n_tasks === 0) {
        return <div className="lead"><b>所有研究步骤已完成。</b>{T}</div>;
      }
      return (
        <div className="lead">
          <b>{card.phase === "final_research_pass"
            ? "最终补查准备："
            : `第 ${displayRound(card.round_index)} 轮任务准备：`}</b>{T}
          已生成 {String(card.n_tasks ?? "?")} 个可执行任务
          {(mode === "debug" || card.n_tasks === undefined) && card.reason ? ` · ${String(card.reason)}` : ""}
        </div>
      );
    case "task_batch_dispatched": {
      const n = card.count ?? (card.objectives as string[] | undefined)?.length ?? "?";
      const phase = card.phase === "final_research_pass"
        ? "最终补查"
        : card.phase === "initial" ? "首轮研究" : `第 ${displayRound(card.round_index)} 轮研究`;
      return <div className="flow-line">{phase} · 并发 {String(n)} 个任务</div>;
    }
    case "research_round_completed":
      return (
        <div className="verdict-p verdict-neutral">
          <b>{card.phase === "final_research_pass"
            ? "最终补查完成："
            : `第 ${displayRound(card.round_index)} 轮研究完成：`}</b>{T}
          +{String(card.added ?? 0)} 条证据，累计 <span className="num">{String(card.total ?? 0)}</span> 条，
          剩余 {String(card.remaining ?? 0)} 个目标。
        </div>
      );
    case "nodes_assessed": {
      const assessments = (card.assessments as Array<{ node_id?: string; status?: string; summary?: string; gaps?: string[]; assessment_contract_error?: string }> | undefined) ?? [];
      const unresolvedIds = (card.unresolved_node_ids as string[] | undefined) ?? [];
      const degradedAdvances = assessments.flatMap((assessment) => {
        if (assessment.status !== "complete" || !assessment.node_id) return [];
        const node = knownNodes.find((candidate) => candidate.id === assessment.node_id);
        const partialDeps = (node?.dependency_ids ?? []).filter((id) => unresolvedIds.includes(id));
        return partialDeps.length > 0
          ? [{ nodeId: assessment.node_id, dependencyIds: partialDeps }]
          : [];
      });
      return (
        <div className="verdict-p">
          <b>步骤验收：</b>{T}
          {assessments.map((r, i) => {
            const st = NODE_STATUS[r.status ?? ""] ?? { label: r.status ?? "未知", cls: "st-unknown" };
            const kind = knownNodes.find((node) => node.id === r.node_id)?.kind;
            return (
              <div className="verdict-row" key={i}>
                <div className="verdict-meta">
                  <span
                    className="num verdict-id"
                    title={r.node_id ? nodeLabel(r.node_id) : undefined}
                  >
                    {r.node_id ? assessmentNodeLabel(r.node_id) : "?"}
                  </span>
                  {kind && <span className={`verdict-kind verdict-kind-${kind}`}>{kind === "decision" ? "决策" : "研究"}</span>}
                  <span className={`verdict-st ${st.cls}`}>
                    {r.status === "partial" ? "部分完成 · 仍需补查" : st.label}
                  </span>
                </div>
                {r.summary && <span className="verdict-sum">{assessmentText(r.summary)}</span>}
                {r.assessment_contract_error && (
                  <div className="verdict-gap">验收协议错误：{assessmentText(r.assessment_contract_error)}</div>
                )}
                {(r.gaps?.length ?? 0) > 0 && (
                  <div className="verdict-gap">缺口：{r.gaps!.map(assessmentText).join("；")}</div>
                )}
              </div>
            );
          })}
          {degradedAdvances.map(({ nodeId, dependencyIds }) => (
            <div className="verdict-degraded" key={nodeId}>
              阶段性推进：{assessmentNodeLabel(nodeId)} 已基于
              {dependencyIds.map(assessmentNodeLabel).join("、")} 的现有证据先行完成；
              {dependencyIds.map(assessmentNodeLabel).join("、")} 仍未完全达到验收标准。
            </div>
          ))}
          <div className={`verdict-progress${unresolvedIds.length > 0 ? " verdict-progress-open" : ""}`}>
            {unresolvedIds.length > 0
              ? `此刻仍未完成：${unresolvedIds.map(assessmentNodeLabel).join("、")}`
              : "此刻所有研究步骤均已完成"}
          </div>
        </div>
      );
    }
    case "collect":
      return (
        <div className="lead">
          <b>证据汇总：</b>{T}{String(card.before ?? 0)} → {String(card.after ?? 0)} 条
          （去重 {String(card.deduped ?? 0)}，失败 {String(card.failures ?? 0)}，{String(card.n_sub ?? 0)} 个研究任务）。
        </div>
      );
    case "report_plan": {
      const sections = (card.sections as string[] | undefined) ?? [];
      return (
        <div className="lead">
          <b>报告规划：</b>{T}{sections.length} 节 — {sections.join(" / ")}
          · {String(card.n_limitations ?? 0)} 条局限与后续研究提示。
        </div>
      );
    }
    case "cross_worker_audit": {
      const conflicts = (card.conflicts as unknown[] | undefined) ?? [];
      const hasFindings = card.findings === true || conflicts.length > 0;
      return (
        <div className={`verdict-p ${hasFindings ? "verdict-warn" : ""}`}>
          <b>跨任务一致性与覆盖检查：</b>{T}
          <span className={hasFindings ? "st-part" : "st-ok"}>
            {hasFindings ? "发现证据覆盖不足或冲突" : "未发现明显问题"}
          </span>
          {card.reason ? ` — ${String(card.reason)}` : ""}
          {conflicts.length > 0 && ` · ${conflicts.length} 处矛盾`}
        </div>
      );
    }
    case "writing":
      return <div className="lead"><b>综合写作：</b>{T}综合 {String(card.n_evidence ?? 0)} 条证据成文。</div>;
    case "shape_gate": {
      const missing = (card.missing as string[] | undefined) ?? [];
      const phase = card.phase === "initial" ? "初检" : card.phase === "final" ? "复检" : "校验";
      return (
        <div className="flow-line">
          结构检查（{phase}）：{missing.length ? `缺 ${missing.join("、")}` : "通过 · 结构完整"}
        </div>
      );
    }
    case "done":
    case "error":
    case "cancelled":
      return view.banner ? (
        <div className={`done-line done-${view.banner.kind}`}>
          <span className={`lamp ${view.banner.kind === "done" ? "lamp-done"
            : view.banner.kind === "error" ? "lamp-failed" : "lamp-cancelled"}`} />
          {view.banner.text}
        </div>
      ) : null;
    default:
      return <div className="flow-line">{card.kind}</div>;
  }
}

export function FlowPane({
  view, mode, nowMs, maxToolCalls,
}: {
  view: RunView;
  mode: "debug" | "clean";
  nowMs: number;
  maxToolCalls: number;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  // pinned = 用户停在流末尾(自动跟随)。解除跟随的唯一途径是**用户向上滚**——
  // 追加内容/程序滚动只会往下,不允许它们把 pinned 误翻(实测:报告整块追加时
  // 自家 scrollIntoView 与渲染赛跑,按"离底距离"判定会让跟随中途熄火)。
  const pinnedRef = useRef(true);
  const lastTopRef = useRef(0);
  const [showJump, setShowJump] = useState(false);
  // 这里只保存用户覆盖；未操作过的 Worker 始终跟随 running 展开、done 折叠的默认规则。
  const [collapseOverrides, setCollapseOverrides] = useState<Record<string, boolean>>({});
  const running = view.startedAtMs !== undefined && view.endedAtMs === undefined;
  const sourceTitles = reportSourceTitles(view);
  const planNodes = planNodesFromView(view);
  const nodeNumbers = new Map(planNodes.map((node, index) => [node.id, index + 1]));
  const nodeObjectives = new Map(planNodes.map((node) => [node.id, node.objective]));
  const workerSids = view.timeline.flatMap((item) =>
    item.t === "sub" && view.subagents[item.sid] ? [item.sid] : []
  );
  const firstWorkerIndex = view.timeline.findIndex((item) => item.t === "sub");

  function setAllWorkers(open: boolean) {
    setCollapseOverrides((current) => ({
      ...current,
      ...Object.fromEntries(workerSids.map((sid) => [sid, open])),
    }));
  }

  useEffect(() => {
    const onScroll = (ev: Event) => {
      const el = endRef.current;
      if (!el) return;
      const target = ev.target as HTMLElement | Document;
      const top = target instanceof Document
        ? (document.scrollingElement?.scrollTop ?? 0)
        : target.scrollTop ?? 0;
      const goingUp = top < lastTopRef.current - 2;
      lastTopRef.current = top;
      const nearEnd = el.getBoundingClientRect().top < window.innerHeight + 240;
      if (nearEnd) {
        pinnedRef.current = true;
        setShowJump(false);
      } else if (goingUp) {
        pinnedRef.current = false;
        setShowJump(true);
      }
    };
    // capture:滚动发生在内层 .stage-main 容器上,冒泡不经过 window
    window.addEventListener("scroll", onScroll, true);
    return () => window.removeEventListener("scroll", onScroll, true);
  }, []);

  // 换 run/重放会整体重置 view(timeline 长度回落)——恢复跟随,别带着上一场的翻阅状态。
  const lastLenRef = useRef(0);
  useEffect(() => {
    if (view.timeline.length < lastLenRef.current) {
      pinnedRef.current = true;
      setShowJump(false);
    }
    lastLenRef.current = view.timeline.length;
    if (pinnedRef.current) endRef.current?.scrollIntoView({ block: "end" });
  }, [view.timeline.length, view.reportMd]);

  function jumpToEnd() {
    pinnedRef.current = true;
    setShowJump(false);
    endRef.current?.scrollIntoView({ block: "end" });
  }

  if (view.timeline.length === 0) {
    // 计划/派发阶段还没有事件——空白会被当成卡死(用户实测取消过),给阶段感知占位
    const live = running;
    const msg = !live ? "本次研究没有过程记录"
      : view.stage === "plan" ? "正在生成研究计划…"
      : view.stage === "dispatch" ? "正在准备首轮研究任务…"
      : "研究进行中…";
    return (
      <div className="flow">
        <div className={`process-empty${live ? " process-empty-live" : ""}`}>
          {live && <span className="lamp lamp-current" />}{msg}
        </div>
      </div>
    );
  }

  return (
    <div className="flow">
      {view.timeline.map((it, i) => {
        if (it.t === "sub") {
          const s = view.subagents[it.sid];
          return (
            <div id={`tl-${i}`} key={`sub-${it.sid}`}>
              {i === firstWorkerIndex && workerSids.length > 0 && (
                <div className="sub-collapse-tools" aria-label="研究任务折叠控制">
                  <span>研究任务 · {workerSids.length}</span>
                  <button type="button" onClick={() => setAllWorkers(true)}>全部展开</button>
                  <button type="button" onClick={() => setAllWorkers(false)}>全部折叠</button>
                </div>
              )}
              {s && (
                <SubCard
                  subagent={s}
                  mode={mode}
                  nowMs={nowMs}
                  maxToolCalls={maxToolCalls}
                  nodeNumber={s.nodeId ? nodeNumbers.get(s.nodeId) : undefined}
                  nodeObjective={s.nodeId ? nodeObjectives.get(s.nodeId) : undefined}
                  open={collapseOverrides[s.sid] ?? s.status !== "done"}
                  onOpenChange={(open) => setCollapseOverrides((current) => ({
                    ...current, [s.sid]: open,
                  }))}
                />
              )}
            </div>
          );
        }
        if (it.t === "llm") {
          // 成品档不展示编排 I/O,但锚点 div 始终在场——锚点 id 按时间线下标编号,
          // 缺位会让 FlowRail 的 getElementById 跳转静默失败(实测踩过)。
          return (
            <div className={mode === "debug" ? "flow-llm" : undefined} id={`tl-${i}`} key={`llm-${i}`}>
              {mode === "debug" && <LlmCallsList calls={[it.call]} />}
            </div>
          );
        }
        return (
          <div id={`tl-${i}`} key={`note-${i}`}>
            <NoteBlock card={it.card} view={view} mode={mode} />
          </div>
        );
      })}
      {view.reportMd && (
        <div className="report flow-report" id="tl-report">
          <div className="report-head">
            {view.savedPath && <span className="num report-path">{view.savedPath}</span>}
            <CopyMdButton text={view.reportMd} />
          </div>
          <ReactMarkdown
            remarkPlugins={[remarkGfm, remarkBreaks]}
            components={{
              a: ({ href, children }) => {
                const url = httpUrl(href);
                const visible = textFromNode(children).trim();
                const isBareUrl = !!url && (visible === href || /^https?:\/\//i.test(visible));
                const label = url && isBareUrl
                  ? sourceTitles.get(urlKey(url)) ?? domainOf(url) ?? "查看来源"
                  : children;
                return (
                  <a
                    className={isBareUrl ? "report-source-link" : undefined}
                    href={href}
                    target={url ? "_blank" : undefined}
                    rel={url ? "noopener noreferrer" : undefined}
                    title={isBareUrl ? url : undefined}
                  >
                    {label}
                  </a>
                );
              },
            }}
          >
            {view.reportMd}
          </ReactMarkdown>
        </div>
      )}
      {(view.endedAtMs !== undefined || view.reportMd) && (
        <details className="flow-stats" id="tl-stats">
          <summary className="num"><Icon name="chevron-right" className="summary-chevron" size={13} />运行统计</summary>
          <StatsView view={view} nowMs={nowMs} />
        </details>
      )}
      <div ref={endRef} />
      {showJump && running && (
        <button className="jump-latest" onClick={jumpToEnd}><Icon name="arrow-down" size={14} />跳到最新</button>
      )}
    </div>
  );
}
