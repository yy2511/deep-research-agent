import type { LlmCall } from "../types";
import { stepLabel } from "../displayLabels";
import { Icon } from "./Icon";

function retryReason(reason: string): string {
  switch (reason) {
    case "empty": return "模型返回空内容，无法解析结构化输出";
    case "parse": return "模型输出不是合法 JSON";
    case "schema": return "模型输出未通过字段结构校验";
    default: return `结构化输出校验失败：${reason}`;
  }
}

/** 调试档通用 LLM I/O 列表（研究流内联 / 子代理卡 / 计划确认页规划 trace 共用）。 */
export function LlmCallsList({ calls }: { calls: LlmCall[] }) {
  if (calls.length === 0) return null;
  return (
    <div className="llm-calls">
      {calls.map((c, i) => {
        const outputRecorded = c.output !== undefined;
        const outputEmpty = outputRecorded && c.output === "";
        const hasRetry = (c.retries?.length ?? 0) > 0;
        const ioState = outputEmpty ? "空输出" : c.ioComplete ? "完整 I/O" : "已截断";
        const ioClass = outputEmpty ? "llm-io-empty" : c.ioComplete ? "llm-io-full" : "llm-io-truncated";
        return (
          <details className="llm-call" key={i} open={outputEmpty || hasRetry}>
            <summary>
              <Icon name="chevron-right" className="summary-chevron" size={13} />
              {stepLabel(c.step)} · <span className="num">{c.ms}ms</span>
              {c.model && <> · {c.model}</>}
              {c.workerIteration !== undefined && <> · 执行器第 {c.workerIteration} 次工具决策</>}
              {(c.inTok !== undefined || c.outTok !== undefined) && (
                <> · <span className="num">↑{c.inTok ?? 0} ↓{c.outTok ?? 0}</span></>
              )}
              <span className={`llm-io-state ${ioClass}`}>
                {ioState}
              </span>
            </summary>
            {c.input && (
              <div className="llm-io-block">
                <span className="llm-io-tag">输入</span>
                <pre className="llm-io">{c.input}</pre>
              </div>
            )}
            {outputRecorded && (
              <div className="llm-io-block">
                <span className="llm-io-tag">输出</span>
                <pre className={`llm-io${outputEmpty ? " llm-io-empty-body" : ""}`}>
                  {outputEmpty
                    ? `（空输出：本次计入 ${c.outTok ?? 0} 个输出 token，但后端未收到可见正文）`
                    : c.output}
                </pre>
              </div>
            )}
            {(c.retries ?? []).map((retry, retryIndex) => (
              <div className="llm-io-block llm-io-error" key={retryIndex}>
                <span className="llm-io-tag">错误原因</span>
                <div className="llm-io-diagnostic">
                  {retryReason(retry.reason)}；第 {retry.attempt} 次尝试
                  {retry.exhausted ? "，重试已耗尽" : "，系统将重试"}。
                </div>
                {retry.rawHead && <pre className="llm-io">{retry.rawHead}</pre>}
              </div>
            ))}
          </details>
        );
      })}
    </div>
  );
}

/** 从计划 trace.events 抽出与研究流同结构的 LlmCall[]。 */
export function llmCallsFromPlanTrace(trace?: { events?: any[] } | null): LlmCall[] {
  if (!trace?.events?.length) return [];
  const calls: LlmCall[] = [];
  for (const e of trace.events) {
    if (e && e.type === "llm_call") {
      calls.push({
        step: e.step ?? "build_research_plan",
        model: e.model,
        ms: e.ms ?? 0,
        inTok: e.in_tok,
        outTok: e.out_tok,
        input: e.input,
        output: e.output,
        ioComplete: e.io_complete,
        sid: e.sid,
        workerIteration: e.worker_iteration,
        retries: [],
      });
      continue;
    }
    if (e && e.type === "json_retry" && calls.length > 0) {
      calls[calls.length - 1].retries!.push({
        reason: String(e.reason ?? "unknown"),
        attempt: Number(e.attempt ?? 0),
        exhausted: Boolean(e.exhausted),
        rawHead: typeof e.raw_head === "string" ? e.raw_head : undefined,
      });
    }
  }
  return calls;
}
