import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RunHistoryItem } from "./Sidebar";
import { relativeTime } from "../format";
import { runStatusView } from "./runStatusView";

function renderHistoryItem(status: string, showReplay = false): string {
  return renderToStaticMarkup(
    <RunHistoryItem
      run={{ run_id: "r1", query: "状态契约", status, event_schema_version: 2, replay_compatible: true }}
      currentRunId={null}
      onSelectLive={() => {}}
      onSelectReplay={() => {}}
      showReplay={showReplay}
    />
  );
}

describe("Sidebar run status", () => {
  it.each([
    ["running", "lamp-current", "研究中"],
    ["done", "lamp-done", "已完成"],
    ["partial", "lamp-warning", "部分完成"],
    ["cancelled", "lamp-cancelled", "已停止"],
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

  it("已完成的记录不加状态标记，只显示时间", () => {
    const html = renderHistoryItem("done");
    expect(html).not.toContain("lamp");
    expect(html).not.toContain("已完成");
  });

  it("快放按钮只在开发者选项打开时出现", () => {
    expect(renderHistoryItem("done")).not.toContain("快放研究过程");
    expect(renderHistoryItem("done", true)).toContain("快放研究过程");
  });

  it("旧协议允许查看结果但不回放事件", () => {
    const html = renderToStaticMarkup(
      <RunHistoryItem
        run={{ run_id: "old", query: "历史证据", status: "done", replay_compatible: false,
          replay_error: "事件协议版本不兼容" }}
        currentRunId={null}
        onSelectLive={() => {}}
        onSelectReplay={() => {}}
        showReplay
      />
    );

    expect(html).toContain("不能回放过程");
    expect(html).not.toContain("disabled");
    expect(html).not.toContain("快放研究过程");
  });

  it("时间按今天 / 昨天 / 日期显示", () => {
    const now = new Date(2026, 8, 28, 15, 0);
    const at = (d: Date) => d.getTime() / 1000;
    expect(relativeTime(at(new Date(2026, 8, 28, 14, 2)), now)).toBe("今天 14:02");
    expect(relativeTime(at(new Date(2026, 8, 27, 9, 0)), now)).toBe("昨天");
    expect(relativeTime(at(new Date(2026, 8, 25, 9, 0)), now)).toBe("9月25日");
    expect(relativeTime(at(new Date(2025, 0, 3, 9, 0)), now)).toBe("2025年1月3日");
  });
});
