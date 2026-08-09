import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { initialView, reduce } from "./reducer";

const lines = readFileSync(new URL("../../fixtures/demo_run/events.jsonl", import.meta.url), "utf-8")
  .trim().split("\n").map((l) => JSON.parse(l));

function finalView() {
  let v = initialView();
  for (const e of lines) v = reduce(v, e, e.ts * 1000);
  return v;
}

describe("reducer", () => {
  it("回放 fixture 到终态", () => {
    const v = finalView();
    expect(v.stage).toBe("done");
    expect(v.banner?.kind).toBe("done");
    expect(v.banner?.text).toContain("跨任务检查发现覆盖不足或冲突");
    expect(Object.keys(v.subagents).length).toBeGreaterThanOrEqual(3);
    expect(v.reportMd).toContain("#");
    const loopAgents = Object.values(v.subagents).filter((s) => s.mode === "loop");
    expect(loopAgents.length).toBeGreaterThanOrEqual(2);
    // fixture 里带拒收样本的是 s1（不是插入顺序第一个 s0），按实际数据断言，不假设固定下标
    expect(loopAgents.some((a) => a.calls.some((c) => c.tool === "save_evidence" && (c.rejected ?? 0) > 0))).toBe(true);
    expect(v.stats.tools["save_evidence"].rejected).toBeGreaterThan(0);
  });
  it("seq 重复丢弃（SSE 重连去重兜底）", () => {
    let v = initialView();
    v = reduce(v, lines[0], 0);
    const before = v;
    v = reduce(v, lines[0], 0);
    expect(v).toBe(before);
  });
  it("running 中活跃步骤有计时锚点", () => {
    let v = initialView();
    for (const e of lines.slice(0, 12)) v = reduce(v, e, e.ts * 1000);
    const running = Object.values(v.subagents).find((s) => s.status === "running");
    expect(running?.currentSinceMs).toBeGreaterThan(0);
  });
});

describe("subagent lifecycle and outcome", () => {
  it.each([
    ["ok", "ok"],
    ["empty", "empty"],
    ["timeout", "timeout"],
    ["failed", "failed"],
    ["hard_error", "hard_error"],
  ] as const)("把新版 subagent_done status=%s 保存为 outcome，不混入生命周期", (eventStatus, outcome) => {
    let v = reduce(initialView(), {
      type: "subagent_start", sid: `s-${eventStatus}`, objective: "测试 worker",
    }, 0);
    v = reduce(v, {
      type: "subagent_done", sid: `s-${eventStatus}`, objective: "测试 worker",
      tool_calls: 1, evidence_count: eventStatus === "empty" ? 0 : 2, status: eventStatus,
    }, 1000);

    expect(v.subagents[`s-${eventStatus}`].status).toBe("done");
    expect(v.subagents[`s-${eventStatus}`].outcome).toBe(outcome);
  });

  it("subagent_done 没有 status 时保留为 unknown，而不是冒充 ok", () => {
    const v = reduce(initialView(), {
      type: "subagent_done", sid: "missing-status", objective: "缺状态 worker",
      tool_calls: 1, evidence_count: 3,
    }, 1000);

    expect(v.subagents["missing-status"].status).toBe("done");
    expect(v.subagents["missing-status"].outcome).toBe("unknown");
  });

  it("保存 Worker 的停止检索原因，供 live 与回放界面展示", () => {
    let v = reduce(initialView(), {
      type: "subagent_start", sid: "budget", objective: "测试 worker",
    }, 0);
    v = reduce(v, {
      type: "subagent_done", sid: "budget", objective: "测试 worker",
      tool_calls: 6, evidence_count: 2, status: "ok", stop_reason: "tool_budget",
    }, 1000);

    expect(v.subagents.budget.stopReason).toBe("tool_budget");
  });

  it("subagent_done 可选 summary 入状态，旧回放缺字段时保持 undefined", () => {
    const withSummary = reduce(initialView(), {
      type: "subagent_done", sid: "new-run", objective: "测试 worker",
      tool_calls: 2, evidence_count: 3, status: "ok", summary: "已找到三条相互印证的证据。",
    }, 1000);
    const legacy = reduce(initialView(), {
      type: "subagent_done", sid: "legacy-run", objective: "旧 worker",
      tool_calls: 1, evidence_count: 1, status: "ok",
    }, 1000);

    expect(withSummary.subagents["new-run"].summary).toBe("已找到三条相互印证的证据。");
    expect(legacy.subagents["legacy-run"].summary).toBeUndefined();
  });

  it("把 Worker llm_call 的原始 I/O 与 token 保存在对应卡片", () => {
    let v = reduce(initialView(), {
      type: "subagent_start", sid: "worker-io", objective: "测试 worker",
    }, 0);
    v = reduce(v, {
      type: "llm_call", sid: "worker-io", step: "tool_loop", worker_iteration: 2,
      model: "deepseek-v4-flash", ms: 1234, in_tok: 321, out_tok: 45,
      input: "原始 Worker 输入", output: "原始 Worker 输出",
      io_complete: true,
    }, 1000);

    expect(v.subagents["worker-io"].llmCalls).toEqual([expect.objectContaining({
      step: "tool_loop",
      inTok: 321,
      outTok: 45,
      input: "原始 Worker 输入",
      output: "原始 Worker 输出",
      workerIteration: 2,
      ioComplete: true,
    })]);
    // Worker 级调用不进编年流——它属于子代理卡内部
    expect(v.timeline.filter((it) => it.t === "llm")).toHaveLength(0);
  });
});

