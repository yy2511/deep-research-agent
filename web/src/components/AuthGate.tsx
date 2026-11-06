import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import {
  fetchMe, login, logout, onUnauthorized, register, validateAuthForm,
  PASSWORD_MAX, PASSWORD_MIN, type Account, type AuthFields, type AuthMode,
} from "../auth";
import { Icon } from "./Icon";

type GateState =
  | { kind: "checking" }
  | { kind: "anonymous"; notice?: string }
  | { kind: "error"; message: string }
  | { kind: "ready"; account: Account };

/**
 * 进入工作台前确认登录。已登录才渲染工作台；任何请求 401 时回到登录页。
 * 研究在服务端继续进行，重新登录后可以接着查看。
 */
export function AuthGate({ children }: { children: (account: Account, onLogout: () => void) => ReactNode }) {
  const [state, setState] = useState<GateState>({ kind: "checking" });

  const check = useCallback(async () => {
    setState({ kind: "checking" });
    try {
      const account = await fetchMe();
      setState(account ? { kind: "ready", account } : { kind: "anonymous" });
    } catch (e) {
      setState({ kind: "error", message: e instanceof Error ? e.message : "无法确认登录状态" });
    }
  }, []);

  useEffect(() => { void check(); }, [check]);
  useEffect(() => onUnauthorized(() => setState((s) =>
    s.kind === "ready" ? { kind: "anonymous", notice: "登录已过期，请重新登录。进行中的研究不受影响。" } : s)), []);

  const handleLogout = useCallback(() => {
    void logout().catch(() => undefined).finally(() => setState({ kind: "anonymous" }));
  }, []);

  if (state.kind === "ready") return <>{children(state.account, handleLogout)}</>;
  if (state.kind === "checking") return <div className="auth-shell"><Icon name="loading" className="icon-spin" size={20} /></div>;
  if (state.kind === "error") {
    return (
      <div className="auth-shell">
        <div className="auth-card">
          <p className="field-hint is-bad">{state.message}</p>
          <button className="btn" onClick={() => void check()}>重试</button>
        </div>
      </div>
    );
  }
  return <AuthPage notice={state.notice} onAuthed={(account) => setState({ kind: "ready", account })} />;
}

const EMPTY: AuthFields = { email: "", password: "", confirm: "", invite: "" };

export function AuthPage({ onAuthed, notice, initialMode = "login" }: {
  onAuthed: (account: Account) => void; notice?: string; initialMode?: AuthMode;
}) {
  const [mode, setMode] = useState<AuthMode>(initialMode);
  const [fields, setFields] = useState<AuthFields>(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (key: keyof AuthFields) => (e: { target: { value: string } }) =>
    setFields((f) => ({ ...f, [key]: e.target.value }));

  function switchMode(next: AuthMode) {
    setMode(next);
    setError(null);
    setFields((f) => ({ ...f, password: "", confirm: "" }));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    const problem = validateAuthForm(mode, fields);
    if (problem) { setError(problem); return; }
    setBusy(true);
    setError(null);
    try {
      const account = mode === "login"
        ? await login(fields.email, fields.password)
        : await register(fields.invite, fields.email, fields.password);
      onAuthed(account);
    } catch (err) {
      setError(err instanceof Error ? err.message : "请求失败");
    } finally {
      setBusy(false);
    }
  }

  const registering = mode === "register";
  return (
    <div className="auth-shell">
      <form className="auth-card" onSubmit={(e) => void submit(e)} noValidate>
        <div className="brand auth-brand">
          <span className="brand-mark"><Icon name="search" size={16} strokeWidth={2.2} /></span>
          <span className="brand-text">研究工作台</span>
        </div>
        <h1 className="auth-title">{registering ? "用邀请码注册" : "登录"}</h1>
        {notice && <p className="auth-notice">{notice}</p>}
        {registering && (
          <label className="field">
            <span className="field-label">邀请码</span>
            <input className="input" name="invite" autoComplete="off" value={fields.invite} onChange={set("invite")} />
          </label>
        )}
        <label className="field">
          <span className="field-label">邮箱</span>
          <input className="input" type="email" name="email" autoComplete="email" value={fields.email} onChange={set("email")} />
        </label>
        <label className="field">
          <span className="field-label">密码</span>
          <input className="input" type="password" name="password" maxLength={PASSWORD_MAX}
            autoComplete={registering ? "new-password" : "current-password"} value={fields.password} onChange={set("password")} />
          {registering && <span className="field-hint">{PASSWORD_MIN}–{PASSWORD_MAX} 个字符</span>}
        </label>
        {registering && (
          <label className="field">
            <span className="field-label">确认密码</span>
            <input className="input" type="password" name="confirm" maxLength={PASSWORD_MAX}
              autoComplete="new-password" value={fields.confirm} onChange={set("confirm")} />
          </label>
        )}
        {error && <p className="field-hint is-bad" role="alert">{error}</p>}
        <button className="btn btn-primary auth-submit" type="submit" disabled={busy}>
          {busy ? "请稍候…" : registering ? "注册并登录" : "登录"}
        </button>
        <p className="auth-switch">
          {registering ? "已有账号？" : "收到邀请码？"}
          <button type="button" className="link-btn" onClick={() => switchMode(registering ? "login" : "register")}>
            {registering ? "去登录" : "注册账号"}
          </button>
        </p>
        {!registering && <p className="field-hint">忘记密码请联系管理员重设。</p>}
      </form>
    </div>
  );
}
