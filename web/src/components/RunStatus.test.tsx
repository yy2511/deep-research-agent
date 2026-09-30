import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { initialView } from "../reducer";
import type { RunView } from "../types";
import { RunStatus } from "./RunStatus";

function render(view: RunView, extra: { canCancel?: boolean; cancelling?: boolean } = {}): string {
  return renderToStaticMarkup(
    <RunStatus view={view} nowMs={73000} onCancel={() => {}} canCancel={extra.canCancel ?? false} cancelling={extra.cancelling} />
  );
}

describe("研究状态", () => {
  it("还没开始时什么都不显示", () => {
    expect(render(initialView())).toBe("");
  });

  it("进行中显示用时，可以停止", () => {
    const view = { ...initialView(), startedAtMs: 1000 };
    const html = render(view, { canCancel: true });
    expect(html).toContain("run-state-live");
    expect(html).toContain("研究中 · 1 分 12 秒");
    expect(html).toContain("停止");
  });

  it("已请求停止时显示停止中并禁用按钮，避免重复点击", () => {
    const view = { ...initialView(), startedAtMs: 1000 };
    const html = render(view, { canCancel: true, cancelling: true });
    expect(html).toContain("停止中…");
    expect(html).toContain("disabled");
  });

  it.each([
    ["done", "ok", "已完成"],
    ["partial", "warn", "部分完成"],
    ["error", "bad", "出错了"],
    ["cancelled", "stopped", "已停止"],
    ["unknown", "neutral", "已结束"],
  ] as const)("结束后 banner=%s 显示 %s / %s，不再显示进行中", (kind, tone, label) => {
    const view: RunView = { ...initialView(), startedAtMs: 1000, endedAtMs: 4000, banner: { kind, text: "终态" } };
    const html = render(view);
    expect(html).toContain(`run-state-${tone}`);
    expect(html).toContain(label);
    expect(html).not.toContain("run-state-live");
    expect(html).not.toContain("停止</button>");
  });

  it("交付状态优先于 banner：受限交付说成部分完成", () => {
    const view: RunView = { ...initialView(), startedAtMs: 1, endedAtMs: 2, banner: { kind: "done", text: "" },
      simple: { mode: "simple", phase: "finished", delivery: "limited" } };
    expect(render(view)).toContain("部分完成");
  });
});
