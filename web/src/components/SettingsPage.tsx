import { useEffect, useState } from "react";
import { getJSON, postJSON, putJSON } from "../api";
import type { ExecutionEffective, RetryPolicy } from "../capabilities";
import { effortOptions, modelSettingsError, selectModel, type ProviderStatus } from "../providerSettings";
import { CapabilitySettings } from "./CapabilitySettings";
import { Icon } from "./Icon";
import { McpManager } from "./McpPanel";
import { ModelCapabilities } from "./ModelCapabilities";
import { ModelField } from "./ModelField";
import { ProviderConnections } from "./ProviderConnections";
import { Select } from "./Select";
import { SkillsPanel } from "./SkillsPanel";
import { SettingRow, Switch } from "./ui";

interface NodeConfig { model: string; provider: string; reasoning?: boolean; effort: string | null }
export interface SimpleBudget {
  max_llm_calls: number | null;
  max_tool_calls: number | null;
  wall_timeout_s: number | null;
  reserve_wrapup_calls: number;
  storage_max_chars: number;
}
export interface ConfigPayload {
  browser?: {enabled:boolean;mode?:"isolated"|"extension";extension_browser?:"chrome"|"msedge";profile_dir_name?:string|null};
  vision?: {enabled:boolean;mode:"auto"|"force_auxiliary";provider:string;model:string};
  schema_version?: number;
  call_limits_enabled?: boolean;
  evidence_judge?: {enabled:boolean};
  evidence_judge_status?: {key_configured:boolean;model:string;concurrency:number;batch_timeout_s:number};
  wall_timeout_s?: number | null;
  wrapup_reserve_s?: number;
  summary_timeout_s?: number;
  summary_max_retries?: number;
  compaction_enabled?: boolean;
  context_window_tokens?: number | null;
  context_window_mode?: "manual" | "auto";
  max_output_tokens?: number;
  summary_max_output_tokens?: number | null;
  wrapup_max_output_tokens?: number | null;
  planner: { model: string; provider: string; effort: string | null };
  subagent: NodeConfig;
  /** 最小闭环的真实预算摘要（服务端 SimpleConfig），启动前确认投入范围用。 */
  simple_budget?: SimpleBudget;
  simple_model?: { model: string; provider: string; reasoning?: boolean };
  /** 可写：null 表示跟随本机默认（不改变已有执行权限）。 */
  execution_enabled?: boolean | null;
  execution_backend?: "docker" | "native" | null;
  /** 只读：开关的实际取值与来源。 */
  execution_effective?: ExecutionEffective;
  /** 只读：服务端实际使用的重试次数。 */
  retry_policy?: RetryPolicy;
}

type NodeKey = "planner" | "subagent";
type TestState = "idle" | "testing" | "ok" | "fail";

export const SETTINGS_NODES: Array<{ key: NodeKey; label: string; hint: string; hasReasoning: boolean }> = [
  { key: "planner", label: "规划", hint: "澄清问题、制定研究计划；思考强度只用于制定计划", hasReasoning: false },
  { key: "subagent", label: "研究与写作", hint: "主 Agent 和子任务：检索、阅读、写回答", hasReasoning: true },
];

export function settingsUpdate(config: ConfigPayload) {
  // GET 包含只读运行摘要；只提交 RuntimeModelSettings 可写字段，避免响应扩展破坏保存。
  const { vision, schema_version, planner, subagent, call_limits_enabled, compaction_enabled, context_window_tokens, context_window_mode, max_output_tokens, summary_max_output_tokens, wrapup_max_output_tokens, evidence_judge, wall_timeout_s, wrapup_reserve_s, summary_timeout_s, summary_max_retries } = config;
  // 旧服务端没有这个字段时不提交，由服务端沿用当前值。
  const execution = "execution_enabled" in config ? { execution_enabled: config.execution_enabled ?? null } : {};
  return { ...("browser" in config ? {browser: config.browser} : {}), vision, schema_version, planner, subagent, call_limits_enabled, compaction_enabled, context_window_tokens, context_window_mode, max_output_tokens, summary_max_output_tokens, wrapup_max_output_tokens, evidence_judge, wall_timeout_s, wrapup_reserve_s, summary_timeout_s, summary_max_retries, ...execution, ...("execution_backend" in config ? {execution_backend: config.execution_backend ?? null} : {}) };
}

