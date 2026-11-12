import { useEffect, useRef, useState } from "react";
import type { Account } from "../auth";
import { notifyUnauthorized } from "../session";
import {
  canManageAccount, editableRoles, emptyLoad, ManagementError, ManagementLoader,
  managementWrite, quotaInputError, ROLE_LABELS, usersPath,
  type AuditEvent, type LoadState, type ManagedUser, type PlatformRole, type UserFilters, type UsersResult,
} from "../userManagement";

const EMPTY_FILTERS: UserFilters = { search: "", role: "", status: "" };
const identity = (user: ManagedUser) => user.display_name || user.email || `用户 ${user.id}`;
const count = (value: string | number | null | undefined) => value == null ? "不限" : String(value);

function LoadNotice({ state }: { state: Pick<LoadState<unknown>, "loading" | "error"> }) {
  return <>{state.loading && <p role="status" className="field-hint">正在读取…</p>}
    {state.error && <p role="alert" className="field-hint is-bad">{state.error}</p>}</>;
}

export function UserManagement({ account, onTrial }: { account: Account; onTrial: () => void }) {
  const [state, setState] = useState<LoadState<UsersResult>>(emptyLoad);
  const [draft, setDraft] = useState(EMPTY_FILTERS);
  const [selected, setSelected] = useState<number | null>(null);
  const [notice, setNotice] = useState("");
  const applied = useRef(EMPTY_FILTERS);
  const cursors = useRef<Array<number | null>>([null]);
  const loader = useRef<ManagementLoader<UsersResult> | null>(null);
  useEffect(() => {
    const current = new ManagementLoader<UsersResult>(next => { setState(next); if (next.denied) setSelected(null); });
    loader.current = current; void current.load(usersPath(EMPTY_FILTERS));
    return () => { current.dispose(); loader.current = null; };
  }, []);
  const refresh = () => void loader.current?.load(usersPath(applied.current, cursors.current.at(-1) ?? null));
  const data = state.data;
  const deny = () => {
    loader.current?.dispose();
    setSelected(null);
    setState({ data: null, loading: false, error: "管理权限已失效，请重新登录。", denied: true });
  };
  return <div className="settings-section user-management">
    <div className="section-head"><div><h2>用户管理</h2><p className="field-hint">管理账号身份、状态和每日免费次数，不显示私人研究内容。</p></div>
      <button type="button" className="btn btn-sm" disabled={state.loading || state.denied} onClick={refresh}>刷新</button></div>
    {data?.governance.owner_user_id === null && <p className="notice">尚未设置平台负责人，请联系部署维护者在服务器明确指定负责人。不会自动选择账号。</p>}
    <form className="management-filters" onSubmit={event => { event.preventDefault(); applied.current = { ...draft }; cursors.current = [null]; setNotice(""); void loader.current?.load(usersPath(draft)); }}>
      <label>查找账号<input className="input" type="search" value={draft.search} placeholder="邮箱或 GitHub 账号" onChange={event => setDraft({ ...draft, search: event.target.value })} /></label>
      <label>身份<select value={draft.role} onChange={event => setDraft({ ...draft, role: event.target.value })}><option value="">全部身份</option>{Object.entries(ROLE_LABELS).map(([role, label]) => <option key={role} value={role}>{label}</option>)}</select></label>
      <label>状态<select value={draft.status} onChange={event => setDraft({ ...draft, status: event.target.value })}><option value="">全部状态</option><option value="active">正常</option><option value="disabled">停用</option></select></label>
      <button className="btn btn-sm" type="submit" disabled={state.denied}>查询</button>
    </form>
    {notice && <p className="notice" role="status">{notice}</p>}
    <LoadNotice state={state} />
    {data && <>
      <div className="management-table-wrap"><table className="management-table"><caption>平台账号及每日研究使用情况</caption>
        <thead><tr><th>账号</th><th>身份</th><th>额度来源</th><th>今日研究</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>{data.items.map(user => <tr key={user.id}>
          <td data-label="账号"><strong>{identity(user)}</strong><small>{user.email || "GitHub 登录"} · ID {user.id}</small></td>
          <td data-label="身份">{ROLE_LABELS[user.role]}</td><td data-label="额度来源">{user.role === "guest" ? "平台通用配置" : user.role === "user" ? "个人设置" : "管理账号"}</td>
          <td data-label="今日研究">{user.usage ? `${user.usage.runs_used} / ${count(user.usage.runs_limit)}` : "暂无数据"}</td>
          <td data-label="状态">{user.status === "active" ? "正常" : "停用"}</td>
          <td><button className="btn btn-sm" type="button" onClick={() => { setSelected(user.id); setNotice(""); }}>查看{canManageAccount(account, user) ? "/编辑" : ""}</button></td>
        </tr>)}</tbody>
      </table></div>
      {!data.items.length && <p className="field-hint">没有符合条件的账号。</p>}
      <div className="management-pagination"><button type="button" className="btn btn-sm" disabled={cursors.current.length === 1 || state.loading} onClick={() => { cursors.current.pop(); refresh(); }}>上一页</button>
        <button type="button" className="btn btn-sm" disabled={data.next_cursor === null || state.loading} onClick={() => { if (data.next_cursor !== null) { cursors.current.push(data.next_cursor); refresh(); } }}>下一页</button></div>
    </>}
    {!state.denied && <ManagementAudit onDenied={deny} />}
    {selected !== null && <UserDetail key={selected} id={selected} account={account} onClose={() => setSelected(null)} onTrial={() => { setSelected(null); onTrial(); }}
      onSaved={() => { setNotice("账号设置已更新。"); refresh(); }} onDenied={deny} />}
  </div>;
}

