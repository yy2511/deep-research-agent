import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { LlmCallsList, llmCallsFromPlanTrace } from "./LlmCalls";

describe("LlmCallsList", () => {
  it("规划空输出默认展开，并展示对应 json_retry 原因", () => {
    const calls = llmCallsFromPlanTrace({
      events: [
        {
          type: "llm_call",
          step: "build_research_plan",
          model: "glm-5.2",
          ms: 19298,
          in_tok: 1421,
          out_tok: 632,
          input: "planner prompt",
          output: "",
          io_complete: true,
        },
        {
          type: "json_retry",
          reason: "empty",
          attempt: 1,
          raw_head: "",
        },
      ],
    });
    const html = renderToStaticMarkup(<LlmCallsList calls={calls} />);

    expect(html).toContain('<details class="llm-call" open="">');
    expect(html).toContain("空输出");
    expect(html).toContain("计入 632 个输出 token");
    expect(html).toContain("后端未收到可见正文");
    expect(html).toContain("错误原因");
    expect(html).toContain("模型返回空内容，无法解析结构化输出");
    expect(html).toContain("系统将重试");
  });

  it("最终一次失败标明重试已经耗尽", () => {
    const calls = llmCallsFromPlanTrace({
      events: [
        { type: "llm_call", step: "build_research_plan", ms: 100, output: "", io_complete: true },
        { type: "json_retry", reason: "empty", attempt: 2, exhausted: true, raw_head: "" },
      ],
    });
    const html = renderToStaticMarkup(<LlmCallsList calls={calls} />);

    expect(html).toContain("第 2 次尝试，重试已耗尽");
  });
});
