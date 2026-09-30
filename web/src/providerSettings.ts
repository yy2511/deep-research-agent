import type { ConfigPayload } from "./components/SettingsPage";

export interface ModelInfo {
  id: string;
  context_window?: number | null;
  max_output_tokens?: number | null;
  reasoning?: boolean | null;
  reasoning_efforts?: string[] | null;
  sources: Record<string, "api" | "catalog" | "manual">;
}
export interface ProviderStatus {
  name: string; label: string; custom: boolean; base_url: string;
  key_configured: boolean; reachable: boolean; models: string[]; model_details: ModelInfo[];
  error?: string | null; checked_at?: number | null;
  catalog_provider?: string; reasoning_format?: string;
}
export const sourceLabel = { api: "API 返回", catalog: "模型目录", manual: "手动填写" };

export function selectModel(config: ConfigPayload, key: "planner" | "subagent", provider: ProviderStatus | undefined, model: string): ConfigPayload {
  const info = provider?.model_details?.find(m => m.id === model);
  const selected = { ...config[key], provider: provider?.name ?? config[key].provider, model, effort: null };
  if (key === "planner") return { ...config, planner: selected };
  const context = info?.context_window ?? null;
  return {
    ...config, subagent: { ...selected, reasoning: info?.reasoning === false ? false : config.subagent.reasoning },
    context_window_tokens: context,
    compaction_enabled: context && context > 12000 ? config.compaction_enabled : false,
  };
}

export function effortOptions(info: ModelInfo | undefined, current: string | null) {
  const labels: Record<string, string> = { none: "关闭", minimal: "极低", low: "低", medium: "中", high: "高", xhigh: "更高", max: "最高" };
  const options = [{ value: "", label: "服务商默认" }, ...(info?.reasoning_efforts ?? []).map(value => ({ value, label: `${labels[value] ?? value} · ${value}` }))];
  if (current && !options.some(o => o.value === current)) options.push({ value: current, label: `${current} · 未确认` });
  return options;
}

export function modelSettingsError(config: ConfigPayload, providers: ProviderStatus[]) {
  for (const key of ["planner", "subagent"] as const) {
    const node = config[key];
    const provider = providers.find(p => p.name === node.provider);
    if (!provider?.key_configured) return `${key}：请选择已配置 Key 的 API 连接。`;
    if (!node.model.trim()) return `${key}：请选择或填写模型 ID。`;
    if (node.effort && !/^[a-zA-Z0-9_-]{1,32}$/.test(node.effort)) return `${key}：思考档位须为简短英文名称。`;
  }
  return null;
}
