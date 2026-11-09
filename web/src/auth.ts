/**
 * 登录状态（阶段 1 设计 §2，ADR 0005）。
 *
 * 身份在 HttpOnly Cookie 里，前端读不到也不保存令牌；只通过 /api/me 知道“现在是谁”。
 * 任何请求收到 401，就通知 AuthGate 回到登录页（notifyUnauthorized）。
 */
import { ApiError } from "./api";
import type { TrialStatus } from "./trial";

export { onUnauthorized } from "./session";

/** GitHub 登录的访客（guest）没有邮箱，显示 display_name（阶段 2，#45）。 */
export type Account = {
  id: number; email: string | null; role: "guest" | "user" | "admin";
  display_name?: string | null; avatar_url?: string | null;
  /** 访客的试用次数（#49/#50）；非访客为 null。 */
  trial?: TrialStatus | null;
};

/** 侧边栏等处显示的名字。 */
export function accountLabel(account: Account): string {
  return account.display_name || account.email || `用户 ${account.id}`;
}

export const GITHUB_LOGIN_URL = "/api/auth/github/start";

/** 服务端是否配置了 GitHub 登录。查不到时当作没有，不影响邮箱登录。 */
export async function fetchAuthOptions(): Promise<{ github: boolean }> {
  try {
    const res = await fetch("/api/auth/options");
    if (!res.ok) return { github: false };
    const body = await res.json();
    return { github: body?.github === true };
  } catch {
    return { github: false };
  }
}

/** GitHub 登录失败时，服务端跳回 /?login_error=<代码>；这里换成说明。只认已知代码，不显示网址里的原文。 */
const GITHUB_LOGIN_ERRORS: Record<string, string> = {
  denied: "你在 GitHub 上取消了授权，没有登录。",
  state: "登录链接已失效或来自别的页面，请重新点“用 GitHub 登录”。",
  github: "暂时无法通过 GitHub 确认身份，请稍后重试。",
  disabled: "账号已停用。",
  unavailable: "服务暂时不可用，请稍后重试。",
};

export function loginErrorFrom(search: string): string | null {
  const code = new URLSearchParams(search).get("login_error");
  if (code === null) return null;
  return GITHUB_LOGIN_ERRORS[code] ?? "登录没有完成，请重试。";
}

export const PASSWORD_MIN = 8;
export const PASSWORD_MAX = 256;

async function errorOf(res: Response, fallback: string): Promise<ApiError> {
  const body = await res.json().catch(() => ({}));
  return new ApiError(typeof body.detail === "string" && body.detail ? body.detail : fallback);
}

/** 当前登录用户；未登录返回 null，其他错误（如数据库不可用）抛出。 */
export async function fetchMe(): Promise<Account | null> {
  const res = await fetch("/api/me");
  if (res.status === 401) return null;
  if (!res.ok) throw await errorOf(res, "无法确认登录状态");
  return res.json();
}

async function postAuth(url: string, body: unknown): Promise<Account> {
  const res = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) throw await errorOf(res, "请求失败");
  return res.json();
}

export function login(email: string, password: string): Promise<Account> {
  return postAuth("/api/auth/login", { email, password });
}

export function register(inviteCode: string, email: string, password: string): Promise<Account> {
  return postAuth("/api/auth/register", { invite_code: inviteCode, email, password });
}

/**
 * 退出登录。只有服务端确认会话已失效才算退出：成功，或 401（本来就已失效）。
 * 其他错误（如数据库暂时不可用、网络断开）抛出，会话和 Cookie 可能仍然有效，界面不能当成已退出。
 */
export async function logout(): Promise<void> {
  let res: Response;
  try {
    res = await fetch("/api/auth/logout", { method: "POST" });
  } catch {
    throw new ApiError("网络连接失败");
  }
  if (res.ok || res.status === 401) return;
  throw await errorOf(res, "服务暂时不可用");
}

export type AuthMode = "login" | "register";
export type AuthFields = { email: string; password: string; confirm: string; invite: string };

/** 提交前的本地检查；只查明显问题，最终以服务端为准。 */
export function validateAuthForm(mode: AuthMode, f: AuthFields): string | null {
  if (!f.email.trim()) return "请填写邮箱";
  if (!f.password) return "请填写密码";
  if (mode === "login") return null;
  if (!f.invite.trim()) return "请填写邀请码";
  if (f.password.length < PASSWORD_MIN || f.password.length > PASSWORD_MAX) {
    return `密码须为 ${PASSWORD_MIN}–${PASSWORD_MAX} 个字符`;
  }
  if (f.password !== f.confirm) return "两次输入的密码不一致";
  return null;
}
