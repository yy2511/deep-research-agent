import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OperationsResults, OperationsStatus } from "./components/OperationsDashboard";
import { customRangeError, INITIAL_OPERATIONS, OperationsLoader, operationsQuery, presetDates, utcDate,
  type OperationsSnapshot, type OperationsState } from "./operations";
import { onUnauthorized } from "./session";

const snapshot: OperationsSnapshot = {
  as_of: "2026-11-12T10:00:00Z", timezone: "UTC", scope: "registered_database_records",
  range: { start_date: "2026-11-06", end_date: "2026-11-12" },
  totals: { users: 128, enabled_users: 124, disabled_users: 4, roles: { guest: 112, user: 14, admin: 2 }, showcases: 6 },
  period: { new_users: 12, research_users: 35, research_tasks: 87, conversations: 49 },
  daily: [
    { date: "2026-11-11", new_users: 0, research_users: 0, research_tasks: 0, conversations: 0 },
    { date: "2026-11-12", new_users: 3, research_users: 11, research_tasks: 17, conversations: 9 },
  ],
};
const ok = (body = snapshot) => new Response(JSON.stringify(body), { status: 200 });
const fail = (status: number, detail: string) => new Response(JSON.stringify({ detail }), { status });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function harness() {
  const changes: OperationsState[] = [];
  const loader = new OperationsLoader(value => changes.push(value));
  return { loader, changes, latest: () => changes.at(-1)! };
}

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("运营统计日期与请求", () => {
  const now = new Date("2026-11-12T00:30:00Z");
  it("使用 UTC 自然日，默认近七日由服务端处理，预设与全部没有任意范围上限", () => {
    expect(utcDate(new Date("2026-11-11T20:30:00-04:00"))).toBe("2026-11-12");
    expect(presetDates(7, now)).toEqual({ start: "2026-11-06", end: "2026-11-12" });
    expect(presetDates(30, now)).toEqual({ start: "2026-10-14", end: "2026-11-12" });
    expect(operationsQuery("7", "", "", now)).toBe("");
    expect(operationsQuery("today", "", "", now)).toBe("start_date=2026-11-12&end_date=2026-11-12");
    expect(operationsQuery("30", "", "", now)).toBe("start_date=2026-10-14&end_date=2026-11-12");
    expect(operationsQuery("all")).toBe("all_time=true");
    expect(operationsQuery("custom", "2000-01-01", "2026-11-12")).toBe("start_date=2000-01-01&end_date=2026-11-12");
  });
  it("校验缺失、无效日期、倒置和未来，支持闰日和长期范围", () => {
    for (const [start, end] of [["", "2026-11-12"], ["2026-02-30", "2026-11-12"], ["2026-11-12", "2026-11-11"], ["2026-11-12", "2026-11-13"], ["0000-01-01", "2026-11-12"]]) {
      expect(customRangeError(start, end, now)).not.toBeNull();
    }
    expect(customRangeError("2024-02-29", "2026-11-12", now)).toBeNull();
  });
  it("GET 只访问统计接口，使用 no-store、同源 Cookie 和可取消请求", async () => {
    const fetch = vi.fn().mockResolvedValue(ok()); vi.stubGlobal("fetch", fetch);
    const h = harness(); await h.loader.load("all_time=true");
    expect(fetch).toHaveBeenCalledWith("/api/admin/operations?all_time=true", expect.objectContaining({ cache: "no-store", credentials: "same-origin", signal: expect.any(AbortSignal) }));
    expect(h.latest()).toEqual({ data: snapshot, key: "all_time=true", loading: false, error: null });
    expect(h.changes[0].data).toBeNull();
  });
});

