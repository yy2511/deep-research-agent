import { useEffect, useState } from "react";
import { getJSON, postJSON, putJSON } from "../api";
import { effortOptions, modelSettingsError, selectModel, type ProviderStatus } from "../providerSettings";
import { Icon } from "./Icon";
import { McpManager } from "./McpPanel";
import { ModelCapabilities } from "./ModelCapabilities";
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
  schema_version?: number;
  call_limits_enabled?: boolean;
  evidence_judge?: {enabled:boolean};
  evidence_judge_status?: {key_configured:boolean;model:string;concurrency:number;batch_timeout_s:number};
  compaction_enabled?: boolean;
  context_window_tokens?: number | null;
  planner: { model: string; provider: string; effort: string | null };
  subagent: NodeConfig;
  /** 最小闭环的真实预算摘要（服务端 SimpleConfig），启动前确认投入范围用。 */
  simple_budget?: SimpleBudget;
  simple_model?: { model: string; provider: string; reasoning?: boolean };
}

type NodeKey = "planner" | "subagent";
type TestState = "idle" | "testing" | "ok" | "fail";

export const SETTINGS_NODES: Array<{ key: NodeKey; label: string; hint: string; hasReasoning: boolean }> = [
  { key: "planner", label: "规划", hint: "澄清问题、制定研究计划；思考强度只用于制定计划", hasReasoning: false },
  { key: "subagent", label: "研究与写作", hint: "主 Agent 和子任务：检索、阅读、写回答", hasReasoning: true },
];

export function settingsUpdate(config: ConfigPayload) {
  // GET 包含只读运行摘要；只提交 RuntimeModelSettings 可写字段，避免响应扩展破坏保存。
  const { schema_version, planner, subagent, call_limits_enabled, compaction_enabled, context_window_tokens, evidence_judge } = config;
  return { schema_version, planner, subagent, call_limits_enabled, compaction_enabled, context_window_tokens, evidence_judge };
}

/** 本机偏好：只存在浏览器里，不影响后端配置。 */
export interface LocalPrefs { showRecords: boolean; showReplay: boolean; traceFull: boolean }

export type SettingsTab = "models" | "connections" | "skills" | "mcp" | "general";
const TABS: Array<[SettingsTab, string]> = [
  ["models", "模型"], ["connections", "API 连接"], ["skills", "技能"], ["mcp", "外部资料（MCP）"], ["general", "通用"],
];

