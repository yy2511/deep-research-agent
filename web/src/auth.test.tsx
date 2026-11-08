import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { getJSON, probeSession } from "./api";
import { accountLabel, fetchAuthOptions, fetchMe, login, loginErrorFrom, logout, register, validateAuthForm } from "./auth";
import { AuthPage, LogoutError } from "./components/AuthGate";
import { onUnauthorized } from "./session";

function stubFetch(status: number, body: unknown = {}) {
  const calls: { url: string; init?: RequestInit }[] = [];
  vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  });
  return calls;
}

afterEach(() => { vi.unstubAllGlobals(); });

describe("登录页", () => {
  it("默认是登录，提供注册入口和忘记密码说明", () => {
    const html = renderToStaticMarkup(<AuthPage onAuthed={() => {}} />);
    for (const text of ["研究工作台", "登录", "邮箱", "密码", "注册账号", "忘记密码请联系管理员重设"]) {
      expect(html).toContain(text);
    }
    expect(html).not.toContain('name="invite"');
    expect(html).not.toContain('name="confirm"');
    expect(html).toMatch(/autocomplete="current-password"/i);
  });

  it("注册需要邀请码、确认密码，并说明密码长度", () => {
    const html = renderToStaticMarkup(<AuthPage onAuthed={() => {}} initialMode="register" />);
    for (const text of ["用邀请码注册", "邀请码", "确认密码", "8–256 个字符", "注册并登录", "去登录"]) {
      expect(html).toContain(text);
    }
    for (const name of ["invite", "email", "password", "confirm"]) expect(html).toContain(`name="${name}"`);
    expect(html).toMatch(/autocomplete="new-password"/i);
  });

  it("登录失效时说明原因", () => {
    const html = renderToStaticMarkup(<AuthPage onAuthed={() => {}} notice="登录已过期，请重新登录。" />);
    expect(html).toContain("登录已过期，请重新登录。");
  });
});

describe("提交前检查", () => {
  const ok = { email: "a@example.com", password: "long enough", confirm: "long enough", invite: "code" };
  it("登录只要求填写邮箱和密码", () => {
    expect(validateAuthForm("login", ok)).toBeNull();
    expect(validateAuthForm("login", { ...ok, email: " " })).toBe("请填写邮箱");
    expect(validateAuthForm("login", { ...ok, password: "" })).toBe("请填写密码");
    // 登录不在前端检查长度：旧密码规则以服务端为准。
    expect(validateAuthForm("login", { ...ok, password: "short" })).toBeNull();
  });
  it("注册检查邀请码、长度和两次输入", () => {
    expect(validateAuthForm("register", ok)).toBeNull();
    expect(validateAuthForm("register", { ...ok, invite: "" })).toBe("请填写邀请码");
    expect(validateAuthForm("register", { ...ok, password: "short", confirm: "short" })).toBe("密码须为 8–256 个字符");
    expect(validateAuthForm("register", { ...ok, password: "x".repeat(257), confirm: "x".repeat(257) })).toBe("密码须为 8–256 个字符");
    expect(validateAuthForm("register", { ...ok, confirm: "different" })).toBe("两次输入的密码不一致");
  });
});

describe("登录状态", () => {
  it("/api/me 401 表示未登录，其他错误如实抛出", async () => {
    stubFetch(401, { detail: "未登录或登录已过期" });
    expect(await fetchMe()).toBeNull();
    stubFetch(503, { detail: "数据库未就绪" });
    await expect(fetchMe()).rejects.toThrow("数据库未就绪");
    stubFetch(200, { id: 1, email: "a@example.com", role: "admin" });
    expect(await fetchMe()).toEqual({ id: 1, email: "a@example.com", role: "admin" });
  });

  it("登录与注册发送 JSON，失败时显示服务端的说明", async () => {
    const calls = stubFetch(200, { id: 2, email: "b@example.com", role: "user" });
    await login("b@example.com", "pw-pw-pw-pw");
    await register("code", "b@example.com", "pw-pw-pw-pw");
    expect(calls.map((c) => [c.url, c.init?.method, JSON.parse(String(c.init?.body))])).toEqual([
      ["/api/auth/login", "POST", { email: "b@example.com", password: "pw-pw-pw-pw" }],
      ["/api/auth/register", "POST", { invite_code: "code", email: "b@example.com", password: "pw-pw-pw-pw" }],
    ]);
    stubFetch(401, { detail: "邮箱或密码错误" });
    await expect(login("b@example.com", "wrong")).rejects.toThrow("邮箱或密码错误");
  });

  it("任何普通请求收到 401 都通知回到登录页", async () => {
    let fired = 0;
    const off = onUnauthorized(() => { fired += 1; });
    stubFetch(401, { detail: "未登录或登录已过期" });
    await expect(getJSON("/api/runs")).rejects.toThrow("未登录或登录已过期");
    expect(fired).toBe(1);
    stubFetch(500, { detail: "坏了" });
    await expect(getJSON("/api/runs")).rejects.toThrow("坏了");
    expect(fired).toBe(1);
    off();
    stubFetch(401);
    await expect(getJSON("/api/runs")).rejects.toThrow();
    expect(fired).toBe(1);
  });

  it("进度流断开时借 /api/me 判断是否登录失效", async () => {
    let fired = 0;
    const off = onUnauthorized(() => { fired += 1; });
    stubFetch(200, { id: 1 });
    await probeSession();
    expect(fired).toBe(0);
    stubFetch(401);
    await probeSession();
    expect(fired).toBe(1);
    vi.stubGlobal("fetch", async () => { throw new TypeError("network down"); });
    await probeSession();
    expect(fired).toBe(1);
    off();
  });
});

