import { useEffect, useRef, useState } from "react";
import type { Account } from "../auth";
import { notifyUnauthorized } from "../session";
import { Icon } from "./Icon";
import { emptyLoad, ManagementError, ManagementLoader, managementRequest, managementWrite, ROLE_LABELS, type Governance, type ManagedUser } from "../userManagement";

type OwnerState = Governance & { reauth_methods: Array<"password" | "github">; reauth_ttl_seconds: number };
type ProofStatus = { ready: boolean; target_id?: number; expires_at?: string };
type TimezoneState = { timezone: string; pending_timezone: string | null; effective_at: string | null; version: number };

export function OwnerManagement({ account }: { account: Account }) {
  const [governance, setGovernance] = useState<OwnerState | null>(null);
  const [actor, setActor] = useState<ManagedUser | null>(null);
  const [target, setTarget] = useState<ManagedUser | null>(null);
  const [targetId, setTargetId] = useState(""); const [password, setPassword] = useState("");
  const [proof, setProof] = useState<ProofStatus>({ ready: false });
  const [error, setError] = useState(""); const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false); const [confirmed, setConfirmed] = useState(false);
  const [denied, setDenied] = useState(false);
  const pending = useRef(false); const active = useRef(true); const targetGeneration = useRef(0);
  useEffect(() => {
    active.current = true; const controller = new AbortController();
    void Promise.all([
      managementRequest<OwnerState>("/api/admin/governance", { signal: controller.signal }),
      managementRequest<ManagedUser>(`/api/admin/users/${account.id}`, { signal: controller.signal }),
      managementRequest<ProofStatus>("/api/admin/owner-transfer/status", { signal: controller.signal }),
    ]).then(async ([nextGovernance, nextActor, nextProof]) => {
      if (!active.current || controller.signal.aborted) return;
      setGovernance(nextGovernance); setActor(nextActor);
      if (nextProof.ready && nextProof.target_id) {
        const nextTarget = await managementRequest<ManagedUser>(`/api/admin/users/${nextProof.target_id}`, { signal: controller.signal });
        if (!active.current || controller.signal.aborted) return;
        setTarget(nextTarget); setTargetId(String(nextTarget.id)); setProof(nextProof);
      }
    }).catch(failure => { if (active.current && !controller.signal.aborted) handleError(failure); })
      .finally(() => { if (active.current && !controller.signal.aborted) setLoading(false); });
    return () => { active.current = false; controller.abort(); ++targetGeneration.current; };
  }, [account.id]);
  function handleError(failure: unknown) {
    setError(failure instanceof Error ? failure.message : "操作失败，请重新核对状态。");
    if (failure instanceof ManagementError && [401, 403].includes(failure.status)) {
      setDenied(true); setActor(null); setTarget(null); setGovernance(null); setProof({ ready: false });
      if (failure.status === 401) notifyUnauthorized();
    }
    if (failure instanceof ManagementError && failure.status === 409) { setTarget(null); setProof({ ready: false }); setError("身份或负责人状态已改变。请刷新页面后重新核对，未自动重试。"); }
  }
  async function findTarget() {
    if (pending.current || denied || loading) return;
    if (!/^\d+$/.test(targetId) || !Number.isSafeInteger(Number(targetId))) { setError("请填写有效的账号 ID。"); return; }
    pending.current = true; setBusy(true); setError(""); setProof({ ready: false }); setConfirmed(false); setTarget(null);
    const sequence = ++targetGeneration.current;
    try {
      const next = await managementRequest<ManagedUser>(`/api/admin/users/${targetId}`);
      if (!active.current || sequence !== targetGeneration.current) return;
      if (next.id === account.id || next.role !== "admin" || next.status !== "active") throw new Error("移交目标必须是另一个正常状态的平台管理员。");
      setTarget(next);
    } catch (failure) { if (active.current && sequence === targetGeneration.current) handleError(failure); }
    finally { pending.current = false; if (active.current) setBusy(false); }
  }
  async function verify(method: "password" | "github") {
    if (pending.current || !governance || !actor || !target || denied || loading) return;
    pending.current = true; setBusy(true); setError(""); setConfirmed(false);
    const body = { target_id: target.id, expected_governance_version: governance.version, expected_actor_version: actor.management_version, expected_target_version: target.management_version };
    try {
      if (method === "password") {
        const next = await managementWrite<{ target_id: number; expires_at: string }>("/api/admin/owner-transfer/verify", "POST", { ...body, password });
        if (active.current) { setProof({ ready: true, ...next }); setPassword(""); }
      } else {
        const next = await managementWrite<{ authorize_url: string }>("/api/admin/owner-transfer/github", "POST", body);
        if (active.current) {
          const url = new URL(next.authorize_url);
          if (url.protocol !== "https:" || url.hostname !== "github.com" || url.pathname !== "/login/oauth/authorize") throw new Error("身份验证地址无效，请联系部署维护者。");
          window.location.assign(url.href);
        }
      }
    } catch (failure) { if (active.current) { setPassword(""); handleError(failure); } }
    finally { pending.current = false; if (active.current) setBusy(false); }
  }
  async function transfer() {
    if (!proof.ready || !confirmed || pending.current || denied || loading) return;
    pending.current = true; setBusy(true); setError("");
    try {
      await managementWrite("/api/admin/owner-transfer", "POST", {});
      if (active.current) { setProof({ ready: false }); setTarget(null); setGovernance(null); setActor(null); notifyUnauthorized(); }
    } catch (failure) { if (active.current) { setProof({ ready: false }); setConfirmed(false); handleError(failure); } }
    finally { pending.current = false; if (active.current) setBusy(false); }
  }
  const transferStep = proof.ready ? 3 : target ? 2 : 1;
  return <div className="settings-section owner-management"><div className="section-head management-page-heading"><div><p className="management-eyebrow">PLATFORM / GOVERNANCE</p><h2>负责人管理</h2><p className="field-hint">明确平台的最终管理权，让每一次移交都有据可查。</p></div></div>
    {loading && <p role="status">正在确认负责人状态…</p>}{error && <p role="alert" className="field-hint is-bad">{error}</p>}
    {!denied && governance && <>
      <section className="owner-current"><span className="owner-current-icon"><Icon name="verify" size={24} /></span><div><h3>当前负责人</h3>{governance.owner_user_id === null ? <p>尚未设置，请联系部署维护者。</p> : <><strong>{actor?.id === governance.owner_user_id ? actor.display_name || actor.email || `用户 ${actor.id}` : `用户 ${governance.owner_user_id}`}</strong><p>账号 ID {governance.owner_user_id}</p></>}</div><span className="management-role is-owner">唯一负责人</span></section>
      {governance.owner_user_id === account.id && <section className="owner-transfer-section"><div className="owner-section-heading"><h3>移交平台负责人</h3><p className="field-hint">平台只有一位负责人。验证身份后还需明确确认，移交不会自动发生。</p></div>
        <ol className="owner-transfer-steps" aria-label="负责人移交流程">
          {["核对账号", "验证身份", "确认移交"].map((label, index) => <li key={label} className={index + 1 < transferStep ? "is-complete" : index + 1 === transferStep ? "is-current" : ""} aria-current={index + 1 === transferStep ? "step" : undefined}><span>{index + 1 < transferStep ? <Icon name="check" size={13} /> : index + 1}</span><strong>{label}</strong></li>)}
        </ol>
        <div className="owner-transfer-content"><div className="owner-step-heading"><span>01</span><div><h4>选择新的负责人</h4><p className="field-hint">目标必须是另一个正常状态的平台管理员。</p></div></div>
          <form className="management-filters owner-target-form" onSubmit={event => { event.preventDefault(); void findTarget(); }}>
            <label>新负责人账号 ID<input className="input" inputMode="numeric" value={targetId} disabled={busy || proof.ready || loading} placeholder="输入账号 ID" onChange={event => { setTargetId(event.target.value); setTarget(null); setProof({ ready: false }); setConfirmed(false); }} /></label>
            <button type="submit" className="btn btn-sm" disabled={busy || proof.ready || loading}>核对账号<Icon name="arrow-right" size={14} /></button>
          </form>
          {target && <div className="management-transfer-target"><div className="owner-target-identity"><span className="management-avatar is-admin" aria-hidden="true">{(target.display_name || target.email || "用户").slice(0, 1).toUpperCase()}</span><div><strong>{target.display_name || target.email || `用户 ${target.id}`}</strong><p>ID {target.id} · {ROLE_LABELS[target.role]} · 正常</p></div><Icon name="check-circle" size={18} /></div>
            {!proof.ready && <section className="owner-verification"><div className="owner-step-heading"><span>02</span><div><h4>重新验证当前身份</h4><p className="field-hint">请先重新验证你当前的负责人身份。验证有效期 {Math.ceil(governance.reauth_ttl_seconds / 60)} 分钟。</p></div></div>
              {governance.reauth_methods.includes("password") && <form onSubmit={event => { event.preventDefault(); void verify("password"); }}><label>当前账号密码<input className="input" type="password" autoComplete="current-password" value={password} disabled={busy} onChange={event => setPassword(event.target.value)} /></label><button type="submit" className="btn btn-sm btn-primary" disabled={busy || !password}>验证密码</button></form>}
              {governance.reauth_methods.includes("github") && <button type="button" className="btn btn-sm" disabled={busy} onClick={() => void verify("github")}>使用同一 GitHub 身份验证</button>}
            </section>}
            {proof.ready && <section className="management-confirm owner-transfer-confirm"><p className="management-confirm-label"><Icon name="check-circle" size={15} />身份验证已完成</p><h4>最后确认</h4><p>将唯一负责人身份移交给 ID {target.id}。验证有效至 {proof.expires_at ? new Date(proof.expires_at).toLocaleString() : "服务器规定时间"}。</p>
              <label className="management-check"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={busy} />我已核对目标，确认移交负责人身份</label>
              <div className="management-actions"><button type="button" className="btn btn-ghost" disabled={busy} onClick={() => { setProof({ ready: false }); setTarget(null); setConfirmed(false); setPassword(""); }}>取消移交</button><button type="button" className="btn btn-primary" disabled={busy || !confirmed} onClick={() => void transfer()}>{busy ? "提交中…" : "确认移交并退出登录"}</button></div>
            </section>}
          </div>}
        </div>
        <p className="owner-transfer-footnote"><Icon name="info" size={15} /><span>完成后，你将成为管理员，双方需重新登录；研究归属不变。</span></p>
      </section>}
      <QuotaTimezone onDenied={() => handleError(new ManagementError("管理权限已失效，请重新登录。", 403))} />
    </>}
  </div>;
}