// 模型可搜下拉：展开完整列表、输入即过滤、点击选中、仍允许自定义模型名。
function ModelField({ value, models, onChange, label }: {
  value: string; models: string[]; onChange: (v: string) => void; label: string;
}) {
  const [open, setOpen] = useState(false);
  const q = value.trim().toLowerCase();
  const matches = models.filter((m) => m.toLowerCase().includes(q));
  // 空输入、已等于某个完整模型名、或零匹配（刚切了连接）时展示全部。
  const showAll = q === "" || matches.length === 0 || models.some((m) => m.toLowerCase() === q);
  const filtered = (showAll ? models : matches).slice(0, 60);
  return (
    <div className="model-field">
      <input
        className="input"
        aria-label={label} role="combobox" aria-expanded={open} aria-autocomplete="list"
        value={value}
        placeholder="选择或输入模型名"
        onChange={(e) => { onChange(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
      />
      {models.length > 0 && (
        <button type="button" className="model-caret" tabIndex={-1} aria-label="展开模型列表"
          onMouseDown={(e) => { e.preventDefault(); setOpen((o) => !o); }}>
          <Icon name="chevron-down" size={14} />
        </button>
      )}
      {open && filtered.length > 0 && (
        <ul className="menu-list" role="listbox" aria-label={`${label}列表`}>
          <li className="menu-list-head">{showAll ? `${models.length} 个模型` : `${filtered.length}/${models.length} 匹配`}</li>
          {filtered.map((m) => (
            // onMouseDown 而非 onClick：抢在 input 的 onBlur 关闭列表之前触发选中
            <li key={m} role="option" aria-selected={m === value} className={`menu-item mono${m === value ? " is-on" : ""}`}
              onMouseDown={() => { onChange(m); setOpen(false); }}>{m}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ModelSettings({ onSaved }: { onSaved?: (c: ConfigPayload) => void }) {
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  const [savedJson, setSavedJson] = useState("");
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testState, setTestState] = useState<Record<string, { state: TestState; error?: string }>>({});
  const [refreshing, setRefreshing] = useState<string | null>(null);
  const [notice, setNotice] = useState("");

  function load() {
    Promise.all([getJSON<ConfigPayload>("/api/config"), getJSON<ProviderStatus[]>("/api/providers")])
      .then(([settings, connections]) => {
        setConfig(settings); setSavedJson(JSON.stringify(settingsUpdate(settings))); setProviders(connections); setError(null);
      })
      .catch(e => setError(e instanceof Error ? e.message : "读取设置失败"));
  }
  useEffect(load, []);

  if (!config) return <div className="settings-section"><p className={error ? "field-hint is-bad" : "field-hint"}>{error || "正在读取设置…"}</p></div>;
  const dirty = JSON.stringify(settingsUpdate(config)) !== savedJson;

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
  async function save() {
    if (!config) return;
    const invalid = modelSettingsError(config, providers);
    if (invalid) { setError(invalid); return; }
    setSaving(true);
    setError(null);
    try {
      const saved = await putJSON<ConfigPayload>("/api/config", settingsUpdate(config));
      setConfig(saved); setSavedJson(JSON.stringify(settingsUpdate(saved)));
      setNotice("已保存，之后开始的研究会使用新设置。");
      onSaved?.(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }
  // 校验错误原文带字段前缀（如 "subagent.effort=..."），只看前半句人类可读部分来定位出错的卡片。
  const primaryMessage = error?.split("[type=")[0].toLowerCase() ?? "";

  return <>
    <div className="settings-section">
      <div className="section-head"><div><h2>模型</h2><p>给两个环节分别选模型。修改只影响之后开始的研究。</p></div></div>
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
                    onUseContext={key === "subagent" ? tokens => setConfig(c => c ? { ...c, context_window_tokens: tokens } : c) : undefined} />
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
      <div className="setting-group">
        <h3>研究选项</h3>
        <SettingRow title="自动整理长上下文" description="对话太长时，把较早的过程压缩成摘要，保留最近几轮。证据和原始记录不受影响；整理本身会调用研究模型。">
          <Switch label="自动整理长上下文" checked={!!config.compaction_enabled} onChange={v => setConfig({ ...config, compaction_enabled: v })} />
        </SettingRow>
        {config.compaction_enabled && <label className="field field-indent"><span className="field-label">模型上下文容量（token）</span>
          <input className="input input-num" type="number" min={12001} step={1} value={config.context_window_tokens ?? ""}
            placeholder="填服务商实际支持的容量" aria-label="模型上下文容量"
            onChange={e => setConfig({ ...config, context_window_tokens: e.target.value ? Number(e.target.value) : null })} />
          <small className="field-hint">会预留 12,000 token；用到 85% 时开始整理。不知道容量时请关掉这个选项。</small></label>}
        <SettingRow title="保存证据时核验引文"
          description={<>把结论和引文发给核验模型（Jev），判断引文是否撑得起结论，结果显示在证据卡上，也会反馈给 Agent 修正。不会强行拦下引用；超时显示“未核验”。
            <br />{config.evidence_judge_status?.key_configured ? "密钥已配置" : "密钥未配置"} · {config.evidence_judge_status?.model ?? "Jev"}</>}>
          <Switch label="保存证据时核验引文" checked={!!config.evidence_judge?.enabled} onChange={v => setConfig({ ...config, evidence_judge: { enabled: v } })} />
        </SettingRow>
        <SettingRow title="限制调用次数" description="开启后，主 Agent 和子任务的模型、工具调用次数都有上限，并留出收尾的次数。不论开关，研究总时限都有效。">
          <Switch label="限制调用次数" checked={!!config.call_limits_enabled} onChange={v => setConfig({ ...config, call_limits_enabled: v })} />
        </SettingRow>
      </div>
      {error && !SETTINGS_NODES.some(n => primaryMessage.includes(n.key)) && <p className="field-hint is-bad">{error}</p>}
    </div>
    <footer className="settings-footer">
      <span className={`field-hint${dirty ? " is-warn" : ""}`}>{dirty ? "有未保存的修改" : ""}</span>
      <button className="btn btn-ghost" disabled={!dirty || saving} onClick={load}>放弃修改</button>
      <button className="btn btn-primary" onClick={() => void save()} disabled={saving || !dirty}>{saving ? "保存中…" : "保存"}</button>
    </footer>
  </>;
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
  return <div className="settings-page">
    <nav className="settings-nav" aria-label="设置分类">
      <h1>设置</h1>
      {TABS.map(([id, label]) => <button key={id} type="button" aria-current={tab === id ? "page" : undefined}
        className={`settings-nav-item${tab === id ? " is-active" : ""}`} onClick={() => onTab(id)}>{label}</button>)}
    </nav>
    <div className="settings-main">
      <div className="settings-scroll">
        {tab === "models" ? <ModelSettings onSaved={onSaved} />
          : tab === "connections" ? <div className="settings-section-wrap"><ConnectionSettings /></div>
          : tab === "skills" ? <SkillsPanel />
          : tab === "mcp" ? <McpManager />
          : <GeneralSettings prefs={prefs} onPrefs={onPrefs} onDemo={onDemo} />}
      </div>
    </div>
  </div>;
}
