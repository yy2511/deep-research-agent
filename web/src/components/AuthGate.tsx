import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import {
  fetchAuthOptions, fetchMe, login, loginErrorFrom, logout, onUnauthorized, register, validateAuthForm,
  GITHUB_LOGIN_URL, PASSWORD_MAX, PASSWORD_MIN, type Account, type AuthFields, type AuthMode,
} from "../auth";
import { Icon } from "./Icon";
import { fetchShowcases, type Showcase } from "../showcases";

type GateState =
  | { kind: "checking" }
  | { kind: "anonymous"; notice?: string; github?: boolean; showcases?: Showcase[] }
  | { kind: "error"; message: string }
  | { kind: "ready"; account: Account };

/**
 * 进入工作台前确认登录。已登录才渲染工作台；任何请求 401 时回到登录页。
 * 研究在服务端继续进行，重新登录后可以接着查看。
 *
 * 未登录且有示例时（#47），先给看示例（renderPublic），点“登录”再到登录页；登录过期时直接到登录页。
 */
export function AuthGate({ children, renderPublic }: {
  children: (account: Account, onLogout: () => void) => ReactNode;
  renderPublic?: (showcases: Showcase[], onLogin: () => void) => ReactNode;
}) {
  const [state, setState] = useState<GateState>({ kind: "checking" });
  const [wantsLogin, setWantsLogin] = useState(false);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  // GitHub 登录失败跳回来时带着 ?login_error=…：读出说明后从地址栏去掉，刷新不再重复显示。
  const [githubError] = useState(() => {
    const message = loginErrorFrom(window.location.search);
    if (message !== null) {
      const url = new URL(window.location.href);
      url.searchParams.delete("login_error");
      window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
    }
    return message;
  });

  const check = useCallback(async () => {
    setState({ kind: "checking" });
    try {
      const account = await fetchMe();
      if (account) { setState({ kind: "ready", account }); return; }
      const [options, showcases] = await Promise.all([fetchAuthOptions(), fetchShowcases()]);
      setState({ kind: "anonymous", github: options.github, showcases });
    } catch (e) {
      setState({ kind: "error", message: e instanceof Error ? e.message : "无法确认登录状态" });
    }
  }, []);

  useEffect(() => { void check(); }, [check]);
  useEffect(() => onUnauthorized(() => setState((s) =>
    s.kind === "ready" ? { kind: "anonymous", notice: "登录已过期，请重新登录。进行中的研究不受影响。" } : s)), []);
  // 登录过期、退出后回到登录页时也要知道有没有 GitHub 登录。
  const anonymous = state.kind === "anonymous" && state.github === undefined;
  useEffect(() => {
    if (!anonymous) return;
    let live = true;
    void Promise.all([fetchAuthOptions(), fetchShowcases()]).then(([o, showcases]) => {
      if (live) setState((s) => (s.kind === "anonymous" ? { ...s, github: o.github, showcases } : s));
    });
    return () => { live = false; };
  }, [anonymous]);

  // 只有服务端确认退出才回到登录页；失败时保持登录并提示重试（#34 审查）。
  const handleLogout = useCallback(() => {
    setLogoutError(null);
    logout().then(
      () => { setWantsLogin(true); setState({ kind: "anonymous" }); },
      (e) => setLogoutError(e instanceof Error ? e.message : "请求失败"),
    );
  }, []);

  if (state.kind === "ready") {
    return (
      <>
        {logoutError && <LogoutError message={logoutError} onRetry={handleLogout} onDismiss={() => setLogoutError(null)} />}
        {children(state.account, handleLogout)}
      </>
    );
  }
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
  const showcases = state.showcases ?? [];
  const showPublic = renderPublic && showcases.length > 0 && !wantsLogin && !state.notice && !githubError;
  if (showPublic) return <>{renderPublic(showcases, () => setWantsLogin(true))}</>;
  return <AuthPage notice={state.notice} github={state.github === true} initialError={githubError}
    onBackToShowcases={renderPublic && showcases.length > 0 ? () => setWantsLogin(false) : undefined}
    onAuthed={(account) => { setWantsLogin(false); setState({ kind: "ready", account }); }} />;
}

export function LogoutError({ message, onRetry, onDismiss }: { message: string; onRetry: () => void; onDismiss: () => void }) {
  return (
    <div className="auth-toast" role="alert">
      <Icon name="warning" size={15} />
      <span>退出登录失败（{message}），当前仍处于登录状态。</span>
      <button className="link-btn" onClick={onRetry}>重试</button>
      <button className="icon-btn" aria-label="关闭" onClick={onDismiss}><Icon name="close" size={14} /></button>
    </div>
  );
}

const EMPTY: AuthFields = { email: "", password: "", confirm: "", invite: "" };

export function AuthPage({ onAuthed, notice, initialMode = "login", github = false, initialError = null, onBackToShowcases }: {
  onAuthed: (account: Account) => void; notice?: string; initialMode?: AuthMode;
  /** 服务端配置了 GitHub 登录时显示按钮（阶段 2，#45）。 */
  github?: boolean; initialError?: string | null;
  /** 有示例时（#47），登录页可以回去继续看示例。 */
  onBackToShowcases?: () => void;
}) {
  const [mode, setMode] = useState<AuthMode>(initialMode);
  const [fields, setFields] = useState<AuthFields>(EMPTY);
  const [error, setError] = useState<string | null>(initialError);
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
        {github && !registering && (
          <>
            {/* 整页跳转：GitHub 授权后由服务端设置登录 Cookie 并跳回首页。 */}
            <a className="btn auth-submit auth-github" href={GITHUB_LOGIN_URL}>用 GitHub 登录</a>
            <p className="auth-divider"><span>或用邮箱登录</span></p>
          </>
        )}
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
        {onBackToShowcases && <button type="button" className="link-btn" onClick={onBackToShowcases}>先看看示例</button>}
      </form>
    </div>
  );
}
