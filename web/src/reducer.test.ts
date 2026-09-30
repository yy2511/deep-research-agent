import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { initialView, reduce } from "./reducer";

const lines = readFileSync(new URL("../../fixtures/demo_run/events.jsonl", import.meta.url), "utf-8")
  .trim().split("\n").map((l) => JSON.parse(l));

describe("reducer", () => {
  it("seq 重复丢弃（SSE 重连去重兜底）", () => {
    let v = initialView();
    v = reduce(v, lines[0], 0);
    const before = v;
    v = reduce(v, lines[0], 0);
    expect(v).toBe(before);
  });
});

describe("timeline 编年流", () => {

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
});

describe("lead_message 过程消息归并", () => {
  const evt = (over: Record<string, unknown>) => ({
    type: "lead_message", msg_id: "run:1", turn: 1, part_seq: 0,
    mode: "append", text: "我先", complete: false, role: "lead", kind: "note", ...over,
  });
  const withSeq = (e: any) => ({ ...e });

  it("append 增量拼接，complete 只标结束不追加全文", () => {
    let v = reduce(initialView(), withSeq(evt({ part_seq: 0, text: "我先" })), 10);
    v = reduce(v, withSeq(evt({ seq: 1, part_seq: 1, text: "查官方文档。" })), 11);
    v = reduce(v, withSeq(evt({ seq: 2, part_seq: 2, mode: "complete", text: "" })), 12);

    expect(v.leadMessages).toHaveLength(1);
    expect(v.leadMessages?.[0].text).toBe("我先查官方文档。");
    expect(v.leadMessages?.[0].complete).toBe(true);
    expect(v.leadMessages?.[0].gap).toBe(false);   // 两片都到齐，结束标记不产生缺口
  });

  it("replace 用全文覆盖（非流式路径的权威值）", () => {
    let v = reduce(initialView(), withSeq(evt({ text: "半截" })), 10);
    v = reduce(v, withSeq(evt({ seq: 1, mode: "replace", text: "完整正文", complete: true })), 11);

    expect(v.leadMessages?.[0].text).toBe("完整正文");
    expect(v.leadMessages?.[0].complete).toBe(true);
  });

  it("part_seq 出现缺口时标记 gap，不假装拼出了完整正文", () => {
    let v = reduce(initialView(), withSeq(evt({ part_seq: 0, text: "a" })), 10);
    v = reduce(v, withSeq(evt({ seq: 1, part_seq: 3, text: "d" })), 11);

    expect(v.leadMessages?.[0].text).toBe("ad");
    expect(v.leadMessages?.[0].gap).toBe(true);
  });

  it("complete 先到而正文缺失时不建空消息", () => {
    const v = reduce(initialView(), withSeq(evt({ mode: "complete", text: "" })), 10);
    expect(v.leadMessages ?? []).toHaveLength(0);
  });

  it("全局 seq 去重仍然生效：补发的重复事件不重复追加", () => {
    let v = reduce(initialView(), withSeq(evt({ seq: 5, part_seq: 0, text: "a" })), 10);
    v = reduce(v, withSeq(evt({ seq: 5, part_seq: 0, text: "a" })), 11);

    expect(v.leadMessages?.[0].text).toBe("a");
    expect(v.lastSeq).toBe(5);
  });
});
  it("运行期自报路线是全量快照：后一次直接替换前一次", () => {
    const at = 1_700_000_000_000;
    let v = reduce(initialView(), {
      type: "research_status", mode: "simple", phase: "running", seq: 0, ts: 1,
    }, at);
    v = reduce(v, {
      type: "research_plan_updated", seq: 1, ts: 2, note: "开头",
      items: [{ id: "a", text: "第一步", status: "doing" }], diff: null,
    }, at);
    v = reduce(v, {
      type: "research_plan_updated", seq: 2, ts: 3, note: "改道",
      items: [{ id: "b", text: "换了方向", status: "todo" }],
      diff: { added: ["b"], changed: [], removed: ["a"] },
    }, at);
    expect(v.simple?.researchPlan?.items.map((item) => item.id)).toEqual(["b"]);
    expect(v.simple?.researchPlan?.note).toBe("改道");
    expect(v.simple?.researchPlan?.diff?.removed).toEqual(["a"]);
  });
