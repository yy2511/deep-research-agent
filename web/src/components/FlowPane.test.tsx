import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { initialView, reduce } from "../reducer";
import type { RunView, SubagentOutcome } from "../types";
import { FlowPane } from "./FlowPane";

function render(view: RunView, mode: "debug" | "clean" = "clean") {
  return renderToStaticMarkup(
    <FlowPane view={view} mode={mode} nowMs={1000} maxToolCalls={12} />
  );
}

/** 事件序列直接过 reducer——测试与真实数据通路一致,不手搓 view 内部结构 */
function viewOf(events: any[]): RunView {
  let v = initialView();
  for (const e of events) v = reduce(v, e, (e.ts ?? 0) * 1000);
  return v;
}

describe("FlowPane 编年叙事", () => {
  it("scope→research_plan→任务批次→研究任务→裁决→任务准备按序渲染成一条流", () => {
    const v = viewOf([
      { type: "scope", query: "研究问题X", ts: 0 },
      { type: "research_plan", count: 2, initial_tasks: [
        { id: "t1", node_id: "n1", objective: "初始计划甲", search_query: "q1", boundaries: null },
        { id: "t2", node_id: "n1", objective: "初始计划乙", search_query: "q2", boundaries: null },
      ], plan_nodes: [], ts: 1 },
      { type: "task_batch_dispatched", count: 2, round_index: 0, ts: 2 },
      { type: "subagent_start", sid: "s0", objective: "执行任务甲", ts: 3 },
      { type: "nodes_assessed", ts: 9,
        assessments: [
          { node_id: "criteria", status: "complete", summary: "判据完成" },
          { node_id: "select", status: "partial", summary: "证据不足", gaps: ["缺对比"] },
        ],
        completed_ids: ["criteria"], unresolved_node_ids: ["select"] },
      { type: "ready_set_computed", round_index: 1, pending: 1, reason: "依赖已满足,补对比缺口", ts: 10 },
    ]);
    const html = render(v);
    // 内容在场
    expect(html).toContain("研究问题X");
    expect(html).toContain("初始计划甲");
    expect(html).toContain("执行任务甲");
    expect(html).toContain("criteria");
    expect(html).toContain("判据完成");
    expect(html).toContain("依赖已满足,补对比缺口");
    // 顺序:问题 < 计划 < 任务卡 < 裁决 < 推进
    const iQ = html.indexOf("研究问题X"), iPlan = html.indexOf("初始计划甲"),
      iSub = html.indexOf("执行任务甲"), iAssess = html.indexOf("判据完成"),
      iAdvance = html.indexOf("依赖已满足");
    expect(iQ).toBeLessThan(iPlan);
    expect(iPlan).toBeLessThan(iSub);
    expect(iSub).toBeLessThan(iAssess);
    expect(iAssess).toBeLessThan(iAdvance);
  });

  it("研究规划渲染完整 DAG：节点种类徽章、依赖标注、首轮任务挂靠和锁定提示", () => {
    const v = viewOf([
      { type: "research_plan", count: 2, ts: 1,
        initial_tasks: [
          { id: "t1", node_id: "m1", objective: "任务甲", search_query: "q1", boundaries: null },
          { id: "t2", node_id: "m1", objective: "任务乙", search_query: "q2", boundaries: null },
        ],
        plan_nodes: [
          { id: "m1", objective: "收集出货量口径", kind: "research", dependency_ids: [], acceptance_criteria: "c1" },
          { id: "d1", objective: "选出前5厂商", kind: "decision", dependency_ids: ["m1"], acceptance_criteria: "c2" },
          { id: "m2", objective: "逐厂商深查", kind: "research", dependency_ids: ["d1"], acceptance_criteria: "c3" },
        ],
      },
    ]);
    const html = render(v);
    expect(html).toContain("3 个研究步骤");
    expect(html).toContain("收集出货量口径");
    expect(html).toContain("plan-kind-decision");   // 决策徽章
    expect(html).toContain('title="步骤 1 · 收集出货量口径">步骤 1</span>');
    expect(html).toContain('title="步骤 2 · 选出前5厂商">步骤 2</span>');
    expect(html).toContain("首轮可执行");
    expect(html).toContain("等待前置");
    expect(html).toContain("任务甲");                 // 任务挂靠在场
    expect(html).toContain("等待前置步骤");            // 锁定步骤提示
    expect(html).not.toContain("pms-id");              // 简洁模式隐藏内部 ID
  });

  it("research_plan 初始任务指向计划外节点时仍诚实平铺", () => {
    const v = viewOf([
      { type: "research_plan", count: 1, ts: 1,
        initial_tasks: [{ id: "t1", node_id: "missing", objective: "初始任务", search_query: "q", boundaries: null }],
        plan_nodes: [
          { id: "m1", objective: "根目标", kind: "research", dependency_ids: [], acceptance_criteria: "c" },
        ] },
    ]);
    const html = render(v);
    expect(html).toContain("根目标");
    expect(html).toContain("初始任务");
    expect(html).not.toContain("等待前置步骤");        // 根节点不该被误标锁定
  });

  it("节点验收显示各节点状态与缺口", () => {
    const v = viewOf([
      { type: "nodes_assessed", ts: 1,
        assessments: [
          { node_id: "m1", status: "complete", summary: "已覆盖" },
          { node_id: "m2", status: "partial", summary: "证据不足", gaps: ["缺腾讯混元"] },
        ],
        completed_ids: ["m1"], unresolved_node_ids: ["m2"] },
    ]);
    const html = render(v);
    expect(html).toContain("步骤验收");
    expect(html).toContain("m1");
    expect(html).toContain("完成");
    expect(html).toContain("部分完成");
    expect(html).toContain("仍需补查");
    expect(html).toContain("缺腾讯混元");
    expect(html).toContain("此刻仍未完成：m2");
  });

  it("节点验收单独显示协议错误，不伪装成普通证据缺口", () => {
    const v = viewOf([
      { type: "nodes_assessed", ts: 1,
        assessments: [
          { node_id: "m4", status: "blocked",
            summary: "Completion Assessor 协议错误",
            assessment_contract_error: "results[0].node_id 必须是 m4" },
        ],
        completed_ids: [], unresolved_node_ids: ["m4", "m5"] },
    ]);
    const html = render(v);
    expect(html).toContain("验收协议错误");
    expect(html).toContain("results[0].node_id 必须是 m4");
  });

  it("验收摘要隐藏协议字段黑话，并显示节点类型", () => {
    const v = viewOf([
      { type: "research_plan", ts: 0, initial_tasks: [], plan_nodes: [
        { id: "d1", objective: "作出选择", kind: "decision", dependency_ids: [], acceptance_criteria: "c" },
      ] },
      { type: "nodes_assessed", ts: 1,
        assessments: [{ node_id: "d1", status: "partial",
          summary: "未达到acceptance_criteria，因此不能complete" }],
        completed_ids: [], unresolved_node_ids: ["d1"] },
    ]);
    const html = render(v);
    expect(html).toContain("验收标准，因此不能完成");
    expect(html).toContain("verdict-kind-decision");
    expect(html).not.toContain("acceptance_criteria");
  });

  it("下游决策基于未完成上游的阶段性证据推进时，明确解释时间快照与降级解锁", () => {
    const v = viewOf([
      { type: "research_plan", ts: 1, initial_tasks: [], plan_nodes: [
        { id: "m1", objective: "收集岗位证据", kind: "research", dependency_ids: [], acceptance_criteria: "20 条" },
        { id: "m2", objective: "形成准备路线", kind: "decision", dependency_ids: ["m1"], acceptance_criteria: "路线完整" },
      ] },
      { type: "nodes_assessed", ts: 2,
        assessments: [{ node_id: "m2", status: "complete", summary: "路线已形成" }],
        completed_ids: ["m2"], unresolved_node_ids: ["m1"] },
    ]);
    const html = render(v);
    expect(html).toContain("阶段性推进");
    expect(html).toContain("步骤 2 已基于步骤 1 的现有证据先行完成");
    expect(html).toContain("步骤 1 仍未完全达到验收标准");
    expect(html).not.toContain("仍在补查");
    expect(html).toContain("此刻仍未完成：步骤 1");
  });

  it("长步骤目标只进悬停说明，不挤压验收摘要", () => {
    const longObjective = "收集中型团队部署 Agent 应用的近期实践方法、验证路径与运行证据要求";
    const v = viewOf([
      { type: "research_plan", ts: 1,
        initial_tasks: [],
        plan_nodes: [
          { id: "deployment-research", objective: longObjective, kind: "research",
            dependency_ids: [], acceptance_criteria: "覆盖五类准备活动" },
        ] },
      { type: "nodes_assessed", ts: 2,
        assessments: [
          { node_id: "deployment-research", status: "complete",
            summary: "授权证据已覆盖五类硬性准备活动" },
        ],
        completed_ids: ["deployment-research"], unresolved_node_ids: [] },
    ]);
    const html = render(v);
    expect(html).toContain(`title="步骤 1 · ${longObjective}"`);
    expect(html).toContain('class="num verdict-id" title=');
    expect(html).toContain(">步骤 1</span>");
    expect(html).toContain("授权证据已覆盖五类硬性准备活动");
  });

  it("报告规划与跨任务一致性检查入流", () => {
    const v = viewOf([
      { type: "report_plan", sections: ["执行摘要", "比较"], n_limitations: 2, ts: 1 },
      { type: "cross_worker_audit", findings: true, reason: "比较维度不足", conflicts: [], ts: 2 },
    ]);
    const html = render(v);
    expect(html).toContain("报告规划");
    expect(html).toContain("2 条局限与后续研究提示");
    expect(html).toContain("跨任务一致性与覆盖检查");
    expect(html).toContain("发现证据覆盖不足或冲突");
    expect(html).toContain("比较维度不足");
  });

  it("轮次从 1 开始展示，最终补查不伪装成普通研究轮次", () => {
    const v = viewOf([
      { type: "task_batch_dispatched", round_index: 0, count: 2, phase: "initial", ts: 1 },
      { type: "research_round_completed", round_index: 0, added: 3, total: 3, remaining: 1, ts: 2 },
      { type: "task_batch_dispatched", round_index: 2, count: 1, phase: "final_research_pass", ts: 3 },
      { type: "research_round_completed", round_index: 2, added: 1, total: 4, remaining: 0,
        phase: "final_research_pass", ts: 4 },
    ]);
    const html = render(v);
    expect(html).toContain("首轮研究 · 并发 2 个任务");
    expect(html).toContain("第 1 轮研究完成");
    expect(html).toContain("最终补查 · 并发 1 个任务");
    expect(html).toContain("最终补查完成");
    expect(html).not.toContain("R0");
  });

  it("报告在流末尾内嵌渲染", () => {
    const v = viewOf([
      { type: "scope", query: "问题", ts: 0 },
      { type: "report_md", markdown: "# 报告标题\n\n正文首段", saved_path: "runs/x/report.md", ts: 5 },
      { type: "done", status: "done", n_evidence: 8, ts: 6 },
    ]);
    const html = render(v);
    expect(html).toContain("报告标题");
    expect(html).toContain("正文首段");
    // 报告出现在 done 标记之后(流的终点是成果)
    expect(html.indexOf("研究完成")).toBeLessThan(html.indexOf("报告标题"));
    // 报告头部一键带走全文
    expect(html).toContain("复制 Markdown");
  });

  it("报告裸链接显示已知文章标题，同时保留完整可点击 URL", () => {
    const longUrl = "https://example.com/job/123?property=%7B%22requestId%22%3A%22very-long-value%22%7D";
    const v = viewOf([
      { type: "subagent_start", sid: "s0", objective: "收集岗位", ts: 0 },
      { type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "search", ok: true,
        args_summary: '{"query":"Agent 招聘"}', result_summary: "{}",
        links: [{ title: "AI Agent 研发工程师招聘", url: longUrl }], evidence_total: 0, ts: 1 },
      { type: "subagent_tool_call", sid: "s0", call_no: 2, tool: "fetch_page", ok: true,
        args_summary: '{"doc_id":"job"}', result_summary: `【AI Agent 研发】(${longUrl})`,
        url: longUrl, evidence_total: 0, ts: 2 },
      { type: "report_md", markdown: `## References\n\n[1] ${longUrl}`, ts: 3 },
    ]);
    const html = render(v);
    expect(html).toContain(`href="${longUrl.replace(/&/g, "&amp;")}"`);
    expect(html).toContain('class="report-source-link"');
    expect(html).toContain(">AI Agent 研发工程师招聘</a>");
    expect(html).not.toContain(`>${longUrl}</a>`);
  });

  it("未知裸链接显示域名兜底，显式 Markdown 标题保持原文", () => {
    const unknownUrl = "https://news.example.org/an/extremely/long/path?token=abc";
    const explicitUrl = "https://docs.example.org/guide";
    const v = viewOf([
      { type: "scope", query: "问题", ts: 0 },
      { type: "report_md", markdown: `[1] ${unknownUrl}\n\n[官方指南](${explicitUrl})`, ts: 1 },
    ]);
    const html = render(v);
    expect(html).toContain(">news.example.org</a>");
    expect(html).toContain(">官方指南</a>");
    expect(html).not.toContain(">docs.example.org</a>");
  });
});