type PendingChange = { title: string; description: string; path: string; method: "PATCH" | "PUT"; body: Record<string, unknown> };

export function UserDetail({ id, account, onClose, onSaved, onTrial, onDenied }: {
  id: number; account: Account; onClose: () => void; onSaved: () => void; onTrial: () => void; onDenied: () => void;
}) {
  const [state, setState] = useState<LoadState<ManagedUser>>(emptyLoad);
  const [role, setRole] = useState<PlatformRole>("user");
  const [mode, setMode] = useState("limited");
  const [runs, setRuns] = useState(""); const [briefs, setBriefs] = useState("");
  const [reason, setReason] = useState(""); const [pending, setPending] = useState<PendingChange | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const dialog = useRef<HTMLDialogElement | null>(null); const writing = useRef(false); const active = useRef(true);
  const loader = useRef<ManagementLoader<ManagedUser> | null>(null);
  useEffect(() => {
    active.current = true;
    const focused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.current?.showModal();
    const current = new ManagementLoader<ManagedUser>(next => {
      setState(next);
      if (next.denied) onDenied();
      if (next.data) { setRole(next.data.role); setMode(next.data.quota?.mode === "unlimited" && account.role === "owner" ? "unlimited" : "limited"); setRuns(next.data.quota?.daily_runs_limit == null ? "" : String(next.data.quota.daily_runs_limit)); setBriefs(next.data.quota?.daily_briefs_limit == null ? "" : String(next.data.quota.daily_briefs_limit)); }
    });
    loader.current = current; void current.load(`/api/admin/users/${id}`);
    return () => { active.current = false; current.dispose(); dialog.current?.close(); focused?.focus(); };
  }, [id]); // The dialog is keyed by account ID; callbacks are intentionally not load triggers.
  const user = state.data;
  const editable = user && canManageAccount(account, user);
  function quota() {
    if (mode === "limited") { const invalid = quotaInputError(runs, briefs); if (invalid) { setError(invalid); return null; } }
    return { mode, daily_runs_limit: mode === "limited" ? runs : null, daily_briefs_limit: mode === "limited" ? briefs : null, expected_version: user?.quota?.version ?? null };
  }
  function propose(change: PendingChange) { if (writing.current) return; setError(""); setSuccess(""); setPending(change); }
  async function save() {
    if (!pending || writing.current) return;
    writing.current = true; setBusy(true); setError("");
    try {
      await managementWrite(pending.path, pending.method, { ...pending.body, reason });
      if (!active.current) return;
      setPending(null); setSuccess("已保存。角色或状态改变后，该账号需要重新登录。"); setReason(""); onSaved();
      void loader.current?.load(`/api/admin/users/${id}`);
    } catch (failure) {
      if (!active.current) return;
      setPending(null);
      if (failure instanceof ManagementError && (failure.status === 401 || failure.status === 403)) {
        setState({ data: null, loading: false, error: failure.message, denied: true }); onDenied();
        if (failure.status === 401) notifyUnauthorized();
      } else if (failure instanceof ManagementError && failure.status === 409) {
        setError("账号已被其他管理者修改，已重新加载。请核对最新值后重新选择，不会自动覆盖。");
        void loader.current?.load(`/api/admin/users/${id}`);
      } else setError(failure instanceof Error ? failure.message : "保存失败；请刷新确认结果后再重试。");
    } finally { writing.current = false; if (active.current) setBusy(false); }
  }
  return <dialog ref={dialog} className="management-dialog" aria-labelledby="user-detail-title" onCancel={event => { event.preventDefault(); if (!writing.current) onClose(); }}>
    <div className="management-dialog-head"><h2 id="user-detail-title">账号详情</h2><button type="button" className="btn btn-sm" disabled={busy} onClick={onClose}>关闭</button></div>
    <LoadNotice state={state} />
    {error && <p role="alert" className="field-hint is-bad">{error}</p>}{success && <p role="status" className="notice">{success}</p>}
    {user && <>
      <section className="management-identity"><h3>{identity(user)}</h3><p>ID {user.id} · {ROLE_LABELS[user.role]} · {user.status === "active" ? "正常" : "停用"}</p>{user.email && <p>{user.email}</p>}
        {user.usage && <p className="field-hint">今日研究 {user.usage.runs_used}/{count(user.usage.runs_limit)}，规划 {user.usage.briefs_used}/{count(user.usage.briefs_limit)}。时区 {user.usage.timezone}，下次重置 {user.usage.resets_at ? new Date(user.usage.resets_at).toLocaleString() : "暂不可用"}。</p>}
        {user.usage?.tracking_started_at && <p className="field-hint">使用记录从 {new Date(user.usage.tracking_started_at).toLocaleString()} 起计，不代表完整历史。</p>}
      </section>
      {!editable && <p className="notice">该账号只能查看。不能管理自己、负责人或超出你的权限层级的账号。</p>}
      {user.role === "guest" && <section className="card"><h3>访客免费额度</h3><p>由平台通用配置决定，不支持个人覆盖。</p><button type="button" className="btn btn-sm" onClick={onTrial} disabled={busy}>查看平台试用配置</button></section>}
      {editable && <fieldset disabled={busy} className="management-fields">
        <label>操作说明<input className="input" value={reason} onChange={event => setReason(event.target.value)} placeholder="说明调整原因（写入管理记录）" /></label>
        <section className="card"><h3>账号状态</h3><p>停用后不能登录或发起新研究；不会删除研究记录。</p><button type="button" className="btn btn-sm" onClick={() => propose({ title: user.status === "active" ? "确认停用账号" : "确认恢复账号", description: `${identity(user)}（ID ${user.id}）将${user.status === "active" ? "停用，现有登录会话失效" : "恢复登录权限"}。`, path: `/api/admin/users/${id}/status`, method: "PATCH", body: { status: user.status === "active" ? "disabled" : "active", expected_version: user.management_version } })}>{user.status === "active" ? "停用账号" : "恢复账号"}</button></section>
        <section className="card"><h3>身份</h3><label>新的身份<select value={role} onChange={event => { setRole(event.target.value as PlatformRole); setPending(null); }}>{editableRoles(account).map(value => <option key={value} value={value}>{ROLE_LABELS[value]}</option>)}</select></label>
          <p className="field-hint">普通用户可使用代码、浏览器、MCP、资料库和附件等完整研究能力；访客仅用网页及全站试用额度。管理员拥有全站配置权且不限次数。</p>
          {role === "user" && user.role !== "user" && <p className="notice">转换为普通用户时，必须同时确认下面的个人每日额度。</p>}
          <button type="button" className="btn btn-sm" disabled={role === user.role} onClick={() => { const policy = role === "user" ? quota() : undefined; if (role === "user" && !policy) return; propose({ title: "确认变更身份", description: `${identity(user)}（ID ${user.id}）：${ROLE_LABELS[user.role]} → ${ROLE_LABELS[role]}。${role === "admin" ? "将授予全站配置管理权和不限次数的研究能力。" : role === "guest" ? "将仅能使用网页研究，不能使用代码、浏览器、MCP、资料库和附件，并改用平台通用额度。" : "将允许使用代码、浏览器、MCP、资料库和附件等研究能力，并采用已确认的个人额度。"}该账号需要重新登录。`, path: `/api/admin/users/${id}/role`, method: "PATCH", body: { role, expected_version: user.management_version, ...(policy ? { quota: policy } : {}) } }); }}>变更身份</button>
        </section>
        {(user.role === "user" || role === "user") && <section className="card"><h3>个人每日免费次数</h3>
          <label>额度模式<select value={mode} onChange={event => { setMode(event.target.value); setPending(null); }}><option value="limited">设置每日次数</option>{account.role === "owner" && <option value="unlimited">不限次数</option>}</select></label>
          {mode === "limited" && <div className="field-grid"><label>每日研究次数<input className="input" type="number" min="0" max="9223372036854775807" step="1" value={runs} onChange={event => { setRuns(event.target.value); setPending(null); }} /></label><label>每日规划次数<input className="input" type="number" min="0" max="9223372036854775807" step="1" value={briefs} onChange={event => { setBriefs(event.target.value); setPending(null); }} /></label></div>}
          <p className="field-hint">0 表示禁止新的调用。首问与追问各算一次，规划另计。调整不会清空今日已用；只有负责人可以授予不限次数。</p>
          {user.role === "user" && <button type="button" className="btn btn-sm" onClick={() => { const policy = quota(); if (!policy) return; propose({ title: "确认修改每日额度", description: `${identity(user)}（ID ${user.id}）：${mode === "unlimited" ? "不限次数" : `每天研究 ${runs} 次、规划 ${briefs} 次`}，保留今日已用次数。`, path: `/api/admin/users/${id}/quota`, method: "PUT", body: { ...policy, expected_management_version: user.management_version } }); }}>保存个人额度</button>}
        </section>}
      </fieldset>}
      {pending && <section className="management-confirm" aria-label="确认账号修改"><h3>{pending.title}</h3><p>{pending.description}</p><div className="management-actions"><button type="button" className="btn btn-ghost" disabled={busy} onClick={() => setPending(null)}>取消修改</button><button type="button" className="btn btn-primary" disabled={busy} onClick={() => void save()}>{busy ? "保存中…" : "确认保存"}</button></div></section>}
    </>}
  </dialog>;
}