export function QuotaTimezone({ onDenied }: { onDenied?: () => void } = {}) {
  const [state, setState] = useState(emptyLoad<TimezoneState>);
  const [timezone, setTimezone] = useState(""); const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false); const [error, setError] = useState(""); const [notice, setNotice] = useState("");
  const loader = useRef<ManagementLoader<TimezoneState> | null>(null); const saving = useRef(false); const active = useRef(true);
  useEffect(() => {
    active.current = true; const current = new ManagementLoader<TimezoneState>(next => { setState(next); if (next.denied) onDenied?.(); if (next.data) setTimezone(next.data.pending_timezone ?? next.data.timezone); }); loader.current = current;
    void current.load("/api/admin/quota-timezone"); return () => { active.current = false; current.dispose(); };
  }, []);
  async function save() {
    if (saving.current || !state.data || !confirm) return;
    saving.current = true; setBusy(true); setError(""); setNotice("");
    try {
      await managementWrite("/api/admin/quota-timezone", "PUT", { timezone: timezone.trim(), expected_version: state.data.version });
      if (active.current) { setConfirm(false); setNotice("时区计划已保存，请核对下方实际生效时间。"); void loader.current?.load("/api/admin/quota-timezone"); }
    } catch (failure) {
      if (active.current) {
        setConfirm(false); setError(failure instanceof Error ? failure.message : "保存失败。");
        if (failure instanceof ManagementError && [401, 403].includes(failure.status)) { setState({ data: null, loading: false, denied: true, error: failure.message }); loader.current?.dispose(); onDenied?.(); if (failure.status === 401) notifyUnauthorized(); }
        else if (failure instanceof ManagementError && failure.status === 409) { setError("时区计划已改变，已重新加载，请再次核对。"); void loader.current?.load("/api/admin/quota-timezone"); }
      }
    } finally { saving.current = false; if (active.current) setBusy(false); }
  }
  return <section className="owner-timezone-section"><div className="owner-section-heading"><h3><Icon name="globe" size={17} />免费额度日界时区</h3><p className="field-hint">影响访客和普通用户每日额度。新时区在当前时区的下一个日界生效，不会立即刷新次数。</p></div>
    {state.loading && <p role="status">读取时区中…</p>}{(error || state.error) && <p role="alert" className="field-hint is-bad">{error || state.error}</p>}{notice && <p role="status" className="notice">{notice}</p>}
    {state.data && <><div className="owner-timezone-current"><span>当前日界时区</span><strong>{state.data.timezone}</strong></div>{state.data.pending_timezone && <p className="notice">待生效：{state.data.pending_timezone}，时间：{state.data.effective_at ? new Date(state.data.effective_at).toLocaleString() : "待确认"}</p>}
      <label className="owner-timezone-input">新时区<input className="input" value={timezone} disabled={busy} placeholder="例如 Asia/Shanghai" onChange={event => { setTimezone(event.target.value); setConfirm(false); }} /></label>
      <p className="field-hint">填写当前时区可取消尚未生效的切换。</p>
      <label className="management-check"><input type="checkbox" checked={confirm} disabled={busy} onChange={event => setConfirm(event.target.checked)} />确认此修改影响全平台免费额度的日界</label>
      <button type="button" className="btn btn-sm" disabled={busy || !confirm || !timezone.trim()} onClick={() => void save()}>{busy ? "保存中…" : "保存时区计划"}</button></>}
  </section>;
}