describe("退出登录", () => {
  it("服务端确认退出或会话本已失效，才算退出", async () => {
    const calls = stubFetch(200, { logged_out: true });
    await expect(logout()).resolves.toBeUndefined();
    expect(calls[0]).toMatchObject({ url: "/api/auth/logout", init: { method: "POST" } });
    stubFetch(401, { detail: "未登录或登录已过期" });
    await expect(logout()).resolves.toBeUndefined();
  });

  it("服务暂时不可用或网络失败时报错，不当成已退出", async () => {
    stubFetch(503, { detail: "数据库暂时不可用，请稍后再试" });
    await expect(logout()).rejects.toThrow("数据库暂时不可用，请稍后再试");
    stubFetch(500, {});
    await expect(logout()).rejects.toThrow("服务暂时不可用");
    vi.stubGlobal("fetch", async () => { throw new TypeError("network down"); });
    await expect(logout()).rejects.toThrow("网络连接失败");
  });

  it("失败提示说明仍处于登录状态，并提供重试", () => {
    const html = renderToStaticMarkup(<LogoutError message="网络连接失败" onRetry={() => {}} onDismiss={() => {}} />);
    for (const text of ["退出登录失败（网络连接失败）", "当前仍处于登录状态", "重试", 'role="alert"']) expect(html).toContain(text);
  });
});

describe("GitHub 登录（#45）", () => {
  it("服务端配置了才显示按钮，按钮整页跳到后端的 GitHub 入口", () => {
    const off = renderToStaticMarkup(<AuthPage onAuthed={() => {}} />);
    expect(off).not.toContain("用 GitHub 登录");
    const on = renderToStaticMarkup(<AuthPage onAuthed={() => {}} github />);
    expect(on).toContain('href="/api/auth/github/start"');
    expect(on).toContain("用 GitHub 登录");
    expect(on).toContain("或用邮箱登录");
    // 注册页是邀请码注册，不显示 GitHub 按钮。
    expect(renderToStaticMarkup(<AuthPage onAuthed={() => {}} github initialMode="register" />)).not.toContain("用 GitHub 登录");
  });

  it("查询是否启用 GitHub 登录；查不到时当作没有", async () => {
    stubFetch(200, { github: true });
    await expect(fetchAuthOptions()).resolves.toEqual({ github: true });
    stubFetch(500, {});
    await expect(fetchAuthOptions()).resolves.toEqual({ github: false });
    vi.stubGlobal("fetch", async () => { throw new TypeError("network down"); });
    await expect(fetchAuthOptions()).resolves.toEqual({ github: false });
  });

  it("跳回来的错误代码换成说明，不显示网址里的原文", () => {
    expect(loginErrorFrom("")).toBeNull();
    expect(loginErrorFrom("?login_error=denied")).toContain("取消了授权");
    expect(loginErrorFrom("?login_error=state")).toContain("重新点");
    expect(loginErrorFrom("?login_error=<script>")).toBe("登录没有完成，请重试。");
    const html = renderToStaticMarkup(<AuthPage onAuthed={() => {}} github initialError={loginErrorFrom("?login_error=github")} />);
    expect(html).toContain("暂时无法通过 GitHub 确认身份");
  });

  it("没有邮箱的访客显示 GitHub 名字", () => {
    expect(accountLabel({ id: 3, email: null, role: "guest", display_name: "Octo Cat" })).toBe("Octo Cat");
    expect(accountLabel({ id: 1, email: "a@b.co", role: "admin" })).toBe("a@b.co");
    expect(accountLabel({ id: 7, email: null, role: "guest" })).toBe("用户 7");
  });
});
