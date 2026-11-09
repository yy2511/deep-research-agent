import { feedbackUrl, REPO_URL } from "../feedback";

/** “提意见”与“源码 · star”两个链接（#55）。在新标签页打开 GitHub，不打断当前页面。 */
export function FeedbackLinks({ query, finishedAt, className = "", compact = false }: {
  query?: string; finishedAt?: number; className?: string;
  /** 侧边栏用短文字，避免折行。 */
  compact?: boolean;
}) {
  return (
    <p className={`feedback-links ${className}`.trim()}>
      <a className="link-btn" href={feedbackUrl({ query, finishedAt })} target="_blank" rel="noreferrer">提意见</a>
      <span aria-hidden="true"> · </span>
      <a className="link-btn" href={REPO_URL} target="_blank" rel="noreferrer">{compact ? "源码 · star" : "在 GitHub 上查看源码 · 点个 star"}</a>
    </p>
  );
}
