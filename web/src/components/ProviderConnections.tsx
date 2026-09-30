import { useState } from "react";
import { postJSON, putJSON, request } from "../api";
import type { ProviderStatus } from "../providerSettings";
import { Icon } from "./Icon";
import { Select } from "./Select";

const blank = { label: "", base_url: "", api_key: "", catalog_provider: "", reasoning_format: "auto" };
const formats = [
  { value: "auto", label: "自动识别（官方地址）" },
  { value: "standard", label: "标准 · reasoning_effort" },
  { value: "thinking", label: "DeepSeek / OpenCode · thinking" },
  { value: "openrouter", label: "OpenRouter · reasoning" },
  { value: "none", label: "不发送思考参数" },
];

export function ProviderConnections({ providers, onChange, onRefresh, refreshing }: {
  providers: ProviderStatus[]; onChange: (items: ProviderStatus[]) => void;
  onRefresh: (id: string) => Promise<void>; refreshing: string | null;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [form, setForm] = useState(blank);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [deleting, setDeleting] = useState<string | null>(null);

  function edit(provider?: ProviderStatus) {
    setError(""); setDeleting(null); setEditing(provider?.name ?? "new");
    setForm(provider ? { label: provider.label, base_url: provider.base_url, api_key: "",
      catalog_provider: provider.catalog_provider ?? "", reasoning_format: provider.reasoning_format ?? "auto" } : blank);
  }
  async function save() {
    setBusy(true); setError("");
    try {
      const item = editing === "new"
        ? await postJSON<ProviderStatus>("/api/providers", form)
        : await putJSON<ProviderStatus>(`/api/providers/${editing}`, form);
      onChange([...providers.filter(p => p.name !== item.name), item]);
      setForm(blank); setEditing(null);
    } catch (e) { setError(e instanceof Error ? e.message : "保存失败"); }
    finally { setBusy(false); }
  }
  async function remove(id: string) {
    setBusy(true); setError("");
    try {
      await request(`/api/providers/${id}`, { method: "DELETE" });
      onChange(providers.filter(p => p.name !== id)); setDeleting(null);
    } catch (e) { setError(e instanceof Error ? e.message : "删除失败"); }
    finally { setBusy(false); }
  }
  return <div className="settings-section">
    <div className="section-head">
      <div><h2>API 连接</h2><p>添加兼容 OpenAI 接口的服务地址和 Key，然后获取可用模型。Key 保存后不再显示。</p></div>
      {!editing && <button className="btn" onClick={() => edit()} disabled={busy}><Icon name="plus" size={15} />添加连接</button>}
    </div>
    {error && <p className="field-hint is-bad" role="alert">{error}</p>}
    {providers.length > 0 && <div className="list-card">
      {[...providers].sort((a, b) => Number(b.custom) - Number(a.custom)).map(p => <div className="list-row-group" key={p.name}>
        <div className="list-row">
          <div className="list-row-main">
            <strong>{p.label || p.name}{!p.custom && <span className="tag">来自 .env</span>}</strong>
            <code>{p.base_url}</code>
          </div>
          <span className={`status-text${p.key_configured ? " is-ready" : " is-warn"}`}>
            {p.key_configured ? <><Icon name="check" size={13} />Key 已保存</> : "未配置 Key"}
          </span>
          <span className="status-text">{p.checked_at ? `${p.models.length} 个模型${p.reachable ? "" : "（获取失败，保留旧列表）"}` : "还没获取模型"}</span>
          <div className="list-row-actions">
            <button className="btn btn-sm" disabled={!p.key_configured || !!refreshing || busy || !!editing} onClick={() => void onRefresh(p.name)}>{refreshing === p.name ? "获取中…" : "获取模型"}</button>
            {p.custom ? <>
              <button className="btn btn-sm" disabled={busy || !!editing || !!refreshing} onClick={() => edit(p)}>编辑</button>
              <button className="btn btn-sm btn-ghost" disabled={busy || !!editing || !!refreshing} onClick={() => { setDeleting(p.name); setError(""); }}>删除</button>
            </> : <span className="field-hint">在 .env 中修改</span>}
          </div>
        </div>
        {p.error && <p className="field-hint is-bad list-row-note">{p.error}</p>}
        {deleting === p.name && <div className="confirm-bar"><span>删除「{p.label}」和保存的 Key？</span>
          <button className="btn btn-sm btn-danger" disabled={busy} onClick={() => void remove(p.name)}>确认删除</button>
          <button className="btn btn-sm btn-ghost" disabled={busy} onClick={() => setDeleting(null)}>保留</button></div>}
      </div>)}
    </div>}
    {editing && <form className="form-card" onSubmit={e => { e.preventDefault(); void save(); }}>
      <h3>{editing === "new" ? "添加连接" : "编辑连接"}</h3>
      <label className="field"><span className="field-label">名称</span><input className="input" value={form.label} maxLength={80} required placeholder="例如：我的 OpenCode"
        onChange={e => setForm({ ...form, label: e.target.value })} /></label>
      <label className="field"><span className="field-label">API 地址</span><input className="input mono" value={form.base_url} type="url" required placeholder="https://api.example.com/v1"
        onChange={e => setForm({ ...form, base_url: e.target.value })} />
        <small className="field-hint">填服务商给的根地址，通常以 /v1 结尾。不要填完整的聊天接口地址。</small></label>
      <label className="field"><span className="field-label">API Key</span><input className="input" value={form.api_key} type="password" autoComplete="new-password" required={editing === "new"}
        placeholder={editing === "new" ? "sk-…" : "留空则保留已保存的 Key"}
        onChange={e => setForm({ ...form, api_key: e.target.value })} />
        <small className="field-hint">只保存在本机，保存后不再显示。</small></label>
      <details className="disclosure"><summary>高级设置（使用代理服务时可能需要）</summary>
        <div className="field"><span className="field-label">思考参数格式</span><Select ariaLabel="思考参数格式" value={form.reasoning_format} options={formats}
          onChange={v => setForm({ ...form, reasoning_format: v })} /></div>
        <label className="field"><span className="field-label">模型目录服务商</span><input className="input" value={form.catalog_provider} placeholder="可选，例如 opencode 或 openai"
          onChange={e => setForm({ ...form, catalog_provider: e.target.value })} />
          <small className="field-hint">官方地址会自动从 models.dev 匹配模型资料。代理地址要在这里指定服务商才会补充；实际容量和档位以代理服务为准。</small></label>
      </details>
      <div className="form-actions">
        <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => { setEditing(null); setForm(blank); setError(""); }}>取消</button>
        <button type="submit" className="btn btn-primary" disabled={busy}>{busy ? "保存中…" : "保存连接"}</button>
      </div>
    </form>}
  </div>;
}
