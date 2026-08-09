import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { initialView } from "../reducer";
import { StatsView } from "./ResultPane";

describe("StatsView", () => {
  it("用自然语言展示真实耗时口径和调用明细", () => {
    const view = initialView();
    const html = renderToStaticMarkup(
      <StatsView
        view={view}
        nowMs={0}
        metaStats={{
          wall_s: 15,
          agg_s: 164,
          llm: [{ step: "build_research_plan", model: "m", calls: 1,
            total_ms: 1000, in_tok: 20, out_tok: 10 }],
          steps: [
            { step: "dispatch_task_batch(并发)", calls: 1, total_ms: 2100 },
            { step: "dispatch_task_batch(Round 1)", calls: 1, total_ms: 1800 },
            { step: "dispatch_task_batch(Final Research Pass, Round 2)", calls: 1, total_ms: 900 },
            { step: "dedup", calls: 1, total_ms: 2 },
          ],
          tools: [{ tool: "save_evidence", calls: 2, ok: 2, accepted: 3, rejected: 1 }],
        }}
      />
    );

    expect(html).toContain("总耗时");
    expect(html).toContain("累计步骤耗时");
    expect(html).toContain("生成研究计划");
    expect(html).toContain("并发执行首轮研究");
    expect(html).toContain("并发执行第 2 轮研究");
    expect(html).toContain("并发执行最终补查");
    expect(html).toContain("证据去重");
    expect(html).toContain("收录证据");
    expect(html).not.toContain("并发度");
    expect(html).not.toContain("节点 × 模型");
    expect(html).not.toContain("dispatch_task_batch");
  });
});