describe("timeline 编年流", () => {
  /** timeline 项的紧凑指纹,便于断言顺序 */
  function keys(v: ReturnType<typeof initialView>): string[] {
    return v.timeline.map((it) =>
      it.t === "note" ? `note:${it.card.kind}` : it.t === "sub" ? `sub:${it.sid}` : `llm:${it.call.step}`
    );
  }

  it("编排事件按到达顺序进 timeline,子代理首现挂锚点", () => {
    let v = initialView();
    v = reduce(v, { type: "scope", query: "研究问题" }, 0);
    v = reduce(v, { type: "research_plan", count: 2, initial_tasks: [
      { id: "t1", node_id: "n1", objective: "a", search_query: "a", boundaries: null },
      { id: "t2", node_id: "n1", objective: "b", search_query: "b", boundaries: null },
    ], plan_nodes: [] }, 100);
    v = reduce(v, { type: "task_batch_dispatched", count: 2, round_index: 0 }, 200);
    v = reduce(v, { type: "subagent_start", sid: "s0", objective: "任务A" }, 300);
    v = reduce(v, { type: "subagent_start", sid: "s1", objective: "任务B" }, 400);
    v = reduce(v, {
      type: "nodes_assessed",
      assessments: [{ node_id: "m1", status: "complete", summary: "判据完成" }],
      completed_ids: ["m1"], unresolved_node_ids: ["m2"],
    }, 500);
    v = reduce(v, { type: "ready_set_computed", round_index: 1, pending: 1, reason: "补缺口" }, 600);
    v = reduce(v, { type: "task_batch_dispatched", round_index: 1, count: 1, objectives: ["筛选候选"] }, 700);
    v = reduce(v, { type: "subagent_start", sid: "w1", objective: "筛选候选", round_index: 1 }, 800);
    v = reduce(v, { type: "research_round_completed", round_index: 1, added: 3, total: 8, remaining: 0 }, 900);

    expect(keys(v)).toEqual([
      "note:scope", "note:research_plan", "note:task_batch_dispatched", "sub:s0", "sub:s1",
      "note:nodes_assessed", "note:ready_set_computed", "note:task_batch_dispatched",
      "sub:w1", "note:research_round_completed",
    ]);
    expect(v.stage).toBe("research");
    expect(v.subagents.w1.roundIndex).toBe(1);
    // 裁决 note 保留完整载荷供渲染
    const assess = v.timeline.find((it) => it.t === "note" && it.card.kind === "nodes_assessed");
    expect(assess?.t === "note" && assess.card.completed_ids).toEqual(["m1"]);
  });

  it("Worker 记录 node_id，并从 research_plan 为早期首轮事件回填步骤归属", () => {
    let v = reduce(initialView(), { type: "research_plan", initial_tasks: [
      { id: "old-task", node_id: "m1", objective: "旧任务" },
    ], plan_nodes: [] }, 0);
    v = reduce(v, { type: "subagent_start", sid: "old-task", objective: "旧任务", round_index: 0 }, 1);
    v = reduce(v, { type: "subagent_start", sid: "new-task", objective: "新任务",
      node_id: "m2", round_index: 1 }, 2);
    expect(v.researchTaskNodeIds).toEqual({ "old-task": "m1" });
    expect(v.subagents["old-task"].nodeId).toBe("m1");
    expect(v.subagents["new-task"].nodeId).toBe("m2");
  });

  it("同版早期动态 Worker 仅在剩余 research 步骤唯一时确定性回填归属", () => {
    let v = reduce(initialView(), { type: "research_plan", initial_tasks: [], plan_nodes: [
      { id: "m1", kind: "research" }, { id: "m2", kind: "decision" },
    ] }, 0);
    v = reduce(v, { type: "nodes_assessed", assessments: [], completed_ids: [],
      unresolved_node_ids: ["m1", "m2"] }, 1);
    v = reduce(v, { type: "subagent_start", sid: "dynamic", objective: "补查", round_index: 1 }, 2);
    expect(v.subagents.dynamic.nodeId).toBe("m1");

    let ambiguous = reduce(initialView(), { type: "research_plan", initial_tasks: [], plan_nodes: [
      { id: "a", kind: "research" }, { id: "b", kind: "research" },
    ] }, 0);
    ambiguous = reduce(ambiguous, { type: "nodes_assessed", assessments: [], completed_ids: [],
      unresolved_node_ids: ["a", "b"] }, 1);
    ambiguous = reduce(ambiguous, { type: "subagent_start", sid: "unknown", objective: "补查", round_index: 1 }, 2);
    expect(ambiguous.subagents.unknown.nodeId).toBeUndefined();
  });

  it("tool_call 只更新既有卡片,不追加 timeline 项;首事件缺 start 时兜底建锚", () => {
    let v = initialView();
    v = reduce(v, { type: "subagent_start", sid: "s0", objective: "任务A" }, 0);
    const lenAfterStart = v.timeline.length;
    v = reduce(v, {
      type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "search", ok: true,
      args_summary: "{\"query\": \"q\"}", result_summary: "", evidence_total: 0,
    }, 100);
    expect(v.timeline.length).toBe(lenAfterStart);
    expect(v.subagents.s0.calls).toHaveLength(1);

    // 乱序兜底:start 丢失时,首个 tool_call 也要建出锚点(回放健壮性)
    let v2 = initialView();
    v2 = reduce(v2, {
      type: "subagent_tool_call", sid: "ghost", call_no: 1, tool: "search", ok: true,
      args_summary: "{}", result_summary: "", evidence_total: 0,
    }, 0);
    expect(v2.timeline).toEqual([expect.objectContaining({ t: "sub", sid: "ghost" })]);
  });

  it("subagent_tool_call 的结构化来源字段(links/url/n_excerpts)映射进 ToolCall", () => {
    let v = initialView();
    v = reduce(v, { type: "subagent_start", sid: "s0", objective: "任务" }, 0);
    v = reduce(v, {
      type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "search", ok: true,
      args_summary: "{}", result_summary: "{…", evidence_total: 0,
      links: [{ title: "T1", url: "https://a.com/x" }],
    }, 1);
    v = reduce(v, {
      type: "subagent_tool_call", sid: "s0", call_no: 2, tool: "fetch_page", ok: true,
      args_summary: "{}", result_summary: "【T1】(https://a.com/x)", evidence_total: 0,
      url: "https://a.com/x", n_excerpts: 4,
    }, 2);
    const [c1, c2] = v.subagents.s0.calls;
    expect(c1.links).toEqual([{ title: "T1", url: "https://a.com/x" }]);
    expect(c2.url).toBe("https://a.com/x");
    expect(c2.nExcerpts).toBe(4);
  });

  it("saved_cards 映射进 Worker 并按 cardNo 幂等去重", () => {
    let v = reduce(initialView(), { type: "subagent_start", sid: "s0", objective: "任务" }, 0);
    const event = {
      type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "save_evidence", ok: true,
      args_summary: "{}", result_summary: "{}", evidence_total: 2,
      saved_cards: [
        { card_no: 1, claim: "结论一", support_quote: "原文一", quote_truncated: false,
          source_title: "来源一", source_url: "https://a.example/1", published_at: "2026-08-06" },
        { card_no: 2, claim: "结论二", support_quote: "原文二", quote_truncated: true,
          source_title: null, source_url: "https://b.example/2", published_at: null },
        { card_no: 2, claim: "重复卡", support_quote: "不应进入", quote_truncated: false },
      ],
    };

    v = reduce(v, event, 1);
    v = reduce(v, event, 2); // 无 seq 的重复投递也不能重复追加证据

    expect(v.subagents.s0.cards).toEqual([
      { cardNo: 1, claim: "结论一", supportQuote: "原文一", quoteTruncated: false,
        sourceTitle: "来源一", sourceUrl: "https://a.example/1", publishedAt: "2026-08-06" },
      { cardNo: 2, claim: "结论二", supportQuote: "原文二", quoteTruncated: true,
        sourceTitle: null, sourceUrl: "https://b.example/2", publishedAt: null },
    ]);
  });

  it("同 sid 重复 start 不产生第二个锚点", () => {
    let v = initialView();
    v = reduce(v, { type: "subagent_start", sid: "s0", objective: "任务A" }, 0);
    v = reduce(v, { type: "subagent_start", sid: "s0", objective: "任务A" }, 100);
    expect(v.timeline.filter((it) => it.t === "sub")).toHaveLength(1);
  });

  it("无 sid 的 llm_call 作为编排 I/O 进 timeline", () => {
    let v = initialView();
    v = reduce(v, {
      type: "llm_call", step: "assess_nodes", model: "m", ms: 900,
      in_tok: 10, out_tok: 5, input: "in", output: "out",
    }, 0);
    expect(v.timeline).toEqual([
      expect.objectContaining({ t: "llm", call: expect.objectContaining({ step: "assess_nodes" }) }),
    ]);
  });

  it("终态事件落 timeline 终点标记", () => {
    let v = initialView();
    v = reduce(v, { type: "done", status: "done", n_evidence: 8 }, 1000);
    expect(keys(v)).toEqual(["note:done"]);

    let v2 = initialView();
    v2 = reduce(v2, { type: "error", message: "boom" }, 1000);
    expect(keys(v2)).toEqual(["note:error"]);
  });

  it("结构检查事件推进顶部阶段", () => {
    const v = reduce(initialView(), {
      type: "shape_gate", phase: "initial", missing: [],
    }, 1000);
    expect(v.stage).toBe("check");
  });
});

