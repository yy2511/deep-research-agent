import { useEffect, useRef, useState } from "react";
import { STEP_LABEL, type PlanProgress } from "../planProgress";
import { Icon } from "./Icon";

function seconds(ms: number) {
  return `${Math.max(0, Math.floor(ms / 1000))} 秒`;
}

/** 规划进行中：显示步骤、已等待时间和实时滚动的思考过程。 */
export function PlanningProgress({ progress }: { progress: PlanProgress }) {
  const [now, setNow] = useState(() => Date.now());
  const [open, setOpen] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const current = progress.stage;
  const thinking = current ? progress.thinking[current] ?? "" : "";
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [thinking, open]);
  const currentIndex = current ? progress.steps.indexOf(current) : -1;

  return (
    <section className="plan-progress" aria-live="polite" aria-label="规划进度">
      <ol className="plan-steps">
        {progress.steps.map((step, index) => {
          const state = index < currentIndex ? "done" : index === currentIndex ? "active" : "pending";
          return (
            <li key={step} className={`plan-step is-${state}`}>
              <span className="plan-step-mark">
                {state === "done" ? <Icon name="check" size={13} /> : state === "active"
                  ? <Icon name="loading" className="icon-spin" size={13} /> : null}
              </span>
              <span className="plan-step-label">{STEP_LABEL[step]}</span>
              {state === "active" && <span className="plan-step-time">已 {seconds(now - progress.stageStartedAt)}</span>}
            </li>
          );
        })}
      </ol>
      {current && (
        <div className="thinking">
          <div className="thinking-head">
            <span>思考过程</span>
            {thinking && (
              <button type="button" className="link-btn" onClick={() => setOpen((v) => !v)}>
                {open ? "收起" : "展开"}
              </button>
            )}
          </div>
          {!thinking ? (
            <p className="field-hint">{current === "gaps"
              ? "快速判断中，这一步不展开思考，通常几秒钟。"
              : "等待模型返回…（有的模型不公开思考过程，这时只显示步骤和时间）"}</p>
          ) : open && (
            <div className="thinking-box" ref={box}>{thinking}</div>
          )}
        </div>
      )}
      {!current && <p className="field-hint">正在连接规划模型… 已 {seconds(now - progress.startedAt)}</p>}
    </section>
  );
}

/** 规划结束后：一行可展开的“查看规划时的思考”。 */
export function ThinkingReview({ text }: { text: string }) {
  if (!text) return null;
  return (
    <details className="disclosure thinking-review">
      <summary>查看规划时的思考（{text.length} 字）</summary>
      <div className="thinking-box is-static">{text}</div>
    </details>
  );
}
