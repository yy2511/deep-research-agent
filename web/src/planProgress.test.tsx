import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { PlanningProgress, ThinkingReview } from "./components/PlanningProgress";
import { applyPlannerEvent, startProgress, thinkingText } from "./planProgress";

describe("规划进度", () => {
  it("按推送进入步骤、累积各步思考，结束后拼成可回看的文字", () => {
    let p = startProgress(["gaps", "plan"], 1000);
    p = applyPlannerEvent(p, { type: "stage", stage: "gaps" }, 2000);
    p = applyPlannerEvent(p, { type: "reasoning", stage: "gaps", text: "先读题，" });
    p = applyPlannerEvent(p, { type: "reasoning", stage: "gaps", text: "条件都写全了" });
    p = applyPlannerEvent(p, { type: "stage", stage: "plan" }, 5000);
    expect(p.stage).toBe("plan");
    expect(p.stageStartedAt).toBe(5000);
    expect(p.thinking.gaps).toBe("先读题，条件都写全了");
    expect(thinkingText(p)).toBe("【判断要不要先问你几个问题】\n先读题，条件都写全了");
    expect(thinkingText(startProgress(["plan"]))).toBe("");
  });

  it("进度框显示步骤状态与当前步的思考；没有思考内容时说明原因", () => {
    let p = applyPlannerEvent(startProgress(["gaps", "plan"]), { type: "stage", stage: "plan" });
    p = applyPlannerEvent(p, { type: "reasoning", stage: "plan", text: "路线分三步" });
    const html = renderToStaticMarkup(<PlanningProgress progress={p} />);
    for (const text of ["判断要不要先问你几个问题", "生成研究计划", "is-done", "is-active", "路线分三步", "收起"]) {
      expect(html).toContain(text);
    }
    const waiting = renderToStaticMarkup(<PlanningProgress progress={applyPlannerEvent(startProgress(["plan"]), { type: "stage", stage: "plan" })} />);
    expect(waiting).toContain("不公开思考过程");
    const quick = renderToStaticMarkup(<PlanningProgress progress={applyPlannerEvent(startProgress(["gaps", "plan"]), { type: "stage", stage: "gaps" })} />);
    expect(quick).toContain("通常几秒钟");
  });

  it("回看框只在有思考内容时出现", () => {
    expect(renderToStaticMarkup(<ThinkingReview text="" />)).toBe("");
    expect(renderToStaticMarkup(<ThinkingReview text="想了想" />)).toContain("查看规划时的思考（3 字）");
  });
});