describe("FlowPane 子代理卡", () => {
  function outcomeView(outcome: SubagentOutcome): RunView {
    const v = viewOf([{ type: "subagent_start", sid: "s1", objective: "测试 worker", ts: 0 }]);
    v.subagents.s1 = { ...v.subagents.s1, status: "done", outcome,
      evidenceTotal: outcome === "empty" ? 0 : 2 };
    return v;
  }

  it("ok 绿灯完成;empty 黄灯无证据;failed 红灯", () => {
    expect(render(outcomeView("ok"))).toContain("lamp-done");
    const empty = render(outcomeView("empty"));
    expect(empty).toContain("lamp-warning");
    expect(empty).toContain("无证据");
    expect(render(outcomeView("failed"))).toContain("lamp-failed");
  });

  it("Worker 卡显示所属计划步骤；早期同版轨迹可由首轮 task id 还原", () => {
    const v = viewOf([
      { type: "research_plan", ts: 0,
        initial_tasks: [{ id: "s0", node_id: "m1", objective: "任务", search_query: "q" }],
        plan_nodes: [{ id: "m1", objective: "收集岗位证据", kind: "research", dependency_ids: [], acceptance_criteria: "c" }] },
      // 模拟新增 node_id 前已保存的 schema v2 轨迹：start 本身没有 node_id。
      { type: "subagent_start", sid: "s0", objective: "查招聘 JD", round_index: 0, ts: 1 },
    ]);
    const html = render(v);
    expect(v.subagents.s0.nodeId).toBe("m1");
    expect(html).toContain('class="sub-node" title="收集岗位证据">步骤 1</span>');
    expect(html).toContain("查招聘 JD");
  });

  it("unknown 中性灯不冒充成功", () => {
    const html = render(outcomeView("unknown"));
    expect(html).toContain("lamp-pending");
    expect(html).toContain("结果未知");
    expect(html).not.toContain("lamp-done");
  });

  it("运行中 Worker 默认展开，完成后默认折叠，并提供批量控制", () => {
    const running = viewOf([
      { type: "subagent_start", sid: "running", objective: "正在查证", ts: 0 },
    ]);
    const done = viewOf([
      { type: "subagent_start", sid: "done", objective: "已经查完", ts: 0 },
      { type: "subagent_done", sid: "done", objective: "已经查完", tool_calls: 1,
        evidence_count: 2, status: "ok", ts: 1 },
    ]);

    expect(render(running)).toContain('<details class="sub-block sub-card" open="">');
    expect(render(done)).toContain('<details class="sub-block sub-card">');
    expect(render(done)).not.toContain('<details class="sub-block sub-card" open="">');
    expect(render(running)).toContain("全部展开");
    expect(render(running)).toContain("全部折叠");
  });

  it("新事件摘要在折叠头和展开卡体均有插槽，旧事件不留空占位", () => {
    const summary = "三条证据共同支持该结论，仍需核对发布日期。";
    const current = viewOf([
      { type: "subagent_done", sid: "current", objective: "汇总结论", tool_calls: 2,
        evidence_count: 3, status: "ok", summary, ts: 1 },
    ]);
    const legacy = viewOf([
      { type: "subagent_done", sid: "legacy", objective: "旧回放", tool_calls: 1,
        evidence_count: 1, status: "ok", ts: 1 },
    ]);

    const currentHtml = render(current);
    expect(currentHtml).toContain('class="sub-head-row"');
    expect(currentHtml).toContain('class="sub-meta"');
    expect(currentHtml).toMatch(
      new RegExp(`class="sub-meta">.*</span></span><span class="sub-summary-compact">${summary}</span>`),
    );
    expect(currentHtml).toContain(`class="sub-summary-full">${summary}</p>`);
    expect(render(legacy)).not.toContain("sub-summary-");
  });

  it("search 手牌显示真实 query;save_evidence 显示验收/拒收;不臆造结果数", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "s0", objective: "任务甲", ts: 0 },
      { type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "search", ok: true,
        args_summary: '{"query": "US federal AI executive order 2025"}',
        result_summary: '{"results": [{"doc_id": "d1", "title": "EO 14365 Signals"',
        evidence_total: 0, ts: 1 },
      { type: "subagent_tool_call", sid: "s0", call_no: 2, tool: "save_evidence", ok: true,
        args_summary: '{"items": [{"doc_id": "d1"}]}', result_summary: "已保存",
        accepted: 3, rejected: 1, reject_reasons: ["与目标无关"], evidence_total: 3, ts: 2 },
    ]);
    const html = render(v);
    expect(html).toContain("搜索网页");
    expect(html).toContain("US federal AI executive order 2025");
    expect(html).toContain("收录 3");
    expect(html).toContain("拒收 1");
    expect(html).not.toContain("个结果"); // result_summary 是截断 JSON,真实数不可知——不显示编造计数
  });

  it("结构化证据逐卡显示 claim、来源和可展开原文，并替代 args 摘要", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "s0", objective: "核验结论", ts: 0 },
      { type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "save_evidence", ok: true,
        args_summary: '{"cards":[{"claim":"旧 args 硬抠摘要"}]}', result_summary: "已保存",
        accepted: 1, rejected: 0, evidence_total: 1, ts: 1,
        saved_cards: [{ card_no: 1, claim: "结构化证据结论", support_quote: "服务端逐字原文",
          quote_truncated: true, source_title: null, source_url: "https://source.example/report",
          published_at: "2026-08-06T00:00:00+00:00" }] },
    ]);

    const html = render(v);
    expect(html).toContain('class="evidence-section"');
    expect(html).toContain('证据 <span class="num">· 1</span>');
    expect(html).toContain("[1]");
    expect(html).toContain("结构化证据结论");
    expect(html).toContain('href="https://source.example/report"');
    expect(html).toContain(">source.example</a>");
    expect(html).toContain('dateTime="2026-08-06T00:00:00+00:00">2026-08-06</time>');
    expect(html).toContain('<details class="evidence-quote">');
    expect(html).toContain("服务端逐字原文");
    expect(html).toContain("…（已截断）");
    expect(html).not.toContain("旧 args 硬抠摘要");
    expect(html.indexOf('class="evidence-section"')).toBeLessThan(html.indexOf('class="acts"'));
  });

  it("旧回放无 saved_cards 时不显示空证据区，并保留 args claim 兜底", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "legacy", objective: "旧任务", ts: 0 },
      { type: "subagent_tool_call", sid: "legacy", call_no: 1, tool: "save_evidence", ok: true,
        args_summary: '{"cards":[{"claim":"旧事件第一条"},{"claim":"旧事件第二条"}]}',
        result_summary: "已保存", accepted: 2, rejected: 0, evidence_total: 2, ts: 1 },
    ]);

    const html = render(v);
    expect(html).not.toContain('class="evidence-section"');
    expect(html).toContain("旧事件第一条 等 2 条");
  });

  it("fetch_page 行从自身结果头部提取标题(【**标题】(url) 格式),不落 doc id 兜底;头部 url 成链", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "s0", objective: "任务", ts: 0 },
      { type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "fetch_page", ok: true,
        args_summary: '{"doc_id": "c82b9262"}',
        result_summary: "【**2026年人形机器人量产元年将至 ...】(https://example.com/p/x)\n【可引用原文片段｜保存时填写 excerpt_no】\n[excerpt_no=1] 片段",
        evidence_total: 0, ts: 1 },
    ]);
    const html = render(v);
    expect(html).toContain("2026年人形机器人量产元年将至");
    expect(html).not.toContain("doc c82b9262");
    // 结构化 url 缺失时，从结果头部确认出的完整 url 仍可点击。
    expect(html).toContain('href="https://example.com/p/x"');
  });

  it("search 行:结构化 links 渲染可点外链+域名,行尾真实计数,超 3 条收尾计总", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "s0", objective: "任务", ts: 0 },
      { type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "search", ok: true,
        args_summary: '{"query": "q"}', result_summary: '{"results": []}',
        links: [
          { title: "条目甲", url: "https://a.sohu.com/1" },
          { title: "条目乙", url: "https://b.com/2" },
          { title: "条目丙", url: "https://c.com/3" },
          { title: "条目丁", url: "https://d.com/4" },
          { title: "条目戊", url: "https://e.com/5" },
        ], evidence_total: 0, ts: 1 },
    ]);
    const html = render(v);
    expect(html).toContain('href="https://a.sohu.com/1"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain("a.sohu.com");   // 行内域名标注
    expect(html).toContain("5 结果");        // 真实总数(来自结构化字段,非编造)
    expect(html).toContain("…共 5 条");      // 只平铺前 3 条
    expect(html).not.toContain("条目戊");
  });

  it("无 links 时从截断 result 提取 title+url，只有完整 url 才成链", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "s0", objective: "任务", ts: 0 },
      { type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "search", ok: true,
        args_summary: '{"query": "q"}',
        result_summary: '{"results": [{"doc_id": "d1", "title": "完整条", "url": "https://x.com/a", "snippet": "s"}, {"doc_id": "d2", "title": "半截条", "url": "https://y.com/incomp',
        evidence_total: 0, ts: 1 },
    ]);
    const html = render(v);
    expect(html).toContain('href="https://x.com/a"');
    expect(html).toContain("半截条");                    // 标题完整仍展示
    expect(html).not.toContain("y.com/incomp");          // 未闭合 URL 不当真
    expect(html).not.toContain("结果</span>");           // 截断数据不编造计数
  });

  it("fetch_page 摘要中的括号 URL（如 Wikipedia 消歧义）不截断", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "s0", objective: "任务", ts: 0 },
      { type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "fetch_page", ok: true,
        args_summary: '{"doc_id": "w"}',
        result_summary: "【Rust (programming language) - Wikipedia】(https://en.wikipedia.org/wiki/Rust_(programming_language))\n…",
        evidence_total: 0, ts: 1 },
    ]);
    const html = render(v);
    expect(html).toContain('href="https://en.wikipedia.org/wiki/Rust_(programming_language)"');
  });

  it("fetch_page 行:结构化 url/n_excerpts 渲染 片段计数+域名", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "s0", objective: "任务", ts: 0 },
      { type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "fetch_page", ok: true,
        args_summary: '{"doc_id": "d9"}',
        result_summary: "【标题甲】(https://post.m.smzdm.com/p/x)\n…",
        url: "https://post.m.smzdm.com/p/x", n_excerpts: 4,
        evidence_total: 0, ts: 1 },
    ]);
    const html = render(v);
    expect(html).toContain('href="https://post.m.smzdm.com/p/x"');
    expect(html).toContain("片段 4");
    expect(html).toContain("post.m.smzdm.com");
  });

  it("save_evidence 全拒时不显示收录 0,只显示拒收", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "s0", objective: "任务", ts: 0 },
      { type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "save_evidence", ok: true,
        args_summary: '{"items": []}', result_summary: "全拒",
        accepted: 0, rejected: 2, reject_reasons: ["与目标无关"], evidence_total: 0, ts: 1 },
    ]);
    const html = render(v);
    expect(html).not.toContain("收录 0");
    expect(html).toContain("拒收 2");
  });

  it("油表格子带悬停释义:已用格 #n·工具·结果,保留区空格解释规则", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "s0", objective: "任务", ts: 0 },
      { type: "subagent_tool_call", sid: "s0", call_no: 1, tool: "search", ok: true,
        args_summary: '{"query": "q"}', result_summary: '{"results": []}', evidence_total: 0, ts: 1 },
    ]);
    const html = render(v);
    expect(html).toContain('title="#1 · 搜索网页 · 成功"');
    expect(html).toContain("预留额度：用于保存证据或结束任务");
    expect(html).toContain("工具额度");
  });

  it("调试档显示 Worker 原始 I/O 与 token,成品档零泄露", () => {
    const v = viewOf([
      { type: "subagent_start", sid: "w", objective: "测试", ts: 0 },
      { type: "llm_call", sid: "w", step: "tool_loop", model: "m", ms: 12,
        in_tok: 321, out_tok: 45, input: "原始 Worker 输入", output: "原始 Worker 输出", ts: 1 },
    ]);
    const dbg = render(v, "debug");
    expect(dbg).toContain("↑321 ↓45");
    expect(dbg).toContain("原始 Worker 输入");
    const clean = render(v, "clean");
    expect(clean).not.toContain("原始 Worker 输入");
  });
});

