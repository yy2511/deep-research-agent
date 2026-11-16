import { useEffect, useRef, useState } from "react";
import {
  customRangeError, INITIAL_OPERATIONS, operationsQuery, OperationsLoader, presetDates, utcDate,
  type OperationsPreset, type OperationsSnapshot, type OperationsState,
} from "../operations";

const PRESETS: Array<[OperationsPreset, string]> = [
  ["today", "今日"], ["7", "近 7 日"], ["30", "近 30 日"], ["all", "全部"], ["custom", "自定义"],
];
const number = (value: number) => value.toLocaleString("zh-CN");

function Metric({ label, value, note }: { label: string; value: number; note: string }) {
  return <div className="operations-metric"><dt>{label}</dt><dd><span className="operations-metric-value">{number(value)}</span><span className="operations-metric-note">{note}</span></dd></div>;
}

/** 所有点都来自返回的 daily；完整数值仍可在下方表格中逐日核对。 */
export function OperationsTrend({ days }: { days: OperationsSnapshot["daily"] }) {
  const figure = useRef<HTMLElement | null>(null);
  const [width, setWidth] = useState(760);
  const hasDays = days.length > 0;
  useEffect(() => {
    const element = figure.current;
    if (!element) return;
    const updateWidth = (next: number) => { if (next > 0) setWidth(next); };
    const measure = () => updateWidth(element.getBoundingClientRect().width);
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(entries => {
      const entry = entries[0];
      if (entry) updateWidth(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [hasDays]);
  const ordered = [...days].sort((a, b) => a.date.localeCompare(b.date));
  if (!ordered.length) return <p className="operations-chart-empty">所选期间暂无每日记录。</p>;
  const maximum = ordered.reduce((max, day) => Math.max(max, day.research_tasks, day.research_users), 0);
  const scale = maximum || 1;
  // Match SVG units to CSS pixels so a narrow panel never scales down its 11px labels.
  // Reserve enough space for the full formatted count, including grouping separators.
  const height = 230, left = Math.max(42, number(scale).length * 7 + 16), right = 16, top = 20, bottom = 34;
  const plotWidth = Math.max(0, width - left - right), plotHeight = height - top - bottom;
  const x = (index: number) => left + (ordered.length === 1 ? plotWidth / 2 : index / (ordered.length - 1) * plotWidth);
  const y = (value: number) => top + plotHeight * (1 - value / scale);
  const ticks = [...new Set([0, Math.ceil(scale / 2), scale])];
  const dateIndices = [...new Set([0, Math.floor((ordered.length - 1) / 2), ordered.length - 1])];
  const series = [{ key: "research_tasks" as const, label: "研究任务", className: "tasks" },
    { key: "research_users" as const, label: "研究用户", className: "users" }];
  return <figure className="operations-chart" ref={figure}>
    <div className="operations-chart-legend">{series.map(item => <span key={item.key}><i className={`operations-chart-key is-${item.className}`} />{item.label}</span>)}</div>
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby="operations-chart-title operations-chart-description">
      <title id="operations-chart-title">每日研究任务与研究用户（UTC）</title>
      <desc id="operations-chart-description">{ordered[0].date} 至 {ordered.at(-1)!.date}，共 {ordered.length} 天。最高每日计数 {number(maximum)}。完整数值见每日登记活动表。</desc>
      {ticks.map(value => <g key={value} className="operations-chart-grid"><line x1={left} x2={width - right} y1={y(value)} y2={y(value)} /><text x={left - 12} y={y(value) + 4} textAnchor="end">{number(value)}</text></g>)}
      {series.map(item => <g key={item.key} className={`operations-chart-series is-${item.className}`}>
        <polyline fill="none" points={ordered.map((day, index) => `${x(index)},${y(day[item.key])}`).join(" ")} />
        {ordered.length === 1 && <circle cx={x(0)} cy={y(ordered[0][item.key])} r="4" />}
      </g>)}
      {dateIndices.map(index => <text key={index} className="operations-chart-date" x={x(index)} y={height - 7} textAnchor={ordered.length === 1 ? "middle" : index === 0 ? "start" : index === ordered.length - 1 ? "end" : "middle"}>{ordered[index].date.slice(5)}</text>)}
    </svg>
    <figcaption>每日登记的研究活动 · UTC；任务包含首问与追问。</figcaption>
  </figure>;
}

/** 纯展示部分便于离线渲染验证，不接触用户身份或研究详情。 */
export function OperationsResults({ data }: { data: OperationsSnapshot }) {
  return <>
    <section className="operations-period" aria-labelledby="operations-period">
      <div className="operations-section-heading"><h3 id="operations-period">期间活动</h3><span className="operations-section-note">所选日期范围</span></div>
      <p className="operations-range">统计范围：{data.range.start_date} 至 {data.range.end_date}（UTC，包含两端日期）</p>
      <dl className="operations-metrics">
        <Metric label="研究任务" value={data.period.research_tasks} note="含首问与追问" />
        <Metric label="研究用户（去重）" value={data.period.research_users} note="至少登记过一条研究" />
        <Metric label="新增账号" value={data.period.new_users} note="期间新注册" />
        <Metric label="新建对话" value={data.period.conversations} note="期间建立的研究会话" />
      </dl>
      <p className="field-hint">研究任务不代表完成，也不等于每条聊天消息。</p>
    </section>
    <section className="operations-current" aria-labelledby="operations-totals">
      <div className="operations-section-heading"><h3 id="operations-totals">当前总览</h3><span className="operations-section-note">不受日期筛选影响</span></div>
      <dl className="operations-current-metrics">
        <div><dt>账号总数</dt><dd><span className="operations-metric-value">{number(data.totals.users)}</span></dd></div>
        <div><dt>可用账号</dt><dd><span className="operations-metric-value">{number(data.totals.enabled_users)}</span></dd></div>
        <div><dt>停用账号</dt><dd><span className="operations-metric-value">{number(data.totals.disabled_users)}</span></dd></div>
        <div><dt>公开示例</dt><dd><span className="operations-metric-value">{number(data.totals.showcases)}</span></dd></div>
      </dl>
      <p className="field-hint">访客 {number(data.totals.roles.guest)} / 用户 {number(data.totals.roles.user)} / 管理员 {number(data.totals.roles.admin)} / 平台负责人 {number(data.totals.roles.owner ?? 0)}。可用账号不代表近期活跃。</p>
    </section>
    <section className="operations-daily" aria-labelledby="operations-daily">
      <div className="operations-section-heading"><h3 id="operations-daily">每日趋势</h3><span className="operations-section-note">研究任务与研究用户</span></div>
      <OperationsTrend days={data.daily} />
      <p id="operations-daily-note" className="field-hint">每日研究用户数不能直接相加为期间去重数；没有活动的日期显示为 0。</p>
      <div className="operations-table-wrap" role="region" aria-label="每日趋势，可横向滚动" tabIndex={0}>
        <table className="operations-table" aria-describedby="operations-daily-note">
          <caption>每日登记活动（UTC，日期倒序）</caption>
          <thead><tr><th scope="col">日期（UTC）</th><th scope="col">新增账号</th><th scope="col">研究用户</th><th scope="col">研究任务</th><th scope="col">新建对话</th></tr></thead>
          <tbody>{data.daily.toReversed().map(day => <tr key={day.date}>
            <th scope="row">{day.date}</th><td>{number(day.new_users)}</td><td>{number(day.research_users)}</td><td>{number(day.research_tasks)}</td><td>{number(day.conversations)}</td>
          </tr>)}</tbody>
        </table>
      </div>
    </section>
  </>;
}

export function OperationsStatus({ state }: { state: OperationsState }) {
  return <>
    {state.loading && <p className="field-hint" role="status">{state.data ? "正在刷新统计…" : "正在加载统计…"}</p>}
    {state.error && <p className="operations-error" role="alert">
      {state.data ? "刷新失败，以下为旧数据。" : ""}{state.error}
    </p>}
    {state.data && <p className="field-hint operations-updated">最近成功获取：<time dateTime={state.data.as_of}>{new Date(state.data.as_of).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC")}</time></p>}
  </>;
}

export function OperationsDashboard() {
  const [state, setState] = useState<OperationsState>(INITIAL_OPERATIONS);
  const [preset, setPreset] = useState<OperationsPreset>("7");
  const [dates, setDates] = useState(() => presetDates(7));
  const [validation, setValidation] = useState<string | null>(null);
  const loader = useRef<OperationsLoader | null>(null);
  const applied = useRef({ preset: "7" as OperationsPreset, start: "", end: "" });
  useEffect(() => {
    const current = new OperationsLoader(setState);
    loader.current = current;
    void current.load("");
    return () => { current.dispose(); loader.current = null; };
  }, []);

  function load(next: OperationsPreset) {
    const error = next === "custom" ? customRangeError(dates.start, dates.end) : null;
    setValidation(error);
    if (!error) {
      applied.current = { preset: next, ...dates };
      void loader.current?.load(operationsQuery(next, dates.start, dates.end));
    }
  }

  function select(next: OperationsPreset) {
    setPreset(next);
    setValidation(null);
    if (next !== "custom") load(next);
  }

  return <div className="settings-section operations-dashboard">
    <div className="section-head operations-heading"><div><span className="settings-page-kicker">平台管理 / OVERVIEW</span><h2>运营概览</h2><p>了解平台的账号规模与研究使用情况。</p><span className="operations-privacy">仅管理员可见 · 仅展示汇总数据</span></div>
      <button type="button" className="btn btn-sm" disabled={state.loading} onClick={() => {
        const current = applied.current;
        void loader.current?.load(operationsQuery(current.preset, current.start, current.end));
      }}>刷新</button>
    </div>
    <form className="operations-filters" onSubmit={event => { event.preventDefault(); load(preset); }}>
      <label>时间范围（UTC）<select value={preset} onChange={event => select(event.target.value as OperationsPreset)}>
        {PRESETS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select></label>
      {preset === "custom" && <>
        <label>开始日期<input type="date" value={dates.start} max={utcDate()} onChange={event => setDates({ ...dates, start: event.target.value })} /></label>
        <label>结束日期<input type="date" value={dates.end} max={utcDate()} onChange={event => setDates({ ...dates, end: event.target.value })} /></label>
        <button className="btn btn-sm" type="submit">应用日期</button>
        <p className="field-hint operations-filter-hint">修改日期后点击“应用日期”；下方以返回的统计范围为准。</p>
      </>}
    </form>
    {validation && <p className="operations-error" role="alert">{validation}</p>}
    <OperationsStatus state={state} />
    <div aria-busy={state.loading}>{state.data && <OperationsResults data={state.data} />}</div>
    <p className="field-hint operations-scope">统计范围为数据库已登记记录，包含所有角色与停用账号；未登记的历史文件不计入。导入记录按其保存的历史日期和归属计数。不含私有提问、身份、答案或证据。</p>
  </div>;
}
