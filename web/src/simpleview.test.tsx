import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { activityState,answerWithCitations,buildRunIndex,toolFacts,toolStatus } from "./components/conversationHelpers";
import { ConversationWorkspace } from "./components/ConversationWorkspace";
import { initialView, missingProcessNotice, planOpen, reduce, BatchGuard, mergeSimpleResult } from "./reducer";
import type { RunView, ToolCall } from "./types";

/** A controlled event sequence exercises incremental messages and pre-display tool replies. */
let eventSeq = 0;
const event = (fields: Record<string, unknown>) => ({
  seq: ++eventSeq,
  ts: 1789457000 + eventSeq,
  ...fields,
});
const raw = [
  ...Array.from({ length: 5 }, (_, i) => {
    const msgId = `message-${i}`;
    return [
      event({ type: "lead_message", msg_id: msgId, mode: "append", part_seq: 0,
        text: i === 0 ? "正在核对 Redis" : `继续核对第 ${i} 项。`, turn: i, role: "lead" }),
      ...(i === 0 ? [event({ type: "lead_message", msg_id: msgId, mode: "append",
        part_seq: 1, text: " 文档。", turn: i, role: "lead" })] : []),
      event({ type: "lead_message", msg_id: msgId, mode: "complete",
        part_seq: i === 0 ? 2 : 1, complete: true, turn: i, role: "lead" }),
    ];
  }).flat(),
  ...Array.from({ length: 12 }, (_, i) => {
    const tool = i === 0 ? "search_sources" : i === 11 ? "finish_research" : "read_source";
    const shared = { type: "subagent_tool_call", call_id: `call-${i}`, call_no: i + 1,
      turn: 1, role: "lead", tool, args_summary: i === 0
        ? '{"query":"site:redis.io persistence"}' : "{}" };
    return [
      event({ ...shared, phase: "running", status: "running", ok: false }),
      event({ ...shared, phase: "done", status: "ok", ok: true,
        result_summary: i === 0 ? '{"results":[' : "{}" }),
    ];
  }).flat(),
];

function viewFromEvents(events: any[]): RunView {
  return events.reduce((v, e) => reduce(v, e, e.ts * 1000), initialView());
}

const liveView = viewFromEvents(raw);
const liveIndex = buildRunIndex(liveView);

function render(view: RunView, mode: "clean" | "debug" = "clean") {
  return renderToStaticMarkup(<ConversationWorkspace view={view} runId="q3" mode={mode} />);
}

describe("受控事件序：过程流", () => {
  it.each([true, false])("重复引文待选出处，不显示拒收或误称已存卡（display=%s）", (structured) => {
    const call: ToolCall = {
      callNo: 1, tool: "save_evidence", ok: true, phase: "done", status: "ok",
      args: '{"cards":[{"claim":"结论","quote":"原话"}]}',
      result: JSON.stringify({results:[{accepted:false,status:"needs_selection",reason:"原话存在多处"}]}),
      atMs: 0,
      ...(structured ? {display:{accepted:0,rejected:0,pending_selection:1,submitted:1,saved_total:0}} : {}),
    };
    const facts = toolFacts(call, liveIndex);
    expect(facts.counters).toContain("待选出处 1");
    expect(facts.counters.some((v) => v.startsWith("拒收"))).toBe(false);
    expect(toolStatus(call)).toEqual({label:"待选出处",tone:"warn"});
  });

  it("lead_message 按到达顺序成块，文本由增量拼成真实句子", () => {
    const messages = liveView.leadMessages ?? [];
    expect(messages.length).toBeGreaterThanOrEqual(5);
    // 首条说明由两个增量片段拼接。
    expect(messages[0].text).toContain("Redis");
    expect(messages.every((m) => m.complete)).toBe(true);
    expect(messages.every((m) => !m.gap)).toBe(true);
  });

  it("工具卡开工/结束合成一张，状态与回执一致", () => {
    const tools = Object.values(liveView.simpleTools ?? {});
    expect(tools.length).toBeGreaterThan(10);
    expect(tools.every((t) => t.phase === "done")).toBe(true);
    expect(tools.every((t) => t.status === "ok")).toBe(true);
    expect(tools.every((t) => toolStatus(t).tone === "ok")).toBe(true);
    expect(tools.some((t) => t.tool === "search_sources")).toBe(true);
    expect(tools.some((t) => t.tool === "finish_research")).toBe(true);
  });

  it("旧事件没有 display：不猜造 URL，但搜索词仍可读", () => {
    const search = Object.values(liveView.simpleTools ?? {})
      .find((t) => t.tool === "search_sources")!;
    expect(search.display).toBeUndefined();
    const facts = toolFacts(search, liveIndex);
    // 参数里的检索词
    expect(facts.main).toContain("redis.io");
    // 结果摘要被截断：如实说明，且绝不凭半截 JSON 造 URL
    expect(facts.warn).toBeTruthy();
    expect(facts.mainHref).toBeUndefined();
    expect(facts.lines.every((line) => !line.href)).toBe(true);
  });
});

