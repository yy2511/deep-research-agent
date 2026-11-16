/** 管理员运营统计：仅保存当前页面的汇总快照，不持久化、不读取研究详情。 */
import { notifyUnauthorized } from "./session";

export type OperationsCounts = {
  new_users: number; research_users: number; research_tasks: number; conversations: number;
};
export type OperationsSnapshot = {
  as_of: string;
  timezone: "UTC";
  scope: "registered_database_records";
  range: { start_date: string; end_date: string };
  totals: {
    users: number; enabled_users: number; disabled_users: number; showcases: number;
    roles: { guest: number; user: number; admin: number; owner?: number };
  };
  period: OperationsCounts;
  daily: Array<OperationsCounts & { date: string }>;
};
export type OperationsPreset = "today" | "7" | "30" | "all" | "custom";
export type OperationsState = {
  data: OperationsSnapshot | null;
  key: string | null;
  loading: boolean;
  error: string | null;
};
export const INITIAL_OPERATIONS: OperationsState = { data: null, key: null, loading: true, error: null };

export function utcDate(now = new Date()): string {
  return now.toISOString().slice(0, 10);
}

export function presetDates(days: number, now = new Date()): { start: string; end: string } {
  const start = new Date(now);
  start.setUTCDate(start.getUTCDate() - days + 1);
  return { start: utcDate(start), end: utcDate(now) };
}

export function operationsQuery(preset: OperationsPreset, start = "", end = "", now = new Date()): string {
  if (preset === "all") return "all_time=true";
  // 默认范围由服务端决定，避免首次打开时依赖客户端时钟。
  if (preset === "7") return "";
  const dates = preset === "custom" ? { start, end } : presetDates(preset === "today" ? 1 : 30, now);
  return new URLSearchParams({ start_date: dates.start, end_date: dates.end }).toString();
}

export function customRangeError(start: string, end: string, now = new Date()): string | null {
  const valid = (value: string) => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000-")) return false;
    const date = new Date(`${value}T00:00:00Z`);
    return Number.isFinite(date.getTime()) && utcDate(date) === value;
  };
  if (!valid(start) || !valid(end)) return "请填写有效的开始和结束日期。";
  if (start > end) return "开始日期不能晚于结束日期。";
  if (end > utcDate(now)) return "结束日期不能晚于今日（UTC）。";
  return null;
}

class OperationsError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

export async function fetchOperations(query: string, signal: AbortSignal): Promise<OperationsSnapshot> {
  const response = await fetch(`/api/admin/operations${query ? `?${query}` : ""}`, {
    signal, cache: "no-store", credentials: "same-origin",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new OperationsError(typeof body.detail === "string" ? body.detail : "运营统计暂不可用，请稍后重试", response.status);
  }
  return response.json();
}

/** 将请求序号和 AbortController 放在一起：A→B→A 也不能接收第一次 A 的晚到结果。 */
export class OperationsLoader {
  private generation = 0;
  private controller: AbortController | null = null;
  private pendingKey: string | null = null;
  private state: OperationsState = INITIAL_OPERATIONS;
  private disposed = false;
  private readonly changed: (state: OperationsState) => void;

  constructor(changed: (state: OperationsState) => void) { this.changed = changed; }

  private publish(state: OperationsState) { this.state = state; this.changed(state); }

  async load(key: string): Promise<void> {
    if (this.disposed || (this.controller && this.pendingKey === key)) return;
    this.controller?.abort();
    const sequence = ++this.generation;
    const controller = new AbortController();
    this.controller = controller;
    this.pendingKey = key;
    this.publish({ data: this.state.key === key ? this.state.data : null, key, loading: true, error: null });
    try {
      const data = await fetchOperations(key, controller.signal);
      if (this.disposed || sequence !== this.generation) return;
      this.publish({ data, key, loading: false, error: null });
    } catch (error) {
      if (this.disposed || sequence !== this.generation) return;
      const denied = error instanceof OperationsError && (error.status === 401 || error.status === 403);
      this.publish({ data: denied ? null : this.state.data, key, loading: false,
        error: error instanceof OperationsError ? error.message : "网络连接失败，请重试。" });
      // 先清空统计，再通知全局登录页，避免登录失效期间保留已加载的快照。
      if (error instanceof OperationsError && error.status === 401) notifyUnauthorized();
    } finally {
      if (sequence === this.generation) { this.controller = null; this.pendingKey = null; }
    }
  }

  dispose() {
    this.disposed = true;
    ++this.generation;
    this.controller?.abort();
    this.controller = null;
    this.pendingKey = null;
    this.state = { ...INITIAL_OPERATIONS, loading: false };
  }
}