describe("统计加载与错误隔离", () => {
  it("相同范围刷新失败保留带旧时间的数据，首次失败绝不显示零", async () => {
    const fetch = vi.fn().mockRejectedValueOnce(new TypeError("offline")).mockResolvedValueOnce(ok()).mockResolvedValueOnce(fail(503, "运营统计暂不可用"));
    vi.stubGlobal("fetch", fetch); const h = harness();
    await h.loader.load(""); expect(h.latest().data).toBeNull(); expect(h.latest().error).toContain("网络");
    await h.loader.load(""); await h.loader.load("");
    expect(h.latest().data).toEqual(snapshot); expect(h.latest().error).toBe("运营统计暂不可用");
    const html = renderToStaticMarkup(<OperationsStatus state={h.latest()} />);
    expect(html).toContain("以下为旧数据"); expect(html).toContain("2026-11-12 10:00:00 UTC");
  });
  it("换范围时立即清掉旧快照，失败时不冒充新范围", async () => {
    const next = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(ok()).mockReturnValueOnce(next.promise));
    const h = harness(); await h.loader.load(""); const pending = h.loader.load("all_time=true");
    expect(h.latest().data).toBeNull(); expect(h.latest().loading).toBe(true);
    next.resolve(fail(503, "暂不可用")); await pending;
    expect(h.latest().data).toBeNull(); expect(h.latest().error).toBe("暂不可用");
  });
  it.each([401, 403])("%s 拒绝立即清空旧统计，只有 401 通知全局登录失效", async status => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(ok()).mockResolvedValueOnce(fail(status, "拒绝访问")));
    const h = harness(); const notified = vi.fn(() => expect(h.latest().data).toBeNull()); const remove = onUnauthorized(notified);
    try {
      await h.loader.load(""); await h.loader.load("");
      expect(h.latest().data).toBeNull(); expect(h.latest().error).toBe("拒绝访问");
      expect(notified).toHaveBeenCalledTimes(status === 401 ? 1 : 0);
    } finally { remove(); }
  });
  it("重复刷新只发一次；A→B→A 中第一次 A 晚到也不能覆盖最新 A", async () => {
    const a1 = deferred<Response>(), b = deferred<Response>(), a2 = deferred<Response>();
    const fetch = vi.fn().mockReturnValueOnce(a1.promise).mockReturnValueOnce(b.promise).mockReturnValueOnce(a2.promise);
    vi.stubGlobal("fetch", fetch); const h = harness();
    const first = h.loader.load("A"); await h.loader.load("A"); expect(fetch).toHaveBeenCalledTimes(1);
    const second = h.loader.load("B"), third = h.loader.load("A");
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true); expect(fetch.mock.calls[1][1].signal.aborted).toBe(true);
    const fresh = { ...snapshot, period: { ...snapshot.period, research_tasks: 100 } };
    a2.resolve(ok(fresh)); await third; a1.resolve(ok()); b.resolve(ok()); await Promise.all([first, second]);
    expect(h.latest().data).toEqual(fresh); expect(h.latest().key).toBe("A");
  });
  it("离开页面取消请求，晚到结果不能触发更新，返回后重新获取", async () => {
    const pending = deferred<Response>(); const fetch = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValueOnce(ok());
    vi.stubGlobal("fetch", fetch); const h = harness(); const request = h.loader.load(""); h.loader.dispose();
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    const count = h.changes.length; pending.resolve(ok()); await request; await h.loader.load("");
    expect(h.changes).toHaveLength(count); expect(fetch).toHaveBeenCalledTimes(1);
    const next = harness(); await next.loader.load(""); expect(fetch).toHaveBeenCalledTimes(2); expect(next.latest().data).toEqual(snapshot);
  });
});

describe("汇总页面离线渲染", () => {
  it("显示指标口径、真实 0、UTC 范围与倒序趋势，不产生私有记录链接", () => {
    const html = renderToStaticMarkup(<OperationsResults data={snapshot} />);
    for (const label of ["账号总数", "可用账号", "公开示例", "研究用户（去重）", "新建对话", "期间去重数", "UTC", "不受日期筛选影响"]) expect(html).toContain(label);
    expect(html).toContain(">0</td>"); expect(html).toContain('scope="col"'); expect(html).toContain('scope="row"');
    expect(html.indexOf('<th scope="row">2026-11-12')).toBeLessThan(html.indexOf('<th scope="row">2026-11-11'));
    expect(html).not.toContain("href="); expect(html).not.toContain("/api/runs");
  });
  it("未加载时不渲染伪零或旧快照时间", () => {
    const html = renderToStaticMarkup(<OperationsStatus state={INITIAL_OPERATIONS} />);
    expect(html).toContain("正在加载统计"); expect(html).not.toContain("最近成功获取");
  });
});
