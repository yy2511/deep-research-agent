import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { PlanCard, type ResearchPlan } from "./PlanCard";

function render(plan: ResearchPlan, mode: "debug" | "clean" = "clean") {
  return renderToStaticMarkup(
    <PlanCard plan={plan} busy={false} hint={null} error={null} mode={mode}
      onStart={() => {}} onRevise={async () => {}} onCancel={() => {}} />
  );
}

describe("PlanCard", () => {
  it("展示唯一 typed plan 的节点、初始任务和运行预算", () => {
    const html = render({
      clarified_query: "先建立判据，再选择并比较项目",
      plan_nodes: [
        {
          id: "criteria",
          objective: "建立评价判据",
          kind: "research",
          dependency_ids: [],
          acceptance_criteria: "列出有来源的指标和候选范围",
        },
        {
          id: "select",
          objective: "选择两个代表项目",
          kind: "decision",
          dependency_ids: ["criteria"],
          acceptance_criteria: "给出两个项目、选择理由和证据引用",
        },
      ],
      initial_tasks: [{
        id: "task-criteria",
        node_id: "criteria",
        objective: "收集评价指标",
        search_query: "vector database evaluation criteria",
        boundaries: null,
      }],
      budget: {
        max_research_rounds: 2,
        max_tasks_per_round: 4,
        max_total_tasks: 8,
      },
    });

    expect(html).toContain("研究步骤");
    expect(html).toContain("研究");
    expect(html).toContain("决策");
    expect(html).toContain("前置步骤：步骤 1 · 建立评价判据");
    expect(html).toContain("验收标准：给出两个项目、选择理由和证据引用");
    expect(html).toContain("最多 2 轮研究");
    expect(html).toContain("每轮最多 4 项任务");
    expect(html).toContain("任务总数上限 8");
    expect(html).toContain("关联步骤：步骤 1 · 建立评价判据");
    expect(html).not.toContain("plan-node-id");
    expect(html).not.toContain("后续动态推进");
  });

  it("调试档展示规划 llm_call 输入输出与落盘 id", () => {
    const plan: ResearchPlan = {
      clarified_query: "q",
      plan_nodes: [{
        id: "m1", objective: "o", kind: "research",
        dependency_ids: [], acceptance_criteria: "c",
      }],
      initial_tasks: [{
        id: "t1", node_id: "m1", objective: "o",
        search_query: "s", boundaries: null,
      }],
      trace: {
        plan_id: "20260724-plan-abc",
        events: [{
          type: "llm_call",
          step: "build_research_plan",
          model: "deepseek-v4-pro",
          ms: 1234,
          in_tok: 100,
          out_tok: 200,
          input: "【system】规划助手",
          output: '{"plan_nodes":[{"id":"m1"}]}',
          io_complete: true,
        }],
      },
    };
    const debugHtml = render(plan, "debug");
    expect(debugHtml).toContain("规划器调用（调试）");
    expect(debugHtml).toContain("plans/20260724-plan-abc");
    expect(debugHtml).toContain("生成研究计划");
    expect(debugHtml).toContain("【system】规划助手");
    // SSR 会把 " 转成 &quot;
    expect(debugHtml).toContain("plan_nodes");
    expect(debugHtml).toContain('<details class="llm-call">');
    expect(debugHtml).toContain("完整 I/O");

    const cleanHtml = render(plan, "clean");
    expect(cleanHtml).not.toContain("规划器调用（调试）");
    expect(cleanHtml).not.toContain("【system】规划助手");
  });
});