function ManagementAudit({ onDenied }: { onDenied: () => void }) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<LoadState<{ items: AuditEvent[]; next_cursor: number | null }>>(emptyLoad);
  const loader = useRef<ManagementLoader<{ items: AuditEvent[]; next_cursor: number | null }> | null>(null);
  const cursors = useRef<Array<number | null>>([null]);
  useEffect(() => {
    if (!open) return;
    const current = new ManagementLoader<{ items: AuditEvent[]; next_cursor: number | null }>(next => { setState(next); if (next.denied) onDenied(); }); loader.current = current;
    cursors.current = [null]; void current.load("/api/admin/audit-events");
    return () => { current.dispose(); loader.current = null; };
  }, [open]);
  const load = () => { const cursor = cursors.current.at(-1); void loader.current?.load(`/api/admin/audit-events${cursor == null ? "" : `?cursor=${cursor}`}`); };
  return <section className="management-audit"><button type="button" className="btn btn-ghost" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "收起" : "查看"}管理记录</button>
    {open && <><h3>管理记录</h3><p className="field-hint">只显示你有权查看的账号管理操作，不包含私人研究。</p><LoadNotice state={state} />{state.data && <><ul>{state.data.items.map(event => <li key={event.id}><time dateTime={event.created_at}>{new Date(event.created_at).toLocaleString()}</time> · 操作者 {event.actor_id ?? "服务器维护者"} · 目标 {event.target_id ?? "平台"} · {event.action}{event.reason ? ` · ${event.reason}` : ""}</li>)}</ul>{!state.data.items.length && <p className="field-hint">暂无管理记录。</p>}<div className="management-pagination"><button type="button" className="btn btn-sm" disabled={cursors.current.length === 1 || state.loading} onClick={() => { cursors.current.pop(); load(); }}>较新记录</button><button type="button" className="btn btn-sm" disabled={state.data.next_cursor === null || state.loading} onClick={() => { if (state.data?.next_cursor != null) { cursors.current.push(state.data.next_cursor); load(); } }}>较早记录</button></div></>}</>}
  </section>;
}
