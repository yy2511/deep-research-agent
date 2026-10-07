import { useState } from "react";
import { putJSON } from "../api";
import { sourceLabel, type ModelInfo, type ProviderStatus } from "../providerSettings";

const k = (n?: number | null) => n ? (n >= 1000 ? `${Math.round(n / 1000).toLocaleString()}k` : String(n)) : "未知";

export function ModelCapabilities({ provider, model, info, onChange, onUseContext }: {
  provider: ProviderStatus; model: string; info?: ModelInfo;
  onChange: (provider: ProviderStatus) => void; onUseContext?: (tokens: number) => void;
}) {
  const [context, setContext] = useState(info?.sources.context_window === "manual" ? String(info.context_window ?? "") : "");
  const [efforts, setEfforts] = useState(info?.sources.reasoning_efforts === "manual" ? info.reasoning_efforts?.join(", ") ?? "" : "");
  const [noEfforts, setNoEfforts] = useState(info?.sources.reasoning_efforts === "manual" && info.reasoning_efforts?.length === 0);
  const [imageInput, setImageInput] = useState(info?.sources.image_input === "manual" ? String(info.image_input) : "");
  const [imageTransport, setImageTransport] = useState(info?.sources.image_transport === "manual" ? String(info.image_transport) : "");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  const sources = [...new Set(["context_window", "max_input_tokens", "max_output_tokens", "reasoning_efforts"]
    .map(f => info?.sources[f]).filter(s => s !== undefined).map(s => sourceLabel[s]))];
  async function save() {
    setBusy(true); setMessage(""); setFailed(false);
    try {
      const updated = await putJSON<ProviderStatus>(`/api/providers/${provider.name}/model-metadata`, {
        model, image_input: imageInput ? imageInput === "true" : null, image_transport: imageTransport ? imageTransport === "true" : null, context_window: context ? Number(context) : null,
        max_input_tokens: info?.sources.max_input_tokens === "manual" ? info.max_input_tokens : null,
        max_output_tokens: info?.sources.max_output_tokens === "manual" ? info.max_output_tokens : null,
        reasoning_efforts: noEfforts ? [] : efforts.trim() ? efforts.split(/[,，]/).map(v => v.trim()).filter(Boolean) : null,
      });
      onChange(updated); setMessage("已保存。");
    } catch (e) { setFailed(true); setMessage(e instanceof Error ? e.message : "保存失败"); }
    finally { setBusy(false); }
  }
  return <div className="model-capabilities">
    <span className="field-hint">
      上下文 {k(info?.context_window)} · 独立输入上限 {k(info?.max_input_tokens)} · 最长输出 {k(info?.max_output_tokens)}
      {info?.reasoning_efforts != null ? ` · 思考档位 ${info.reasoning_efforts.join(" / ") || "无"}` : ""}
      {sources.length ? ` · 资料来自 ${sources.join("、")}` : ""}
      {info?.image_input === true && info?.image_transport === true ? ` · 可原生看图（${sourceLabel[info.sources.image_transport] ?? '已确认'}）` : ""}
    </span>
    {onUseContext && !!info?.context_window && ["api", "manual"].includes(info.sources.context_window) &&
      <button type="button" className="link-btn" onClick={() => onUseContext(info.context_window!)}>用作工作窗口</button>}
    <details className="disclosure disclosure-inline"><summary>手动补充模型资料</summary>
      <p className="field-hint">只影响这个连接下的「{model}」。留空则恢复自动资料；手填不会改变服务商实际支持的能力。</p>
      <div className="field-grid">
        <label className="field"><span className="field-label">上下文窗口（token）</span><input className="input" type="number" min={1} max={100000000} step={1}
          value={context} placeholder="留空使用自动资料" onChange={e => setContext(e.target.value)} /></label>
        <label className="field"><span className="field-label">支持的思考档位</span><input className="input" value={efforts} disabled={noEfforts}
          placeholder="例如 low, high, max" onChange={e => setEfforts(e.target.value)} /></label>
      </div>
      <div className="field-grid">
        <label className="field">模型支持图片输入<select className="input" value={imageInput} onChange={e=>setImageInput(e.target.value)}><option value="">自动 / 未确认</option><option value="true">已确认支持</option><option value="false">不支持</option></select></label>
        <label className="field">此连接可传图片<select className="input" value={imageTransport} onChange={e=>setImageTransport(e.target.value)}><option value="">自动 / 未确认</option><option value="true">已确认可传</option><option value="false">不能传</option></select></label>
      </div>
      <label className="check-line"><input type="checkbox" checked={noEfforts} onChange={e => setNoEfforts(e.target.checked)} />这个模型没有可选的思考档位</label>
      <div className="form-actions form-actions-start"><button type="button" className="btn btn-sm" disabled={busy} onClick={() => void save()}>{busy ? "保存中…" : "保存模型资料"}</button>
        {message && <span className={`field-hint${failed ? " is-bad" : ""}`} role="status">{message}</span>}</div>
    </details>
  </div>;
}
