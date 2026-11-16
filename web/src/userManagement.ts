/** Account-management requests never persist identities or administrative data in browser storage. */
import { notifyUnauthorized } from "./session";
import type { Account } from "./auth";

export type PlatformRole = "owner" | "admin" | "user" | "guest";
export const ROLE_LABELS: Record<PlatformRole, string> = {
  owner: "平台负责人", admin: "平台管理员", user: "平台普通用户", guest: "访客",
};
export type QuotaPolicy = {
  mode: "limited" | "unlimited";
  daily_runs_limit: string | number | null;
  daily_briefs_limit: string | number | null;
  version: number;
};
export type UsageStatus = {
  mode: "limited" | "unlimited" | "global";
  runs_used: string | number; briefs_used: string | number;
  runs_limit: string | number | null; briefs_limit: string | number | null;
  runs_left: string | number | null; briefs_left: string | number | null;
  source?: "platform" | "personal" | "role"; open?: boolean; global_exhausted?: boolean;
  timezone: string; resets_at: string | null;
  tracking_started_at?: string | null;
};
export type ManagedUser = {
  id: number; email: string | null; display_name: string | null;
  role: PlatformRole; status: "active" | "disabled";
  management_version: number; created_at: string;
  quota: QuotaPolicy | null;
  usage: UsageStatus | null;
};
export type Governance = { owner_user_id: number | null; version: number };
export type UsersResult = { items: ManagedUser[]; next_cursor: number | null; governance: Governance };
export type UserFilters = { search: string; role: string; status: string };
export type AuditEvent = {
  id: number; actor_id: number | null; target_id: number | null;
  action: string; created_at: string; reason?: string;
};

export class ManagementError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}
export async function managementRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { ...options, cache: "no-store", credentials: "same-origin" });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail
      : typeof body.detail?.message === "string" ? body.detail.message : "用户管理请求失败，请重试。";
    throw new ManagementError(detail, response.status);
  }
  return response.json();
}
export function managementWrite<T>(path: string, method: "POST" | "PATCH" | "PUT", body: unknown): Promise<T> {
  return managementRequest(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}
export function usersPath(filters: UserFilters, cursor: number | null = null): string {
  const query = new URLSearchParams();
  if (filters.search.trim()) query.set("q", filters.search.trim());
  if (filters.role) query.set("role", filters.role);
  if (filters.status) query.set("status", filters.status);
  if (cursor !== null) query.set("cursor", String(cursor));
  return `/api/admin/users${query.size ? `?${query}` : ""}`;
}
export function canManageAccount(actor: Pick<Account, "id" | "role">, target: ManagedUser): boolean {
  if (actor.id === target.id || target.role === "owner") return false;
  return actor.role === "owner" || (actor.role === "admin" && (target.role === "user" || target.role === "guest"));
}
export function editableRoles(actor: Pick<Account, "role">): Array<Exclude<PlatformRole, "owner">> {
  return actor.role === "owner" ? ["admin", "user", "guest"] : actor.role === "admin" ? ["user", "guest"] : [];
}
export function quotaInputError(runs: string, briefs: string): string | null {
  const valid = (value: string) => /^\d+$/.test(value) && BigInt(value) <= 9223372036854775807n;
  return valid(runs) && valid(briefs) ? null : "研究和规划次数须为 0–9223372036854775807 的整数；0 表示禁止新的调用。";
}
export type LoadState<T> = { data: T | null; loading: boolean; error: string | null; denied: boolean };
export const emptyLoad = <T>(): LoadState<T> => ({ data: null, loading: true, error: null, denied: false });

/** Generations protect against A → B → A races even when a fetch implementation ignores abort. */
export class ManagementLoader<T> {
  private generation = 0;
  private controller: AbortController | null = null;
  private disposed = false;
  private readonly changed: (state: LoadState<T>) => void;
  constructor(changed: (state: LoadState<T>) => void) { this.changed = changed; }
  async load(path: string) {
    if (this.disposed) return;
    this.controller?.abort();
    const controller = new AbortController(); this.controller = controller;
    const sequence = ++this.generation;
    this.changed(emptyLoad<T>());
    try {
      const data = await managementRequest<T>(path, { signal: controller.signal });
      if (this.disposed || sequence !== this.generation) return;
      this.changed({ data, loading: false, error: null, denied: false });
    } catch (error) {
      if (this.disposed || sequence !== this.generation) return;
      const denied = error instanceof ManagementError && [401, 403].includes(error.status);
      this.changed({ data: null, loading: false, error: error instanceof Error ? error.message : "读取失败", denied });
      if (error instanceof ManagementError && error.status === 401) notifyUnauthorized();
    }
  }
  dispose() { this.disposed = true; ++this.generation; this.controller?.abort(); }
}
