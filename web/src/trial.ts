/**
 * 公开试用（#50）：GitHub 访客今天还能发起几次研究。数据来自 GET /api/me 的 trial（后端 #49）。
 * 真正的限制在服务端；这里只负责说清楚，并在用完时提前把按钮变灰。
 */
export type TrialStatus = {
  open: boolean;
  runs_left: number;
  runs_limit: number;
  global_exhausted?: boolean;
  resets_at: string | null;
};

export const LOCAL_RUN_URL = "https://github.com/yy2511/deep-research-agent#readme";

/** “11 月 10 日 00:00”。按浏览器所在时区显示。 */
export function resetTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** 不能再发起研究的原因；还能问时返回 null。 */
export function trialBlocked(trial: TrialStatus | null | undefined): string | null {
  if (!trial) return null;
  if (!trial.open) return "试用暂未开放。";
  if (trial.runs_left > 0) return null;
  const when = resetTime(trial.resets_at);
  return trial.global_exhausted
    ? `今天的试用名额已经用完了${when ? `，${when}恢复` : ""}。`
    : `你今天的试用次数用完了${when ? `，${when}恢复` : ""}。`;
}

/** 还能问时的一句提示。 */
export function trialSummary(trial: TrialStatus): string {
  return `今天还可以问 ${trial.runs_left} 次（每天 ${trial.runs_limit} 次，追问也算一次）`;
}