/** 本机偏好：只存在浏览器里，不影响后端配置。 */
export interface LocalPrefs { showRecords: boolean; showReplay: boolean; traceFull: boolean }

export type SettingsTab = "models" | "capabilities" | "connections" | "skills" | "mcp" | "general";
const TABS: Array<[SettingsTab, string]> = [
  ["models", "模型"], ["capabilities", "研究能力"], ["connections", "API 连接"], ["skills", "技能"], ["mcp", "外部资料（MCP）"], ["general", "通用"],
];

/** 模型页与能力页共用的一份草稿与保存状态，由 SettingsPage 持有；两页之间切换不丢未保存的修改。 */
export function useRuntimeSettings(onSaved?: (c: ConfigPayload) => void) {
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  const [savedJson, setSavedJson] = useState("");
  const [saved, setSaved] = useState<ConfigPayload | null>(null);
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const [revision, setRevision] = useState(0);

  function load() {
    Promise.all([getJSON<ConfigPayload>("/api/config"), getJSON<ProviderStatus[]>("/api/providers")])
      .then(([settings, connections]) => {
        setConfig(settings); setSaved(settings); setSavedJson(JSON.stringify(settingsUpdate(settings))); setProviders(connections); setError(null);
      })
      .catch(e => setError(e instanceof Error ? e.message : "读取设置失败"));
  }
  useEffect(load, []);
  /** 只刷新连接列表（「API 连接」页可能改过），不动草稿。 */
  function reloadProviders() {
    getJSON<ProviderStatus[]>("/api/providers").then(setProviders).catch(e => setError(e instanceof Error ? e.message : "读取连接失败"));
  }
  const dirty = !!config && JSON.stringify(settingsUpdate(config)) !== savedJson;

  async function save() {
    if (!config) return;
    const invalid = modelSettingsError(config, providers);
    if (invalid) { setError(invalid); return; }
    setSaving(true);
    setError(null);
    try {
      const next = await putJSON<ConfigPayload>("/api/config", settingsUpdate(config));
      setConfig(next); setSaved(next); setSavedJson(JSON.stringify(settingsUpdate(next)));
      setNotice("已保存。之后开始的研究使用新设置，进行中的研究保持开始时的设置。");
      setRevision(r => r + 1);
      onSaved?.(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }
  return { config, setConfig, saved, providers, setProviders, reloadProviders, saving, error, setError, notice, setNotice, dirty, load, save, revision };
}
export type RuntimeSettings = ReturnType<typeof useRuntimeSettings>;

function SettingsFooter({ s }: { s: RuntimeSettings }) {
  return <footer className="settings-footer">
    <span className={`field-hint${s.dirty ? " is-warn" : ""}`}>{s.dirty ? "有未保存的修改" : ""}</span>
    <button className="btn btn-ghost" disabled={!s.dirty || s.saving} onClick={s.load}>放弃修改</button>
    <button className="btn btn-primary" onClick={() => void s.save()} disabled={s.saving || !s.dirty}>{s.saving ? "保存中…" : "保存"}</button>
  </footer>;
}

function SettingsLoading({ error }: { error: string | null }) {
  return <div className="settings-section"><p className={error ? "field-hint is-bad" : "field-hint"}>{error || "正在读取设置…"}</p></div>;
}

function ModelSettings({ s }: { s: RuntimeSettings }) {
  const { config, setConfig, providers, setProviders, error, setNotice, notice } = s;
  const [testState, setTestState] = useState<Record<string, { state: TestState; error?: string }>>({});
  const [refreshing, setRefreshing] = useState<string | null>(null);

  if (!config) return <SettingsLoading error={error} />;

  function updateNode(node: NodeKey, patch: Partial<NodeConfig>) {
    setConfig((c) => (c ? ({ ...c, [node]: { ...c[node], ...patch } } as ConfigPayload) : c));
    setTestState(s => ({ ...s, [node]: { state: "idle" } }));
  }
  function changeModel(node: NodeKey, provider: ProviderStatus | undefined, model: string) {
    setConfig(c => c ? selectModel(c, node, provider, model) : c);
    setTestState(s => ({ ...s, [node]: { state: "idle" } }));
  }
  async function test(node: NodeKey) {
    if (!config) return;
    setTestState((s) => ({ ...s, [node]: { state: "testing" } }));
    const n = config[node];
    const body = { model: n.model, provider: n.provider, reasoning: "reasoning" in n ? !!n.reasoning : true, effort: n.effort };
    const started = Date.now();
    try {
      const res = await postJSON<{ ok: boolean; error?: string }>("/api/config/test", body);
      setTestState((s) => ({ ...s, [node]: { state: res.ok ? "ok" : "fail", error: res.ok ? `${((Date.now() - started) / 1000).toFixed(1)} 秒` : res.error } }));
    } catch (e) { setTestState(s => ({ ...s, [node]: { state: "fail", error: e instanceof Error ? e.message : "测试失败" } })); }
  }
  async function refreshProvider(id: string) {
    setRefreshing(id); setNotice("");
    try {
      const [result, catalog] = await Promise.allSettled([
        postJSON<ProviderStatus>(`/api/providers/${id}/refresh`, {}),
        postJSON<{ ok: boolean; error?: string }>("/api/model-catalog/refresh", {}),
      ]);
      setProviders(await getJSON<ProviderStatus[]>("/api/providers"));
      if (result.status === "rejected") throw result.reason;
      const catalogError = catalog.status === "fulfilled" ? catalog.value.error : "模型目录暂时不可用，可以手动补充资料。";
      setNotice([result.value.reachable ? `已获取 ${result.value.models.length} 个模型。` : result.value.error, catalogError].filter(Boolean).join(" "));
    } catch (e) {
      setNotice(e instanceof Error ? e.message : "获取失败，保留已有模型列表。");
    } finally {
      setRefreshing(null);
    }
  }
  // 校验错误原文带字段前缀（如 "subagent.effort=..."），只看前半句人类可读部分来定位出错的卡片。
  const primaryMessage = error?.split("[type=")[0].toLowerCase() ?? "";

  return <>
    <div className="settings-section">
      <div className="section-head"><div><h2>模型</h2><p>给两个环节分别选模型。看图、运行代码、长对话整理等在「研究能力」里设置。修改只影响之后开始的研究。</p></div></div>
      {notice && <p className="notice" role="status">{notice}</p>}
      {SETTINGS_NODES.map(({ key, label, hint, hasReasoning }) => {
        const node = config[key];
        const provider = providers.find((p) => p.name === node.provider);
        const info = provider?.model_details?.find(m => m.id === node.model);
        const ts = testState[key];
        const nodeError = primaryMessage.includes(key) ? error : null;
        const reasoningOff = hasReasoning && !(node as NodeConfig).reasoning;
        return (
          <section className={`card model-card${nodeError ? " is-error" : ""}`} key={key}>
            <div className="card-head"><strong>{label}</strong><small>{hint}</small></div>
            <div className="field-grid field-grid-3">
              <div className="field"><span className="field-label">连接</span>
                <Select ariaLabel={`${label} API 连接`} value={node.provider}
                  onChange={(v) => changeModel(key, providers.find(p => p.name === v), "")}
                  options={providers.map((p) => ({
                    value: p.name, label: p.label || p.name, disabled: !p.key_configured,
                    hint: p.key_configured ? (p.checked_at && !p.reachable ? "获取失败" : "") : "无 Key",
                  }))} />
              </div>
              <div className="field"><span className="field-label">模型</span>
                <ModelField value={node.model} models={provider?.models ?? []} label={`${label}模型 ID`} onChange={(v) => changeModel(key, provider, v)} />
              </div>
              <div className="field"><span className="field-label">思考强度</span>
                {info?.reasoning_efforts == null
                  ? <input className="input" aria-label={`${label}自定义思考档位`} disabled={reasoningOff} value={node.effort ?? ""}
                      placeholder="服务商默认" maxLength={32} onChange={e => updateNode(key, { effort: e.target.value.trim() || null })} />
                  : <Select ariaLabel={`${label}思考强度`} value={node.effort ?? ""} disabled={reasoningOff}
                      onChange={(v) => updateNode(key, { effort: v || null })} options={effortOptions(info, node.effort)} />}
              </div>
            </div>
            {hasReasoning && <label className="check-line"><input type="checkbox" checked={!!(node as NodeConfig).reasoning}
              onChange={(e) => updateNode(key, { reasoning: e.target.checked, ...(!e.target.checked ? { effort: null } : {}) })} />开启深度思考</label>}
            {info?.reasoning_efforts == null && <p className="field-hint">这个服务没有提供思考档位，默认不发送；如果手填，请以服务商说明为准。</p>}
            <div className="card-foot">
              {provider && node.model.trim()
                ? <ModelCapabilities key={`${key}:${provider.name}:${node.model}`} provider={provider} model={node.model} info={info}
                    onChange={p => setProviders(items => items.map(item => item.name === p.name ? p : item))}
                    onUseContext={key === "subagent" ? tokens => setConfig(c => c ? { ...c, context_window_mode: "manual", context_window_tokens: tokens } : c) : undefined} />
                : <span />}
              <div className="card-foot-actions">
                {ts?.state === "ok" && <span className="status-text is-ready"><Icon name="check" size={13} />可用 · {ts.error}</span>}
                <button className="btn btn-sm btn-ghost" disabled={!provider?.key_configured || !!refreshing} onClick={() => provider && void refreshProvider(provider.name)}>{refreshing === provider?.name ? "获取中…" : "刷新模型列表"}</button>
                <button className="btn btn-sm" title="会发一次很短的模型请求，可能产生少量费用" onClick={() => void test(key)} disabled={ts?.state === "testing" || !node.model.trim() || !provider?.key_configured}>
                  {ts?.state === "testing" ? <><Icon name="loading" className="icon-spin" size={13} />测试中</> : "测试调用"}
                </button>
              </div>
            </div>
            {provider?.error && <p className="field-hint is-bad">{provider.error}</p>}
            {ts?.state === "fail" && <p className="field-hint is-bad">测试失败：{ts.error}</p>}
            {nodeError && <p className="field-hint is-bad">{nodeError}</p>}
          </section>
        );
      })}
      {error && !SETTINGS_NODES.some(n => primaryMessage.includes(n.key)) && <p className="field-hint is-bad">{error}</p>}
    </div>
    <SettingsFooter s={s} />
  </>;
}

function CapabilityTab({ s, onTab }: { s: RuntimeSettings; onTab: (tab: SettingsTab) => void }) {
  if (!s.config) return <SettingsLoading error={s.error} />;
  return <>
    <CapabilitySettings s={s} onManageSkills={() => onTab("skills")} />
    <SettingsFooter s={s} />
  </>;
}

/** 模型 / 研究能力两页都读写 SettingsPage 持有的同一份草稿。 */
export function RuntimeTab({ tab, s, onTab }: { tab: "models" | "capabilities"; s: RuntimeSettings; onTab: (tab: SettingsTab) => void }) {
  return tab === "models" ? <ModelSettings s={s} /> : <CapabilityTab s={s} onTab={onTab} />;
}

function ConnectionSettings() {
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [refreshing, setRefreshing] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  useEffect(() => { getJSON<ProviderStatus[]>("/api/providers").then(setProviders).catch(e => setNotice(e instanceof Error ? e.message : "读取失败")); }, []);
  async function refresh(id: string) {
    setRefreshing(id); setNotice("");
    try {
      const [result, catalog] = await Promise.allSettled([
        postJSON<ProviderStatus>(`/api/providers/${id}/refresh`, {}),
        postJSON<{ ok: boolean; error?: string }>("/api/model-catalog/refresh", {}),
      ]);
      setProviders(await getJSON<ProviderStatus[]>("/api/providers"));
      if (result.status === "rejected") throw result.reason;
      const catalogError = catalog.status === "fulfilled" ? catalog.value.error : "模型目录暂时不可用，可以手动补充资料。";
      setNotice([result.value.reachable ? `已获取 ${result.value.models.length} 个模型。` : result.value.error, catalogError].filter(Boolean).join(" "));
    } catch (e) { setNotice(e instanceof Error ? e.message : "获取失败，保留已有模型列表。"); }
    finally { setRefreshing(null); }
  }
  return <>{notice && <p className="notice" role="status">{notice}</p>}
    <ProviderConnections providers={providers} onChange={setProviders} onRefresh={refresh} refreshing={refreshing} /></>;
}

function GeneralSettings({ prefs, onPrefs, onDemo }: { prefs: LocalPrefs; onPrefs: (p: LocalPrefs) => void; onDemo: () => void }) {
  return <div className="settings-section">
    <div className="section-head"><div><h2>通用</h2></div></div>
    <div className="setting-group">
      <h3>开发者选项</h3>
      <p className="field-hint">用于排查问题，日常使用保持默认即可。这些选项只保存在这个浏览器里。</p>
      <SettingRow title="显示「运行记录」" description="在研究页顶部显示运行记录标签，可以查看每次模型和工具调用的细节与用量。">
        <Switch label="显示运行记录" checked={prefs.showRecords} onChange={v => onPrefs({ ...prefs, showRecords: v })} />
      </SettingRow>
      <SettingRow title="记录完整的模型输入输出" description="之后新建的研究会保存每次模型调用的完整内容，方便排查，但会占用更多磁盘。已有研究的追问沿用原来的设置。">
        <Switch label="记录完整的模型输入输出" checked={prefs.traceFull} onChange={v => onPrefs({ ...prefs, traceFull: v })} />
      </SettingRow>
      <SettingRow title="历史回放" description="在历史列表里显示「快放」按钮，按时间顺序重播一次研究过程。">
        <Switch label="历史回放" checked={prefs.showReplay} onChange={v => onPrefs({ ...prefs, showReplay: v })} />
      </SettingRow>
      <SettingRow title="演示研究" description="播放一段内置的示例研究过程，不调用模型。">
        <button className="btn btn-sm" onClick={onDemo}><Icon name="play" size={13} />播放</button>
      </SettingRow>
    </div>
  </div>;
}

export function SettingsPage({ tab, onTab, prefs, onPrefs, onDemo, onSaved }: {
  tab: SettingsTab; onTab: (tab: SettingsTab) => void;
  prefs: LocalPrefs; onPrefs: (p: LocalPrefs) => void; onDemo: () => void;
  onSaved?: (c: ConfigPayload) => void;
}) {
  const runtime = useRuntimeSettings(onSaved);
  const { config, reloadProviders } = runtime;
  const isRuntimeTab = (id: SettingsTab) => id === "models" || id === "capabilities";
  // 从其他页回到模型 / 能力页时只刷新连接列表；草稿保持不变。
  function selectTab(id: SettingsTab) {
    if (isRuntimeTab(id) && !isRuntimeTab(tab) && config) reloadProviders();
    onTab(id);
  }
  return <div className="settings-page">
    <nav className="settings-nav" aria-label="设置分类">
      <h1>设置</h1>
      {TABS.map(([id, label]) => <button key={id} type="button" aria-current={tab === id ? "page" : undefined}
        className={`settings-nav-item${tab === id ? " is-active" : ""}`} onClick={() => selectTab(id)}>{label}</button>)}
    </nav>
    <div className="settings-main">
      <div className="settings-scroll">
        {tab === "models" || tab === "capabilities" ? <RuntimeTab tab={tab} s={runtime} onTab={onTab} />
          : tab === "connections" ? <div className="settings-section-wrap"><ConnectionSettings /></div>
          : tab === "skills" ? <SkillsPanel />
          : tab === "mcp" ? <McpManager />
          : <GeneralSettings prefs={prefs} onPrefs={onPrefs} onDemo={onDemo} />}
      </div>
    </div>
  </div>;
}