describe("FlowPane 编排级 LLM I/O", () => {
  const events = [
    { type: "llm_call", step: "assess_nodes", model: "glm-5.2", ms: 1200,
      input: "acceptance_criteria=至少覆盖三个维度", output: '{"status":"complete"}',
      io_complete: true, ts: 1 },
  ];

  it("调试档原位内联展示编排调用 I/O", () => {
    const html = render(viewOf(events), "debug");
    expect(html).toContain("验收研究步骤");
    expect(html).toContain("acceptance_criteria=至少覆盖三个维度");
    expect(html).toContain('<details class="llm-call">');
    expect(html).toContain("完整 I/O");
  });

  it("成品档不暴露编排原始 I/O", () => {
    const html = render(viewOf(events), "clean");
    expect(html).not.toContain("acceptance_criteria=至少覆盖三个维度");
  });
});

describe("FlowPane 锚点完整性", () => {
  it("成品档下 llm 项也渲染锚点 div——缺位会让 FlowRail 跳转静默失败", () => {
    const v = viewOf([
      { type: "scope", query: "问题", ts: 0 },
      { type: "llm_call", step: "assess_nodes", model: "m", ms: 1, input: "in", output: "out", ts: 1 },
      { type: "nodes_assessed", assessments: [], completed_ids: [], unresolved_node_ids: [], ts: 2 },
    ]);
    const html = render(v, "clean");
    // timeline 下标 0/1/2 全部有锚,含成品档不展示内容的 llm 项
    expect(html).toContain('id="tl-0"');
    expect(html).toContain('id="tl-1"');
    expect(html).toContain('id="tl-2"');
    // 且成品档不泄露 I/O
    expect(html).not.toContain("assess_nodes");
  });
});

describe("FlowPane 空态", () => {
  it("live 无内容时给阶段感知占位,不是吓人的空白", () => {
    const v = viewOf([]);
    v.startedAtMs = 0; // 视为 live
    const html = render(v);
    expect(html).toContain("研究");
  });
});
