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
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);
  const sources = [...new Set(["context_window", "max_output_tokens", "reasoning_efforts"]
    .map(f => info?.sources[f]).filter(s => s !== undefined).map(s => sourceLabel[s]))];
  async function save() {
    setBusy(true); setMessage(""); setFailed(false);
    try {
      const updated = await putJSON<ProviderStatus>(`/api/providers/${provider.name}/model-metadata`, {
        model, context_window: context ? Number(context) : null,
        reasoning_efforts: noEfforts ? [] : efforts.trim() ? efforts.split(/[,，]/).map(v => v.trim()).filter(Boolean) : null,
      });
      onChange(updated); setMessage("已保存。");
    } catch (e) { setFailed(true); setMessage(e instanceof Error ? e.message : "保存失败"); }
    finally { setBusy(false); }
  }
  return <div className="model-capabilities">
    <span className="field-hint">
      上下文 {k(info?.context_window)} · 最长输出 {k(info?.max_output_tokens)}
      {info?.reasoning_efforts != null ? ` · 思考档位 ${info.reasoning_efforts.join(" / ") || "无"}` : ""}
      {sources.length ? ` · 资料来自 ${sources.join("、")}` : ""}
    </span>
    {onUseContext && !!info?.context_window && info.context_window > 12000 &&
      <button type="button" className="link-btn" onClick={() => onUseContext(info.context_window!)}>用作自动整理的容量</button>}
    <details className="disclosure disclosure-inline"><summary>手动补充模型资料</summary>
      <p className="field-hint">只影响这个连接下的「{model}」。留空则恢复自动资料；手填不会改变服务商实际支持的能力。</p>
      <div className="field-grid">
        <label className="field"><span className="field-label">上下文窗口（token）</span><input className="input" type="number" min={1} max={100000000} step={1}
          value={context} placeholder="留空使用自动资料" onChange={e => setContext(e.target.value)} /></label>
        <label className="field"><span className="field-label">支持的思考档位</span><input className="input" value={efforts} disabled={noEfforts}
          placeholder="例如 low, high, max" onChange={e => setEfforts(e.target.value)} /></label>
      </div>
      <label className="check-line"><input type="checkbox" checked={noEfforts} onChange={e => setNoEfforts(e.target.checked)} />这个模型没有可选的思考档位</label>
      <div className="form-actions form-actions-start"><button type="button" className="btn btn-sm" disabled={busy} onClick={() => void save()}>{busy ? "保存中…" : "保存模型资料"}</button>
        {message && <span className={`field-hint${failed ? " is-bad" : ""}`} role="status">{message}</span>}</div>
    </details>
  </div>;
}
