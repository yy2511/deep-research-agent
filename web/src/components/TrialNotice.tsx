import { LOCAL_RUN_URL, trialBlocked, trialSummary, type TrialStatus } from "../trial";

/** 访客提问框下的试用说明（#50）：还剩几次；用完或未开放时给出看示例、本机运行两个出口。 */
export function TrialNotice({ trial, onShowcases }: { trial: TrialStatus; onShowcases?: () => void }) {
  const blocked = trialBlocked(trial);
  if (!blocked) return <p className="field-hint trial-notice">{trialSummary(trial)}</p>;
  return (
    <p className="field-hint is-warn trial-notice" role="status">
      {blocked}
      {onShowcases && <> <button type="button" className="link-btn" onClick={onShowcases}>看看示例</button>，</>}
      {" "}或<a className="link-btn" href={LOCAL_RUN_URL} target="_blank" rel="noreferrer">在自己电脑上运行</a>（不限次数，用你自己的 key）。
    </p>
  );
}
