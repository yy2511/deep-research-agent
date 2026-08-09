import type { RunView } from "../types";
import { stepLabel, toolLabel } from "../displayLabels";

// 历史/回看模式喂的统计快照——形状对应 runstore.aggregate_stats 的输出
// （snake_case + 预排序数组），跟 live 模式的 view.stats（camelCase + Record）
// 是两种不同的口径，llmRows/stepRows/toolRows 负责统一成同一套渲染用的行结构。
export interface MetaStats {
  llm: Array<{ step: string; model: string; calls: number; total_ms: number; in_tok: number; out_tok: number }>;
  steps: Array<{ step: string; calls: number; total_ms: number }>;
  tools: Array<{ tool: string; calls: number; ok: number; accepted: number; rejected: number }>;
  wall_s: number;
  agg_s: number;
}

interface LlmRow { step: string; model: string; calls: number; totalMs: number; inTok: number; outTok: number }
interface StepRow { step: string; calls: number; totalMs: number }
interface ToolRow { tool: string; calls: number; ok: number; accepted: number; rejected: number }

function llmRows(view: RunView, metaStats?: MetaStats): LlmRow[] {
  const rows: LlmRow[] = metaStats
    ? metaStats.llm.map((r) => ({ step: r.step, model: r.model, calls: r.calls,
        totalMs: r.total_ms, inTok: r.in_tok, outTok: r.out_tok }))
    : Object.values(view.stats.llm);
  return [...rows].sort((a, b) => b.totalMs - a.totalMs);
}
function stepRows(view: RunView, metaStats?: MetaStats): StepRow[] {
  const rows: StepRow[] = metaStats
    ? metaStats.steps.map((r) => ({ step: r.step, calls: r.calls, totalMs: r.total_ms }))
    : Object.entries(view.stats.steps).map(([step, r]) => ({ step, ...r }));
  return [...rows].sort((a, b) => b.totalMs - a.totalMs);
}
function toolRows(view: RunView, metaStats?: MetaStats): ToolRow[] {
  const rows: ToolRow[] = metaStats
    ? metaStats.tools.map((r) => ({ tool: r.tool, calls: r.calls, ok: r.ok,
        accepted: r.accepted, rejected: r.rejected }))
    : Object.entries(view.stats.tools).map(([tool, r]) => ({ tool, ...r }));
  return [...rows].sort((a, b) => b.calls - a.calls);
}
function wallSeconds(view: RunView, nowMs: number, metaStats?: MetaStats): number {
  if (metaStats) return metaStats.wall_s;
  if (view.startedAtMs === undefined) return 0;
  return ((view.endedAtMs ?? nowMs) - view.startedAtMs) / 1000;
}
function aggSeconds(view: RunView, metaStats?: MetaStats): number {
  if (metaStats) return metaStats.agg_s;
  return Object.values(view.stats.steps).reduce((sum, r) => sum + r.totalMs, 0) / 1000;
}
function fmtMs(ms: number): string {
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}
function fmtS(s: number): string {
  return `${s.toFixed(1)}s`;
}

export function ResultBanner({ view }: { view: RunView }) {
  if (!view.banner) return null;
  return (
    <div className={`result-banner result-banner-${view.banner.kind}`}>{view.banner.text}</div>
  );
}

export function StatsView({ view, nowMs, metaStats }: { view: RunView; nowMs: number; metaStats?: MetaStats }) {
  const llm = llmRows(view, metaStats);
  const steps = stepRows(view, metaStats);
  const tools = toolRows(view, metaStats);
  const wall = wallSeconds(view, nowMs, metaStats);
  const agg = aggSeconds(view, metaStats);
  return (
    <div className="stats-tab">
      <div className="stats-metrics">
        <div className="stats-metric"><span className="stats-metric-label">总耗时</span>
          <span className="num stats-metric-value">{fmtS(wall)}</span></div>
        <div className="stats-metric"><span className="stats-metric-label">累计步骤耗时</span>
          <span className="num stats-metric-value">{fmtS(agg)}</span></div>
      </div>
      <div className="stats-table-wrap">
        <div className="stats-table-title">模型调用明细</div>
        <table className="stats-table">
          <thead><tr><th>步骤</th><th>模型</th><th>调用</th><th>总耗时</th><th>平均耗时</th><th>输入 tok</th><th>输出 tok</th></tr></thead>
          <tbody>
            {llm.map((r, i) => (
              <tr key={i}>
                <td>{stepLabel(r.step)}</td><td>{r.model}</td>
                <td className="num">{r.calls}</td>
                <td className="num">{fmtMs(r.totalMs)}</td>
                <td className="num">{fmtMs(r.totalMs / Math.max(1, r.calls))}</td>
                <td className="num">{r.inTok}</td><td className="num">{r.outTok}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="stats-table-wrap">
        <div className="stats-table-title">步骤耗时</div>
        <table className="stats-table">
          <thead><tr><th>步骤</th><th>次数</th><th>累计</th></tr></thead>
          <tbody>
            {steps.map((r, i) => (
              <tr key={i}><td>{stepLabel(r.step)}</td><td className="num">{r.calls}</td><td className="num">{fmtMs(r.totalMs)}</td></tr>
            ))}
          </tbody>
        </table>
      </div>
      {tools.length > 0 && (
        <div className="stats-table-wrap">
          <div className="stats-table-title">工具调用统计</div>
          <table className="stats-table">
            <thead><tr><th>工具</th><th>次数</th><th>成功</th><th>收录</th><th>拒收</th></tr></thead>
            <tbody>
              {tools.map((r, i) => (
                <tr key={i}>
                  <td>{toolLabel(r.tool)}</td><td className="num">{r.calls}</td><td className="num">{r.ok}</td>
                  <td className="num">{r.accepted}</td><td className="num">{r.rejected}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
