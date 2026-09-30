import type { RunView } from "../types";

const STATUS_VIEW = new Map<string, { lamp: string; label: string }>([
  ["running", { lamp: "lamp-current", label: "研究中" }],
  ["interrupted", { lamp: "lamp-warning", label: "中断" }],
  ["done", { lamp: "lamp-done", label: "已完成" }],
  ["partial", { lamp: "lamp-warning", label: "部分完成" }],
  ["cancelled", { lamp: "lamp-cancelled", label: "已停止" }],
  ["failed", { lamp: "lamp-failed", label: "失败" }],
]);

export function runStatusView(status: string): { lamp: string; label: string } {
  return STATUS_VIEW.get(status) ?? { lamp: "lamp-pending", label: status || "未知状态" };
}

export type OutcomeTone = "live" | "ok" | "warn" | "stopped" | "bad" | "neutral";

/**
 * 一次研究现在是什么结果，用一个词说清。
 * 顶栏状态和正文上方的状态行都用它，保证两处说法一致。
 */
export function runOutcome(view: RunView): { tone: OutcomeTone; label: string } {
  if (view.endedAtMs === undefined) return { tone: "live", label: "研究中" };
  const simple = view.simple;
  if (simple?.stopReason === "cancelled" || view.banner?.kind === "cancelled") return { tone: "stopped", label: "已停止" };
  if (simple?.draftRejected) return { tone: "warn", label: "回答未通过引用检查" };
  if (simple?.delivery === "delivered") return { tone: "ok", label: "已完成" };
  if (simple?.delivery === "limited") return { tone: "warn", label: "部分完成" };
  if (simple?.delivery === "failed") return { tone: "bad", label: "未完成" };
  switch (view.banner?.kind) {
    case "done": return { tone: "ok", label: "已完成" };
    case "partial": return { tone: "warn", label: "部分完成" };
    case "error": return { tone: "bad", label: "出错了" };
    default: return { tone: "neutral", label: "已结束" };
  }
}
