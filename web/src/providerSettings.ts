import type { ConfigPayload } from "./components/SettingsPage";

export interface ModelInfo {
  id: string;
  context_window?: number | null;
  max_output_tokens?: number | null;
  max_input_tokens?: number | null;
  image_input?: boolean | null;
  image_transport?: boolean | null;
  reasoning?: boolean | null;
  reasoning_efforts?: string[] | null;
  sources: Record<string, "api" | "catalog" | "official" | "manual">;
}
export interface ProviderStatus {
  name: string; label: string; custom: boolean; base_url: string;
  key_configured: boolean; reachable: boolean; models: string[]; model_details: ModelInfo[];
  error?: string | null; checked_at?: number | null;
  catalog_provider?: string; reasoning_format?: string;
}
export const sourceLabel = { api: "API 返回", catalog: "模型目录", official: "官方接口文档", manual: "手动填写" };

export function selectModel(config: ConfigPayload, key: "planner" | "subagent", provider: ProviderStatus | undefined, model: string): ConfigPayload {
  const info = provider?.model_details?.find(m => m.id === model);
  const selected = { ...config[key], provider: provider?.name ?? config[key].provider, model, effort: null };
  if (key === "planner") return { ...config, planner: selected };
  return {
    ...config, subagent: { ...selected, reasoning: info?.reasoning === false ? false : config.subagent.reasoning },
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
  if (config.compaction_enabled && (config.context_window_mode ?? "manual") === "manual" && !config.context_window_tokens)
    return "当前连接容量尚未确认，请填写工作窗口或选择已确认的容量。";
  if (config.compaction_enabled && config.context_window_mode === "auto") {
    const info = providers.find(p => p.name === config.subagent.provider)?.model_details.find(m => m.id === config.subagent.model);
    const confirmed = ["context_window", "max_input_tokens"].some(field =>
      ["api", "manual"].includes(info?.sources[field] ?? "") && !!info?.[field as "context_window" | "max_input_tokens"]);
    if (!confirmed) return "自动模式尚无此连接的容量声明，请刷新模型资料或改用手动工作窗口。";
  }
  return null;
}
