/**
 * 登录状态（阶段 1 设计 §2，ADR 0005）。
 *
 * 身份在 HttpOnly Cookie 里，前端读不到也不保存令牌；只通过 /api/me 知道“现在是谁”。
 * 任何请求收到 401，就通知 AuthGate 回到登录页（notifyUnauthorized）。
 */
import { ApiError } from "./api";

export { onUnauthorized } from "./session";

export type Account = { id: number; email: string; role: "user" | "admin" };

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

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST" });
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