describe("交付正文被引用门拦下：说清原因", () => {
  /** 引用门是机械事实（编号对不上已保存卡片），不依赖任何模型回执。 */
  function rejected(note: string): RunView {
    return mergeSimpleResult(liveView, {
      query: "Q", stop_reason: "finished", delivery_status: "limited",
      research_coverage: {},
      draft: "结论 [c27]。", markdown: "", draft_rejected: true,
      draft_rejected_note: note, cards: [], sources: [],
    }, { status: "done", config_summary: { mode: "simple" } });
  }

  it("引用门触发时说引用，不冒充核查结论", () => {
    const html = render(rejected("正文引用了不存在的卡片：[c27]，不作为答案发布。"));
    expect(html).toContain("不存在的卡片：[c27]");
  });
});

describe("受控事件序：答案与材料", () => {
  const finished: RunView = mergeSimpleResult(liveView, {
    query: "Q", stop_reason: "finished", delivery_status: "limited",
    research_coverage: { goal: "answered" },
    draft: "候选正文 [c1]。", markdown: "# Q\n\n候选正文 [c1]。",
    cards: [{ card_id: "c1", claim: "RDB 是时间点快照", quote: "RDB is a point-in-time snapshot",
              source_ref: "s1", source_url: "https://redis.io/docs/latest/" }],
    sources: [{ ref: "s1", url: "https://redis.io/docs/latest/", title: "Redis persistence | Docs",
                snapshot_kind: "full", raw_chars: 26598 }],
  }, { status: "done", finished_at: 1789457356.5, started_at: 1789457091.8,
       config_summary: { mode: "simple" } });

  it("正文只出现一份：报告一份，交付正文不在别处重复", () => {
    const html = render(finished);
    expect(html).toContain('class="cw-answer cw-markdown"');
    expect(html.split("候选正文").length - 1).toBe(1);
  });

  it("页面上不暴露服务器 report.md 绝对路径", () => {
    const withPath = reduce(finished, { type: "report_md", markdown: finished.reportMd,
      saved_path: "/srv/dra/runs/q3/report.md" }, 1789457356500);
    const html = render(withPath);
    expect(html).not.toContain("/srv/dra/runs");
  });

  it("[cN] 渲染成行内按钮，展开内容不在 <p> 内（合法 HTML 结构）", () => {
    const html = render(finished);
    expect(html).toContain('class="cite cite-btn"');
    // 段落里不得出现块级元素（浏览器会对 <details>/<blockquote> 报无效嵌套）
    expect(html).not.toMatch(/<p>[^<]*<(details|blockquote|div|aside)\b/);
  });

});

