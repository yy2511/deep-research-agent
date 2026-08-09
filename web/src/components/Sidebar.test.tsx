import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RunHistoryItem } from "./Sidebar";
import { runStatusView } from "./runStatusView";

function renderHistoryItem(status: string): string {
  return renderToStaticMarkup(
    <RunHistoryItem
      run={{ run_id: "r1", query: "状态契约", status, event_schema_version: 2, replay_compatible: true }}
      currentRunId={null}
      onSelectLive={() => {}}
      onSelectReplay={() => {}}
    />
  );
}

describe("Sidebar run status", () => {
  it.each([
    ["running", "lamp-current", "运行中"],
    ["done", "lamp-done", "完成"],
    ["partial", "lamp-warning", "部分完成"],
    ["cancelled", "lamp-cancelled", "已取消"],
    ["failed", "lamp-failed", "失败"],
    ["future_status", "lamp-pending", "future_status"],
    ["constructor", "lamp-pending", "constructor"],
    ["toString", "lamp-pending", "toString"],
    ["__proto__", "lamp-pending", "__proto__"],
  ] as const)("status=%s 映射为 %s / %s", (status, lamp, label) => {
    expect(runStatusView(status)).toEqual({ lamp, label });
  });

  it.each([
    ["partial", "lamp-warning", "部分完成"],
    ["constructor", "lamp-pending", "constructor"],
  ] as const)("历史条目实际渲染 status=%s 的 %s", (status, lamp, label) => {
    const html = renderHistoryItem(status);

    expect(html).toContain(`lamp ${lamp}`);
    expect(html).toContain(`title="${label}"`);
    if (status === "constructor") expect(html).not.toContain("lamp-done");
  });

  it("旧协议记录明确标记不可回放且禁用入口", () => {
    const html = renderToStaticMarkup(
      <RunHistoryItem
        run={{ run_id: "old", query: "历史证据", status: "done", replay_compatible: false,
          replay_error: "事件协议版本不兼容" }}
        currentRunId={null}
        onSelectLive={() => {}}
        onSelectReplay={() => {}}
      />
    );

    expect(html).toContain("旧版本、不可回放");
    expect(html).toContain("disabled");
    expect(html).not.toContain("按时序快放");
  });
});
