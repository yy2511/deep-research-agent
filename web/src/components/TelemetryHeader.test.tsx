import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { initialView } from "../reducer";
import type { RunView } from "../types";
import { TelemetryHeader } from "./TelemetryHeader";

function renderHeader(view: RunView): string {
  return renderToStaticMarkup(
    <TelemetryHeader
      view={view}
      nowMs={5000}
      mode="clean"
      onMode={() => {}}
      onCancel={() => {}}
      canCancel={false}
      config={null}
      onConfigChange={() => {}}
    />
  );
}

describe("TelemetryHeader lifecycle", () => {
  it.each([
    ["done", "done", "done", "完成"],
    ["partial", "done", "warning", "完成"],
    ["error", "research", "failed", "研究执行"],
    ["cancelled", "write", "cancelled", "写作"],
    ["unknown", "done", "pending", "完成"],
  ] as const)("终态 kind=%s 停在 %s 时不再显示 current/pulse", (kind, stage, visualState, stageLabel) => {
    const view = initialView();
    view.stage = stage;
    view.startedAtMs = 1000;
    view.endedAtMs = 4000;
    view.banner = {
      kind,
      text: "终态",
    };

    const html = renderHeader(view);

    expect(html).not.toContain("lamp-current");
    expect(html).not.toContain("stage-current");
    expect(html).not.toContain("stage-seg-active");
    expect(html).toContain(
      `<span class="stage stage-${visualState}"><span class="lamp lamp-${visualState}"></span>${stageLabel}</span>`
    );
  });

  it("运行中仍保留 current 灯和 active 进度段", () => {
    const view = initialView();
    view.stage = "research";
    view.startedAtMs = 1000;

    const html = renderHeader(view);

    expect(html).toContain("stage-current");
    expect(html).toContain("lamp-current");
    expect(html).toContain("stage-seg-active");
  });

  it("reflect 阶段在顶栏显示为报告规划", () => {
    const view = initialView();
    view.stage = "reflect";
    view.startedAtMs = 1000;

    const html = renderHeader(view);

    expect(html).toContain("报告规划");
    expect(html).not.toContain(">反思<");
  });
});
