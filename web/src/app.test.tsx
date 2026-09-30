import { beforeEach, describe, expect, it } from "vitest";
import {
  apiStub, installBrowserStubs, jsxRuntimeStub, loadAppHandlers, reactStub,
} from "./testHarness";

/**
 * App 导航状态机：批次隔离、迟到响应、结果合并的真实 async 序列。
 *
 * 重点覆盖两条容易出错的序列：A→B→A 后旧 A 的 SSE 回调，以及
 * "A 有过程 → 切到没有 events 的旧 B"。
 */

let handlers: Record<string, any>;
let appFactory: () => Record<string, any>;

beforeEach(() => {
  installBrowserStubs();
  (globalThis as any).__state = [];           // useState 单元格容器（夹具读取视图用）
  (globalThis as any).__routes = new Map<string, any>();
  (globalThis as any).__streams = [];
  (globalThis as any).__replays = [];
  (globalThis as any).__posts = new Map<string, any>();
  (globalThis as any).__reactStub = reactStub();
  (globalThis as any).__apiStub = apiStub();
  (globalThis as any).__jsxRuntime = jsxRuntimeStub();
  appFactory = loadAppHandlers();
  handlers = appFactory() as Record<string, any>;
});

const SIMPLE = (over: Record<string, unknown> = {}) => ({
  query: "问题", draft: "候选正文", markdown: "# 答案", cards: [], sources: [], ...over,
});

function simpleRun(id: string, query: string, markdown: string) {
  return {
    [`/api/runs/${id}/simple`]: SIMPLE({ query, markdown }),
    [`/api/runs/${id}`]: { meta: { status: "done", mode: "simple", replay_compatible: true } },
  };
}

function view() {
  const cells = (globalThis as any).__state as { value: any }[];
  return cells.find((c) => c?.value && Array.isArray(c.value.timeline))?.value;
}

async function flush() {
  for (let i = 0; i < 12; i++) await Promise.resolve();
}

function routes(entries: Record<string, any>) {
  const map = (globalThis as any).__routes as Map<string, any>;
  for (const [key, value] of Object.entries(entries)) map.set(key, value);
}

describe("App 入口与视图选择", () => {
  it("新研究清掉 URL 与保存的运行，旧 SSE 回调不得再写入", async () => {
    routes(simpleRun("run-A", "A 的问题", "# A 的报告"));
    handlers.connectToRun("run-A");
    const stale = ((globalThis as any).__streams as any[]).at(-1);
    handlers.startNewResearch();
    stale.onEvent({
      type: "lead_message", run_id: "run-A", seq: 1, ts: 1, msg_id: "run-A:1",
      turn: 1, part_seq: 0, mode: "append", text: "新研究之后的旧文字", complete: false,
    });
    expect(JSON.stringify(view())).not.toContain("新研究之后的旧文字");
    expect((globalThis as any).__memory.get("dra_active_run_id")).toBeUndefined();
  });
});

