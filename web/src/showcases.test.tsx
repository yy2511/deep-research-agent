import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ShowcaseList } from "./components/Sidebar";
import { initialView, mergeSimpleResult } from "./reducer";
import { fetchShowcases, showcaseEntry, showcaseOf, type Showcase } from "./showcases";

/** 示例（#47）：所有人只读可见。 */

const sample: Showcase = {
  conversation_id: "c1", title: "缓存怎么选", query: "Redis 的 AOF 和 RDB 怎么选？",
  run_ids: ["c1", "r2"], latest_run_id: "r2", turn_count: 2, finished_at: 1_700_000_000, available: true,
};

afterEach(() => { vi.unstubAllGlobals(); });

describe("示例数据", () => {
  it("按研究编号找到所属示例；打开时看最新一轮，不可用的不打开", () => {
    expect(showcaseOf([sample], "r2")?.title).toBe("缓存怎么选");
    expect(showcaseOf([sample], "other")).toBeNull();
    expect(showcaseOf([sample], null)).toBeNull();
    expect(showcaseEntry(sample)).toBe("r2");
    expect(showcaseEntry({ ...sample, available: false })).toBeNull();
  });

  it("列表读不到时当作没有示例", async () => {
    vi.stubGlobal("fetch", async () => new Response("{}", { status: 503 }));
    await expect(fetchShowcases()).resolves.toEqual([]);
    vi.stubGlobal("fetch", async () => { throw new TypeError("network down"); });
    await expect(fetchShowcases()).resolves.toEqual([]);
    vi.stubGlobal("fetch", async () => new Response(JSON.stringify([sample]), { status: 200 }));
    await expect(fetchShowcases()).resolves.toEqual([sample]);
  });
});

describe("侧边栏示例分组", () => {
  it("标明只读，显示标题和轮数；没有示例时不显示分组", () => {
    const html = renderToStaticMarkup(<ShowcaseList showcases={[sample]} currentRunId="r2" onSelect={() => {}} />);
    for (const text of ["示例 · 只读", "缓存怎么选", "2 轮", "history-item-active"]) expect(html).toContain(text);
    expect(renderToStaticMarkup(<ShowcaseList showcases={[]} currentRunId={null} onSelect={() => {}} />)).toBe("");
  });

  it("文件缺失的示例显示不可用且不能点", () => {
    const html = renderToStaticMarkup(<ShowcaseList showcases={[{ ...sample, available: false }]} currentRunId={null} onSelect={() => {}} />);
    expect(html).toContain("该示例已不可用");
    expect(html).toContain("disabled");
  });
});

describe("只读的研究页", () => {
  it("示例不显示追问输入框，改为说明和登录入口", async () => {
    const { ConversationWorkspace } = await import("./components/ConversationWorkspace");
    const view = { ...mergeSimpleResult(initialView(), { draft: "正文" }, null), startedAtMs: 1, endedAtMs: 2 };
    const normal = renderToStaticMarkup(<ConversationWorkspace view={view} runId="r2" />);
    expect(normal).toContain("research-message");
    const readOnly = renderToStaticMarkup(<ConversationWorkspace view={view} runId="r2" readOnly
      readOnlyAction={<button>用 GitHub 登录，问你自己的问题</button>} />);
    expect(readOnly).not.toContain("research-message");
    expect(readOnly).toContain("这是公开示例，只能查看");
    expect(readOnly).toContain("用 GitHub 登录，问你自己的问题");
  });
});
