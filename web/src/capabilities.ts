/** 设置页“研究能力”的只读状态：服务端返回什么就说什么，不在前端推测。 */

export type NetworkMode = "none" | "bridge";

/** GET /api/execution/status：新研究将使用的执行开关与 Docker 实测结果。 */
export interface ExecutionStatus {
  enabled: boolean;
  available: boolean;
  reason?: string;
  image_id?: string;
  image?: string;
  network_mode: NetworkMode | string;
  /** environment=跟随本机默认；settings=在设置页明确选择过。 */
  source?: "environment" | "settings";
  default_enabled?: boolean;
}

/** GET /api/config 附带的只读摘要（不探测 Docker）。 */
export interface ExecutionEffective {
  enabled: boolean; network_mode: string; source: "environment" | "settings"; default_enabled: boolean;
}

export interface RetryPolicy {
  research: { max_retries: number; max_attempts: number };
  summary: { max_retries: number; max_attempts: number; timeout_s: number };
}

export type Tone = "ok" | "warn" | "bad" | "off" | "busy";

const REASON_TEXT: Record<string, string> = {
  docker_unavailable: "连不上 Docker。请确认本机的 Docker 已经启动。",
  image_missing: "本机还没有执行环境的镜像，需要先构建。",
  image_incompatible: "执行环境的镜像版本不匹配，需要重新构建。",
  cleanup_pending: "上次留下的执行环境还没清理完，先暂停新的执行；重启服务时会再清理。",
  invalid_configuration: "Docker 连接地址不受支持，只能连接本机的 Docker。",
  workspace_unavailable: "工作文件夹暂时不可用。",
};

export function executionReasonText(reason?: string) {
  if (!reason) return "暂时不可用。";
  return REASON_TEXT[reason] ?? `暂时不可用（${reason}）。`;
}

export function networkModeText(mode?: string) {
  return mode === "bridge" ? "可以联网：命令里能下载公开文件" : mode === "none" ? "不联网：只处理已取得的资料和预装工具" : mode ? `网络模式：${mode}` : "网络模式未知";
}

/** 把执行状态翻成一句标题和一句说明；loading / error 由调用方传入。 */
export function executionStatusView(status: ExecutionStatus | null, opts: { loading?: boolean; error?: string } = {}): { tone: Tone; title: string; detail: string } {
  if (opts.error) return { tone: "bad", title: "检查失败", detail: opts.error };
  if (!status) return { tone: "busy", title: "正在检查…", detail: "" };
  if (!status.enabled) return { tone: "off", title: "已关闭", detail: "新研究不会提供运行代码的工具。" };
  if (status.available) return { tone: "ok", title: "已开启，可以使用", detail: "新研究的主、子 Agent 可以在隔离环境里运行代码、生成文件。" };
  return { tone: "warn", title: "已开启，但暂时用不了", detail: `${executionReasonText(status.reason)}研究仍会进行，只是不能运行代码或生成文件。` };
}

export interface ExecutionProbe { status: ExecutionStatus | null; loading: boolean; error?: string; checkedAt?: number }

/** 新研究现在的执行状态：权限只按已保存设置判断；Docker 可用性只采纳与该权限一致、且不在复查中的检查结果。 */
export function executionNowView(savedEnabled: boolean | null | undefined, probe: ExecutionProbe): { tone: Tone; title: string; detail: string; short: string; stale: boolean } {
  const result = (tone: Tone, title: string, detail: string, short: string, stale = false) => ({ tone, title, detail, short, stale });
  if (savedEnabled === false) return result("off", "已关闭", "新研究不会提供运行代码的工具。", "不提供 Bash");
  if (probe.loading) return result("busy", savedEnabled ? "已开启，正在检查 Docker…" : "正在检查…", "", "检查中…");
  if (probe.error) return result("bad", "检查失败", probe.error, "状态检查失败");
  const status = probe.status;
  if (!status) return result("busy", "还没有检查", "", "等待检查");
  if (savedEnabled != null && status.enabled !== savedEnabled)
    return result("busy", "检查结果已过期", "这次检查早于最近一次保存，请点「重新检查」。", "等待重新检查", true);
  const view = executionStatusView(status);
  return result(view.tone, view.title, view.detail, !status.enabled ? "不提供 Bash" : status.available ? "Docker 可用" : "暂时用不了");
}

export function minutesText(seconds: number | null | undefined) {
  if (seconds == null) return "不限";
  if (seconds % 60 === 0) return `${seconds / 60} 分钟`;
  return `${seconds} 秒`;
}
