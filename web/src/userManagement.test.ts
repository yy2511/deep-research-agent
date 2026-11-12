import { afterEach, describe, expect, it, vi } from "vitest";
import { canManageAccount, editableRoles, ManagementError, ManagementLoader, managementRequest, managementWrite, quotaInputError, usersPath, type LoadState, type ManagedUser, type PlatformRole } from "./userManagement";
import { onUnauthorized } from "./session";

const user = (role: PlatformRole, id = 2): ManagedUser => ({ id, role, email: null, display_name: "合成账号", status: "active", management_version: 1, created_at: "", quota: null, usage: null });
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
afterEach(() => vi.unstubAllGlobals());

describe("用户管理权限与输入合同", () => {
  it("仅负责人可管理管理员，任何人均不能管理自己或负责人", () => {
    for (const actor of ["owner", "admin", "user", "guest"] as const) {
      for (const target of ["owner", "admin", "user", "guest"] as const) {
        expect(canManageAccount({ id: 1, role: actor }, user(target))).toBe(target !== "owner" && (actor === "owner" || (actor === "admin" && ["user", "guest"].includes(target))));
        expect(canManageAccount({ id: 2, role: actor }, user(target))).toBe(false);
      }
    }
    expect(editableRoles({ role: "owner" })).toEqual(["admin", "user", "guest"]);
    expect(editableRoles({ role: "admin" })).toEqual(["user", "guest"]);
    expect(editableRoles({ role: "user" })).toEqual([]);
    expect(editableRoles({ role: "guest" })).toEqual([]);
  });
  it("支持数据库整数全范围，拒绝空白、负数、小数和越界而不截断", () => {
    for (const valid of ["0", "1", "0002", "9007199254740992", "9223372036854775807"]) expect(quotaInputError(valid, valid)).toBeNull();
    for (const invalid of ["", " ", "-1", "1.1", "1e3", "Infinity", "9223372036854775808"]) {
      expect(quotaInputError(invalid, "1")).not.toBeNull(); expect(quotaInputError("1", invalid)).not.toBeNull();
    }
  });
  it("搜索与游标编码，无过滤时不发送多余参数", () => {
    expect(usersPath({ search: "", role: "", status: "" })).toBe("/api/admin/users");
    expect(usersPath({ search: " a+b@example.test & ", role: "guest", status: "disabled" }, 4)).toBe("/api/admin/users?q=a%2Bb%40example.test+%26&role=guest&status=disabled&cursor=4");
  });
});

describe("管理请求不缓存且不隐式重试", () => {
  it("保存保留十进制精度，使用同源 cookie 与 no-store", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ ok: true })); vi.stubGlobal("fetch", fetch);
    const body = { daily_runs_limit: "9223372036854775807", expected_version: 2 };
    await managementWrite("/api/admin/users/2/quota", "PUT", body);
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/admin/users/2/quota", { method: "PUT", body: JSON.stringify(body), headers: { "Content-Type": "application/json" }, cache: "no-store", credentials: "same-origin" });
  });
  it("409 与非法 JSON 错误均保留状态，不自动重试写入", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response({ detail: { message: "版本冲突" } }, 409)).mockResolvedValueOnce(new Response("unavailable", { status: 503 })); vi.stubGlobal("fetch", fetch);
    await expect(managementWrite("/quota", "PUT", {})).rejects.toMatchObject({ status: 409, message: "版本冲突" });
    await expect(managementRequest("/users")).rejects.toBeInstanceOf(ManagementError);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

describe("管理加载生命周期", () => {
  it("A → B → A 时旧成功和旧 401 不覆盖最新值，也不登出", async () => {
    const first = deferred<Response>(); const second = deferred<Response>(); const third = deferred<Response>();
    const fetch = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise).mockReturnValueOnce(third.promise); vi.stubGlobal("fetch", fetch);
    const states: Array<LoadState<{ id: number }>> = []; const logout = vi.fn(); const unsubscribe = onUnauthorized(logout);
    const loader = new ManagementLoader<{ id: number }>(state => states.push(state));
    try {
      const a = loader.load("/A"); const b = loader.load("/B"); const c = loader.load("/A");
      expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
      third.resolve(response({ id: 3 })); await c;
      first.resolve(response({ id: 1 })); second.resolve(response({ detail: "旧会话错误" }, 401)); await Promise.all([a, b]);
      expect(states.at(-1)?.data).toEqual({ id: 3 }); expect(logout).not.toHaveBeenCalled();
    } finally { unsubscribe(); loader.dispose(); }
  });
  it("重新加载立即清除旧数据，401/403 清除数据且只有 401 通知登录失效", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response({ secret: "synthetic" })).mockResolvedValueOnce(response({ detail: "禁止" }, 403)).mockResolvedValueOnce(response({ detail: "登录失效" }, 401)); vi.stubGlobal("fetch", fetch);
    const states: Array<LoadState<unknown>> = []; const logout = vi.fn(); const unsubscribe = onUnauthorized(logout); const loader = new ManagementLoader(state => states.push(state));
    try {
      await loader.load("/A"); const denied = loader.load("/B"); expect(states.at(-1)?.data).toBeNull(); await denied;
      expect(states.at(-1)).toEqual({ data: null, loading: false, error: "禁止", denied: true }); expect(logout).not.toHaveBeenCalled();
      await loader.load("/C"); expect(logout).toHaveBeenCalledOnce();
    } finally { unsubscribe(); loader.dispose(); }
  });
  it("离开或被拒绝后 dispose，忽略未遵守 abort 的晚到响应且不再发请求", async () => {
    const late = deferred<Response>(); const fetch = vi.fn().mockReturnValue(late.promise); vi.stubGlobal("fetch", fetch);
    const changed = vi.fn(); const loader = new ManagementLoader(changed); const pending = loader.load("/users"); loader.dispose();
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true); late.resolve(response({ users: [1] })); await pending; await loader.load("/again");
    expect(changed).toHaveBeenCalledTimes(1); expect(fetch).toHaveBeenCalledTimes(1);
  });
});
