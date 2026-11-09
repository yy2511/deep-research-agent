/**
 * 意见反馈（阶段 2 第 4 步 #55；设计 docs/design/阶段2-公开试用.md §5）。
 * 反馈走仓库的 GitHub Discussions（默认的 General 分类），不自建留言系统；
 * 预填问题内容和研究时间，不放研究链接（别人打不开）。
 */
export const REPO_URL = "https://github.com/yy2511/deep-research-agent";
export const FEEDBACK_CATEGORY = "general";
const TITLE_QUERY_CHARS = 40;

function shorten(text: string, max: number): string {
  const flat = text.replace(/\s+/g, " ").trim();
  const chars = Array.from(flat);
  return chars.length > max ? `${chars.slice(0, max).join("")}…` : flat;
}

function day(ts: number | undefined): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "";
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** 讨论区新帖链接。``query`` 为空时是一般反馈（侧边栏入口）。 */
export function feedbackUrl({ query, finishedAt }: { query?: string; finishedAt?: number } = {}): string {
  const q = (query ?? "").trim();
  const title = q ? `反馈：${shorten(q, TITLE_QUERY_CHARS)}` : "反馈：";
  const lines = [
    ...(q ? [`**问的问题**：${shorten(q, 500)}`] : []),
    ...(day(finishedAt) ? [`**研究时间**：${day(finishedAt)}`] : []),
    ...(q ? [""] : []),
    "**哪里有用**：",
    "",
    "**哪里不对 / 不好用**：",
    "",
    "**希望增加什么**：",
  ];
  // 用 encodeURIComponent（空格编码为 %20），不用 URLSearchParams（空格编码为 +，不是每处都按空格解析）。
  const search = Object.entries({ category: FEEDBACK_CATEGORY, title, body: lines.join("\n") })
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`).join("&");
  return `${REPO_URL}/discussions/new?${search}`;
}
