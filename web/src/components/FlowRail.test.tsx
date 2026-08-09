import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { initialView, reduce } from "../reducer";
import type { RunView } from "../types";
import { FlowRail } from "./FlowRail";

function viewOf(events: any[]): RunView {
  let v = initialView();
  for (const e of events) v = reduce(v, e, (e.ts ?? 0) * 1000);
  return v;
}

describe("FlowRail 锚点仪表", () => {
  const v = viewOf([
    { type: "scope", query: "问题", ts: 0, seq: 0 },
    { type: "research_plan", count: 2, initial_tasks: [
      { id: "t1", node_id: "n1", objective: "a", search_query: "a", boundaries: null },
      { id: "t2", node_id: "n1", objective: "b", search_query: "b", boundaries: null },
    ], plan_nodes: [
      { id: "n1", objective: "核验样本", kind: "research", dependency_ids: [] },
      { id: "n2", objective: "综合结论", kind: "decision", dependency_ids: ["n1"] },
    ], ts: 10, seq: 1 },
    { type: "task_batch_dispatched", count: 5, round_index: 0, ts: 12, seq: 2 },
    { type: "subagent_start", sid: "s0", objective: "核验一份很长的研究任务目标", node_id: "n1", ts: 13, seq: 3 },
    { type: "subagent_done", sid: "s0", objective: "核验一份很长的研究任务目标", node_id: "n1",
      tool_calls: 2, evidence_count: 1, status: "ok", ts: 20, seq: 4 },
    { type: "subagent_start", sid: "s1", objective: "查找失败样本", node_id: "n1", ts: 21, seq: 5 },
    { type: "subagent_done", sid: "s1", objective: "查找失败样本", node_id: "n1",
      tool_calls: 1, evidence_count: 0, status: "failed", ts: 22, seq: 6 },
    { type: "nodes_assessed", assessments: [], completed_ids: [], unresolved_node_ids: [], ts: 200, seq: 7 },
    { type: "ready_set_computed", round_index: 1, pending: 1, reason: "补缺", ts: 210, seq: 8 },
    { type: "task_batch_dispatched", round_index: 1, count: 3, objectives: [], ts: 211, seq: 9 },
    { type: "writing", n_evidence: 50, ts: 400, seq: 10 },
    { type: "report_md", markdown: "# t", ts: 500, seq: 11 },
    { type: "done", status: "done", n_evidence: 50, ts: 510, seq: 12 },
  ]);

  it("主要阶段各有一个锚点，后续并发批次不占位", () => {
    const html = renderToStaticMarkup(<FlowRail view={v} nowMs={510_000} />);
    expect(html).toContain("研究问题");
    expect(html).toContain("研究规划");
    expect(html).toContain("首轮研究");
    expect(html).toContain("步骤验收");
    expect(html).toContain("第 2 轮任务准备");
    expect(html).toContain("综合写作");
    expect(html).toContain("研究报告");
    expect(html).not.toContain("并发 3");
  });

  it("按首现顺序列出全部 Worker，并显示步骤号、目标和卡片一致的状态灯", () => {
    const html = renderToStaticMarkup(<FlowRail view={v} nowMs={510_000} />);
    expect(html).toContain('aria-label="步骤 1：核验一份很长的研究任务目标"');
    expect(html).toContain('<span class="num rail-worker-step">#1</span>');
    expect(html.indexOf("核验一份很长的研究任务目标")).toBeLessThan(html.indexOf("查找失败样本"));
    expect(html).toMatch(/lamp lamp-done[^>]*><\/span><span class="num rail-worker-step">#1<\/span><span class="rail-label">核验/);
    expect(html).toMatch(/lamp lamp-failed[^>]*><\/span><span class="num rail-worker-step">#1<\/span><span class="rail-label">查找失败样本/);
  });

  it("锚点带相对时刻,统计块给真实计数", () => {
    const html = renderToStaticMarkup(<FlowRail view={v} nowMs={510_000} />);
    expect(html).toContain("03:20"); // nodes_assessed @200s
    expect(html).toContain('<span>证据</span><b class="num">50</b>');
  });

  it("无 Worker 时闪烁灯只表示运行阶段，结束后不再闪烁", () => {
    const runningView = viewOf([
      { type: "scope", query: "问题", ts: 0, seq: 0 },
      { type: "research_plan", count: 1, initial_tasks: [
        { id: "t1", node_id: "n1", objective: "a", search_query: "a", boundaries: null },
      ], plan_nodes: [], ts: 10, seq: 1 },
      { type: "task_batch_dispatched", count: 1, round_index: 0, ts: 12, seq: 2 },
    ]);
    const live = renderToStaticMarkup(<FlowRail view={runningView} nowMs={20_000} />);
    expect(live.match(/lamp-current/g)?.length).toBe(1);
    const done = renderToStaticMarkup(<FlowRail view={v} nowMs={510_000} />);
    expect(done).not.toContain("lamp-current");
  });
});
