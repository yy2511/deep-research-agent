import { useEffect, useState } from "react";
import { getJSON, postJSON, putJSON } from "../api";
import { Icon } from "./Icon";
import { Select } from "./Select";

interface NodeConfig { model: string; provider: string; reasoning?: boolean; effort: string | null }
export interface ConfigPayload {
  planner: { model: string; provider: string; effort: string | null };
  subagent: NodeConfig;
  writer: NodeConfig;
  max_tool_calls: number;
}
interface ProviderStatus { name: string; key_configured: boolean; reachable: boolean; models: string[]; error?: string }

type NodeKey = "planner" | "subagent" | "writer";
type TestState = "idle" | "testing" | "ok" | "fail";

const NODES: Array<{ key: NodeKey; label: string; hasReasoning: boolean }> = [
  { key: "planner", label: "研究规划器", hasReasoning: false },
  { key: "subagent", label: "研究执行器", hasReasoning: true },
  { key: "writer", label: "报告写作器", hasReasoning: true },
];

// 模型可搜下拉：provider 的 models 已经从 /api/providers 拉回，但原来只塞进隐藏的
// <datalist>（需聚焦/输入才浮出，用户以为"没获取到列表"）。改成可见的 combobox——
// 展开完整列表、输入即过滤、点击选中、仍允许自定义模型名。
function ModelField({ value, models, onChange }: {
  value: string; models: string[]; onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const q = value.trim().toLowerCase();
  const matches = models.filter((m) => m.toLowerCase().includes(q));
  // 展示全部的三种情况：① 空输入；② 恰好等于某个完整模型名（"已选中"而非"正在搜"）；
  // ③ 零匹配——尤其刚切了 provider 后，旧 model 名（如 deepseek-v4-flash）在新 provider
  // （codex_local）列表里一个都不匹配，若按它过滤会空掉、看着像"列表没变化"。
  const showAll = q === "" || matches.length === 0 || models.some((m) => m.toLowerCase() === q);
  const filtered = (showAll ? models : matches).slice(0, 60);
  return (
    <div className="model-field">
      <input
        className="model-input"
        value={value}
        placeholder="模型名（可输入自定义）"
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
        <ul className="model-list">
          <li className="model-list-head">{showAll ? `${models.length} 个模型` : `${filtered.length}/${models.length} 匹配`}</li>
          {filtered.map((m) => (
            // onMouseDown 而非 onClick：抢在 input 的 onBlur 关闭列表之前触发选中
            <li key={m} className={m === value ? "on" : ""}
              onMouseDown={() => { onChange(m); setOpen(false); }}>{m}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function SettingsModal({ onClose, onSaved }: {
  onClose: () => void;
  onSaved: (c: ConfigPayload) => void;
}) {
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testState, setTestState] = useState<Record<string, { state: TestState; error?: string }>>({});
  const [refreshing, setRefreshing] = useState(false);

  useEffect(() => {
    getJSON<ConfigPayload>("/api/config").then(setConfig);
    getJSON<ProviderStatus[]>("/api/providers").then(setProviders);
  }, []);

  if (!config) {
    return <div className="modal-backdrop"><div className="modal">加载中…</div></div>;
  }

  function updateNode(node: NodeKey, patch: Partial<NodeConfig>) {
    // 动态 key 更新联合类型字段，TS 没法逐分支验证合法（同 reducer.ts 的
    // `HANDLERS as Record<string, Handler>` 同类断言，运行时形状始终正确）。
    setConfig((c) => (c ? ({ ...c, [node]: { ...c[node], ...patch } } as ConfigPayload) : c));
  }

  async function test(node: NodeKey) {
    if (!config) return;
    setTestState((s) => ({ ...s, [node]: { state: "testing" } }));
    const n = config[node];
    const body = {
      model: n.model, provider: n.provider,
      reasoning: "reasoning" in n ? !!n.reasoning : true,
      effort: n.effort,
    };
    const res = await postJSON<{ ok: boolean; error?: string }>("/api/config/test", body);
    setTestState((s) => ({ ...s, [node]: { state: res.ok ? "ok" : "fail", error: res.error } }));
  }

  // 改了 .env 的 key/base_url 后重探 provider（免重启）：后端 force_refresh 会清客户端缓存
  // 用现值重建，前端拿回新 status（key_configured/reachable/models 都刷新）。
  async function refreshProviders() {
    setRefreshing(true);
    try {
      setProviders(await postJSON<ProviderStatus[]>("/api/providers/refresh", {}));
    } catch {
      // 单个 provider 的失败已带在 status 里（reachable=false/error），整个请求极少失败；
      // 真失败保持旧状态即可，不打断用户。
    } finally {
      setRefreshing(false);
    }
  }

  async function save() {
    if (!config) return;
    setSaving(true);
    setError(null);
    try {
      const { max_tool_calls: _mtc, ...body } = config;
      const saved = await putJSON<ConfigPayload>("/api/config", body);
      onSaved(saved);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal settings-modal" onClick={(e) => e.stopPropagation()}>
        <div className="settings-head">
          <h3>模型设置</h3>
          <button className="settings-refresh" onClick={refreshProviders} disabled={refreshing}
            title="改了 .env 里的 key / base_url 后点这里重探（免重启即可生效）">
            <Icon name={refreshing ? "loading" : "refresh"} className={refreshing ? "icon-spin" : ""} size={14} />
            {refreshing ? "刷新中…" : "刷新"}
          </button>
        </div>
        {NODES.map(({ key, label, hasReasoning }) => {
          const node = config[key];
          const provider = providers.find((p) => p.name === node.provider);
          const ts = testState[key];
          // 校验错误原文里恒定带字段前缀（如 "writer_effort=..."），子串匹配 key
          // 就能把 pydantic 报错内联标到出错的那张卡——不用解析错误结构。
          // 但 pydantic 报错会在 "[type=value_error, input_value={...}, ...]" 里回显
          // 整个输入 dict（含 planner_model/subagent_model 等所有字段名），只搜前半句
          // 人类可读消息（真实实测踩过：Writer 的 effort 报错，因为回显里有
          // "planner_model" 子串，把 Planner 卡也误标红了）。
          const primaryMessage = error?.split("[type=")[0].toLowerCase() ?? "";
          const nodeError = primaryMessage.includes(key) ? error : null;
          return (
            <div className={`settings-card${nodeError ? " settings-card-error" : ""}`} key={key}>
              <div className="label">{label}</div>
              <div className="settings-row">
                <Select
                  ariaLabel="provider"
                  value={node.provider}
                  onChange={(v) => updateNode(key, { provider: v })}
                  options={providers.map((p) => ({
                    value: p.name, label: p.name, disabled: !p.key_configured,
                    hint: p.key_configured ? (p.reachable ? "" : "不可达") : "无 key",
                  }))}
                />
                <ModelField
                  value={node.model}
                  models={provider?.models ?? []}
                  onChange={(v) => updateNode(key, { model: v })}
                />
              </div>
              <div className="settings-row">
                {/* planner 分支的联合类型压根没有 reasoning 字段（架构上恒定开思考，
                    见 runtime_config.py 的 PlannerModelConfig）；hasReasoning 保证运行期
                    只有 subagent/writer 走到这里，但 TS 推不出这个关联，按既有惯例断言。 */}
                {hasReasoning && (
                  <label>
                    <input type="checkbox" checked={!!(node as NodeConfig).reasoning}
                      onChange={(e) => updateNode(key, { reasoning: e.target.checked })} />
                    深度思考
                  </label>
                )}
                <Select
                  ariaLabel="effort"
                  value={node.effort ?? ""}
                  disabled={hasReasoning && !(node as NodeConfig).reasoning}
                  onChange={(v) => updateNode(key, { effort: v || null })}
                  options={[
                    { value: "", label: "（默认）" },
                    { value: "low", label: "低" },
                    { value: "medium", label: "中" },
                    { value: "high", label: "高" },
                  ]}
                />
                <button onClick={() => test(key)} disabled={ts?.state === "testing"}>
                  测试
                  {ts?.state === "testing" && <Icon name="loading" className="icon-spin" size={13} />}
                  {ts?.state === "ok" && <Icon name="check" className="icon-success" size={13} />}
                  {ts?.state === "fail" && <Icon name="close" className="icon-danger" size={13} />}
                </button>
              </div>
              {ts?.state === "fail" && <div className="settings-error">{ts.error}</div>}
              {nodeError && <div className="settings-error">{nodeError}</div>}
            </div>
          );
        })}
        {error && !NODES.some((n) => (error.split("[type=")[0].toLowerCase()).includes(n.key)) && (
          <div className="settings-error">{error}</div>
        )}
        <div className="settings-actions">
          <button onClick={onClose}>取消</button>
          <button onClick={save} disabled={saving}>{saving ? "保存中…" : "保存"}</button>
        </div>
      </div>
    </div>
  );
}
