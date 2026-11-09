import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { FeedbackLinks } from "./components/FeedbackLinks";
import { feedbackUrl, REPO_URL } from "./feedback";

/** 意见反馈（#55）：打开仓库讨论区的新帖页，预填问题与模板。 */

function params(url: string) {
  const u = new URL(url);
  return { base: `${u.origin}${u.pathname}`, q: u.searchParams };
}

describe("讨论区链接", () => {
  it("带问题时预填标题、问题和研究时间，不放研究链接", () => {
    const { base, q } = params(feedbackUrl({ query: "Redis 的 AOF 和 RDB，哪个更适合对丢数据敏感的场景？", finishedAt: 1_794_200_000 }));
    expect(base).toBe(`${REPO_URL}/discussions/new`);
    expect(q.get("category")).toBe("general");
    expect(q.get("title")).toBe("反馈：Redis 的 AOF 和 RDB，哪个更适合对丢数据敏感的场景？");
    const body = q.get("body")!;
    expect(body).toContain("**问的问题**：Redis 的 AOF 和 RDB");
    expect(body).toMatch(/\*\*研究时间\*\*：\d{4}-\d{2}-\d{2}/);
    expect(body).toContain("**哪里不对 / 不好用**");
    expect(body).not.toContain("/api/runs");
  });

  it("长问题截短到 40 个字；特殊字符正确编码", () => {
    const long = "为什么".repeat(30) + " & ? # =";
    const url = feedbackUrl({ query: long });
    expect(params(url).q.get("title")).toBe(`反馈：${Array.from(long).slice(0, 40).join("")}…`);
    expect(url).not.toContain(" ");
    expect(url).not.toContain("+");  // 空格编码为 %20
    expect(params(url).q.get("body")).toContain("& ? # =");
  });

  it("侧边栏的一般反馈只有模板", () => {
    const { q } = params(feedbackUrl());
    expect(q.get("title")).toBe("反馈：");
    expect(q.get("body")).not.toContain("问的问题");
  });

  it("链接在新标签页打开，旁边是源码 / star", () => {
    const html = renderToStaticMarkup(<FeedbackLinks query="问题" />);
    expect(html).toContain("提意见");
    expect(html).toContain(`href="${REPO_URL}"`);
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noreferrer"');
    expect(renderToStaticMarkup(<FeedbackLinks compact />)).toContain(">源码 · star</a>");
  });
});

describe("报告底部", () => {
  it("有回答的研究结束后显示“提意见”，进行中不显示", async () => {
    const { ConversationWorkspace } = await import("./components/ConversationWorkspace");
    const { initialView, mergeSimpleResult } = await import("./reducer");
    const done = { ...mergeSimpleResult(initialView(), { draft: "正文" }, null), startedAtMs: 1, endedAtMs: 2 };
    expect(renderToStaticMarkup(<ConversationWorkspace view={done} runId="r1" />)).toContain("提意见");
    const running = { ...mergeSimpleResult(initialView(), { draft: "正文" }, null), startedAtMs: 1 };
    expect(renderToStaticMarkup(<ConversationWorkspace view={running} runId="r1" />)).not.toContain("提意见");
  });
});