describe("行内引用在各种 Markdown 结构里都可点", () => {

  const index = buildRunIndex({
    ...initialView(),
    simple: {
      mode: "simple", phase: "finished",
      cards: [{ card_id: "c1", claim: "claim", quote: "quote", source_ref: "s1",
                source_url: "https://e/1", location: "chars=0-10" }],
      sources: [{ ref: "s1", url: "https://e/1", title: "来源", snapshot_kind: "full",
                  raw_chars: 10 }],
    },
  });

  const clickable = (md: string) => {
    const html = renderToStaticMarkup(<>{answerWithCitations(md, index)}</>);
    return { html, count: html.split('class="cite cite-btn"').length - 1 };
  };

  it("粗体 / 斜体 / 表格单元格里的 [cN] 都渲染成按钮", () => {
    expect(clickable("**Evidence [c1]**").count).toBe(1);
    expect(clickable("*见 [c1]*").count).toBe(1);
    expect(clickable("| a |\n| --- |\n| [c1] |").count).toBe(1);
    expect(clickable("- 列表项 [c1]").count).toBe(1);
  });

  it("一行多个引用各生成一个按钮", () => {
    expect(clickable("先 [c1] 后 [c1]").count).toBe(2);
  });

  it("链接与代码里的 [cN] 不生成嵌套按钮（保持原语义）", () => {
    const link = clickable("[看这里 [c1]](https://example.org/)");
    expect(link.count).toBe(0);
    expect(link.html).toContain('href="https://example.org/"');
    const code = clickable("`[c1]`");
    expect(code.count).toBe(0);
    expect(code.html).toContain("<code>[c1]</code>");
  });

  it("未知编号仍是不可点的标记，不假装有卡片", () => {
    const unknown = clickable("**看 [c9]**");
    expect(unknown.count).toBe(0);
    expect(unknown.html).toContain("cite-missing");
  });
});

describe("等待状态：不把核查说成搜索", () => {
  it("提交答案后显示保存结果，不声称执行已移除的独立核查", () => {
    const tools: Record<string, ToolCall> = {
      "1:c1": { callNo: 1, tool: "finish_research", ok: true, phase: "done", status: "ok",
                args: "{}", result: "", atMs: 0 },
    };
    const state = activityState({
      ...initialView(), startedAtMs: 1000,
      timeline: [{ t: "tool", key: "1:c1", atMs: 1000 }], simpleTools: tools,
      simple: { mode: "simple", phase: "research" },
    });
    expect(state?.text).toContain("保存结果");
    expect(state?.text).not.toContain("独立核查");
    expect(state?.text).not.toContain("下一步动作");
  });

  it("有工具在跑时显示该工具（中文）", () => {
    const tools: Record<string, ToolCall> = {
      "1:c1": { callNo: 1, tool: "read_source", ok: false, phase: "running", status: "running",
                args: '{"source_ref": "s1"}', result: "", atMs: 0 },
    };
    const state = activityState({
      ...initialView(), startedAtMs: 1000,
      timeline: [{ t: "tool", key: "1:c1", atMs: 1000 }], simpleTools: tools,
      simple: { mode: "simple", phase: "research" },
    });
    expect(state?.text).toContain("精读原文");
  });

  it("结束后不再显示活动状态", () => {
    expect(activityState({ ...initialView(), startedAtMs: 1, endedAtMs: 2 })).toBeNull();
  });
});

describe("历史兼容：旧运行没有过程事件", () => {
  const base: RunView = missingProcessNotice(
    mergeSimpleResult(initialView(), {
      query: "旧问题", stop_reason: "finished", delivery_status: "limited",
      draft: "旧答案正文", markdown: "# 旧问题\n\n旧答案正文",
      cards: [], sources: [],
    }, { status: "done", config_summary: { mode: "simple" } }),
    { reason: "本次运行没有过程事件记录" },
  );

  it("结果照常可读，并明确说明过程记录缺失（不伪造过程）", () => {
    const html = render(base);
    expect(html).toContain("旧答案正文");
    expect(html).toContain("本次运行没有过程事件记录");
    expect(base.timeline).toHaveLength(0);
  });

  it("旧协议不可回放时仍打开结果", () => {
    expect(planOpen("old", { status: "done", replay_compatible: false }, { hasEvents: false }))
      .toEqual({ kind: "result-only", runId: "old", reason: "旧事件协议不可回放" });
  });
});

describe("批次守卫（纯逻辑）", () => {
  it("同一次导航的第二步复用批次", () => {
    const guard = new BatchGuard();
    const batch = guard.begin("run-A");
    expect(guard.currentOrBegin("run-A")).toBe(batch);
    expect(guard.currentOrBegin("run-B")).not.toBe(batch);
  });
});