describe("partial completion honesty", () => {
  it("DONE status=partial 显示部分完成与未完成节点数", () => {
    const v = reduce(initialView(), {
      type: "done", status: "partial", n_evidence: 8,
      unresolved_node_ids: ["m1", "m2"],
    }, 1000);
    expect(v.banner?.kind).toBe("partial");
    expect(v.banner?.text).toContain("部分完成");
    expect(v.banner?.text).toContain("2 个研究步骤未完成");
  });

  it("DONE partial 显示逐节点协议错误与依赖阻塞原因", () => {
    const v = reduce(initialView(), {
      type: "done", status: "partial", n_evidence: 8,
      unresolved_node_ids: ["m4", "m5"],
      completion_blockers: ["unresolved_plan_nodes"],
      warnings: ["assessment_contract_error"],
      node_terminal_reasons: {
        m4: "assessment_contract_error:results[0].node_id 必须是 m4",
        m5: "blocked_by_dependencies:m4",
      },
    }, 1000);

    expect(v.banner?.text).toContain("m4：验收回执格式错误");
    expect(v.banner?.text).toContain("m5：等待上游 m4");
  });

  it.each([
    ["unresolved_plan_nodes", "仍有研究步骤未完成"],
    ["report_empty", "报告内容为空"],
  ] as const)("completion_blockers=%s 显示具体原因", (reason, label) => {
    const v = reduce(initialView(), {
      type: "done", status: "partial", n_evidence: 8,
      completion_blockers: [reason],
    }, 1000);

    expect(v.banner?.kind).toBe("partial");
    expect(v.banner?.text).toContain(label);
  });

  it("done + warnings 仍显示完成并附警告数", () => {
    const v = reduce(initialView(), {
      type: "done",
      status: "done",
      n_evidence: 8,
      completion_blockers: [],
      warnings: ["cross_worker_audit_findings", "recovered_worker_failure"],
    }, 1000);

    expect(v.banner?.kind).toBe("done");
    expect(v.banner?.text).toContain("研究完成");
    expect(v.banner?.text).toContain("2 条警告");
    expect(v.banner?.text).toContain("跨任务检查发现覆盖不足或冲突");
  });

  it("新运行显示跨任务一致性检查 warning", () => {
    const v = reduce(initialView(), {
      type: "done",
      status: "done",
      n_evidence: 8,
      completion_blockers: [],
      warnings: ["cross_worker_audit_findings", "cross_worker_audit_skipped"],
    }, 1000);

    expect(v.banner?.text).toContain("跨任务检查发现覆盖不足或冲突");
    expect(v.banner?.text).toContain("跨任务一致性与覆盖检查未完成");
  });

  it("未知 blocker 原样显示，避免静默丢失新后端语义", () => {
    const v = reduce(initialView(), {
      type: "done", status: "partial", n_evidence: 8,
      completion_blockers: ["future_reason"],
    }, 1000);

    expect(v.banner?.text).toContain("future_reason");
  });
});

describe("DONE outcome fail-closed", () => {
  it.each([
    ["done", "done"],
    ["partial", "partial"],
    [undefined, "unknown"],
    ["failed", "unknown"],
    ["future_status", "unknown"],
  ] as const)("status=%s 映射为 banner=%s", (status, expectedKind) => {
    const v = reduce(initialView(), {
      type: "done", status, n_evidence: 3,
    }, 1000);

    expect(v.banner?.kind).toBe(expectedKind);
    expect(v.endedAtMs).toBe(1000);
    if (expectedKind === "unknown") {
      expect(v.banner?.text).toContain("结果状态未知");
    }
  });
});