describe("App 导航批次隔离", () => {
  it.each(["instant", "paced"] as const)("%s 历史回放结束后只取一次结果，并复用已读取的运行详情", async (mode) => {
    let detailReads = 0;
    let resultReads = 0;
    (globalThis as any).__timers = [];
    routes({
      "/api/runs/history": () => {
        detailReads += 1;
        return { meta: { status: "done", replay_compatible: true }, executed_tool_calls: 7 };
      },
      "/api/runs/history/simple": () => {
        resultReads += 1;
        return SIMPLE({ markdown: "# 历史最终答案" });
      },
      "/api/runs/history/events?compact=true": [
        { type: "lead_message", run_id: "history", seq: 0, ts: 1, msg_id: "history:1",
          turn: 1, part_seq: 0, mode: "replace", text: "先展示研究过程", complete: true },
        { type: "end", run_id: "history", seq: 1, ts: 2 },
      ],
    });

    await handlers.viewHistoricalRun("history", mode);
    if (mode === "paced") {
      expect(resultReads).toBe(0);
      expect(JSON.stringify(view())).not.toContain("历史最终答案");
      for (const timer of (globalThis as any).__timers) timer();
    }
    await flush();
    expect(resultReads).toBe(1);
    expect(detailReads).toBe(1);
    expect(JSON.stringify(view())).toContain("先展示研究过程");
    expect(JSON.stringify(view())).toContain("历史最终答案");
  });

  it("A→B→A：第一次 A 的旧 SSE 闭包不能再写进第二次 A 的视图", async () => {
    routes({
      ...simpleRun("run-A", "A 的问题", "# A 的报告"),
      ...simpleRun("run-B", "B 的问题", "# B 的报告"),
    });
    const streams = (globalThis as any).__streams as any[];

    handlers.connectToRun("run-A");
    const staleA = streams.at(-1);
    handlers.connectToRun("run-B");
    handlers.connectToRun("run-A");
    await flush();

    // 旧 A 的闭包带着"第一次选择"的批次：即使 run id 又变回 A，也必须被丢弃。
    staleA.onEvent({
      type: "lead_message", run_id: "run-A", seq: 1, ts: 1, msg_id: "run-A:1",
      turn: 1, part_seq: 0, mode: "append", text: "第一次 A 的陈旧文字", complete: false,
    });
    expect(JSON.stringify(view())).not.toContain("第一次 A 的陈旧文字");
    // 当前 A 的流仍然可用
    streams.at(-1).onEvent({
      type: "lead_message", run_id: "run-A", seq: 1, ts: 1, msg_id: "run-A:1",
      turn: 1, part_seq: 0, mode: "append", text: "第二次 A 的真实文字", complete: false,
    });
    expect(JSON.stringify(view())).toContain("第二次 A 的真实文字");
  });

  it("有过程的 A 切到没有 events 的旧 B：只显示 B 的结果，不并进 A 的过程", async () => {
    routes({
      ...simpleRun("run-A", "A 的问题", "# A 的报告"),
      "/api/runs/run-A/events?compact=true": [
        { type: "lead_message", run_id: "run-A", seq: 0, ts: 1, msg_id: "run-A:1",
          turn: 1, part_seq: 0, mode: "replace", text: "A 的研究过程说明", complete: true },
        { type: "end", run_id: "run-A", seq: 1, ts: 2 },
      ],
      // 旧 B：没有 events.jsonl（不可回放），只有结果
      "/api/runs/run-B/simple": SIMPLE({ query: "B 的问题", markdown: "# B 的报告" }),
      "/api/runs/run-B": { meta: { status: "done", mode: "simple", replay_compatible: false } },
      "/api/runs/run-B/events?compact=true": new Error("404 没有事件记录"),
    });

    await handlers.viewHistoricalRun("run-A", "instant");
    await flush();
    expect(JSON.stringify(view())).toContain("A 的研究过程说明");

    await handlers.viewHistoricalRun("run-B", "instant");
    await flush();
    const after = view();
    expect(after.simple?.mode).toBe("simple");
    expect(after.simple?.query).toBe("B 的问题");
    expect(JSON.stringify(after)).toContain("B 的报告");
    // A 的过程不得残留（切换那一刻就该换基底）
    expect(JSON.stringify(after)).not.toContain("A 的研究过程说明");
    expect(after.processMissing).toBeTruthy();
  });

  it("迟到结果不覆盖当前选择（同 id 重复选择也按批次隔离）", async () => {
    let release: (value: any) => void = () => {};
    const pending = new Promise((resolve) => { release = resolve; });
    routes({
      "/api/runs/run-A/simple": () => pending,
      "/api/runs/run-A": { meta: { status: "done", mode: "simple" } },
      ...simpleRun("run-B", "B 的问题", "# B 的报告"),
    });
    const inFlight = handlers.applySimpleResult("run-A", { live: true });
    handlers.connectToRun("run-B");
    release(SIMPLE({ query: "A 的旧答案", markdown: "# A 的旧报告" }));
    await inFlight;
    await flush();
    expect(view()?.simple?.query).not.toBe("A 的旧答案");
  });

  it("结束事件后的结果加载保留已显示的过程", async () => {
    routes(simpleRun("run-A", "A 的问题", "# A 的最终答案"));
    handlers.connectToRun("run-A");
    const stream = ((globalThis as any).__streams as any[]).at(-1);
    stream.onEvent({
      type: "lead_message", run_id: "run-A", seq: 0, ts: 1, msg_id: "run-A:1",
      turn: 1, part_seq: 0, mode: "replace", text: "公开的过程说明", complete: true,
    });
    stream.onEvent({ type: "end", run_id: "run-A", seq: 1, ts: 2 });
    await flush();
    const after = view();
    expect(JSON.stringify(after)).toContain("公开的过程说明");
    expect(JSON.stringify(after)).toContain("A 的最终答案");
  });
});
