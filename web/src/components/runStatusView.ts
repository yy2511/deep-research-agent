const STATUS_VIEW = new Map<string, { lamp: string; label: string }>([
  ["running", { lamp: "lamp-current", label: "运行中" }],
  ["done", { lamp: "lamp-done", label: "完成" }],
  ["partial", { lamp: "lamp-warning", label: "部分完成" }],
  ["cancelled", { lamp: "lamp-cancelled", label: "已取消" }],
  ["failed", { lamp: "lamp-failed", label: "失败" }],
]);

export function runStatusView(status: string): { lamp: string; label: string } {
  return STATUS_VIEW.get(status) ?? { lamp: "lamp-pending", label: status || "未知状态" };
}
