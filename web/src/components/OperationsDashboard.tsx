import { useEffect, useRef, useState } from "react";
import {
  customRangeError, INITIAL_OPERATIONS, operationsQuery, OperationsLoader, presetDates, utcDate,
  type OperationsPreset, type OperationsSnapshot, type OperationsState,
} from "../operations";

const PRESETS: Array<[OperationsPreset, string]> = [
  ["today", "今日"], ["7", "近 7 日"], ["30", "近 30 日"], ["all", "全部"], ["custom", "自定义"],
];
const number = (value: number) => value.toLocaleString("zh-CN");

function Metric({ label, value }: { label: string; value: number }) {
  return <div className="operations-metric"><dt>{label}</dt><dd>{number(value)}</dd></div>;
}

/** 纯展示部分便于离线渲染验证，不接触用户身份或研究详情。 */
export function OperationsResults({ data }: { data: OperationsSnapshot }) {
  return <>
    <section aria-labelledby="operations-totals">
      <h3 id="operations-totals">当前总览 <span className="field-hint">不受日期筛选影响</span></h3>
      <dl className="operations-metrics">
        <Metric label="账号总数" value={data.totals.users} />
        <Metric label="可用账号" value={data.totals.enabled_users} />
        <Metric label="停用账号" value={data.totals.disabled_users} />
        <Metric label="公开示例" value={data.totals.showcases} />
      </dl>
      <p className="field-hint">访客 {number(data.totals.roles.guest)} / 用户 {number(data.totals.roles.user)} / 管理员 {number(data.totals.roles.admin)} / 平台负责人 {number(data.totals.roles.owner ?? 0)}。可用账号不代表近期活跃。</p>
    </section>
    <section aria-labelledby="operations-period">
      <h3 id="operations-period">期间活动</h3>
      <p className="operations-range">统计范围：{data.range.start_date} 至 {data.range.end_date}（UTC，包含两端日期）</p>
      <dl className="operations-metrics">
        <Metric label="新增账号" value={data.period.new_users} />
        <Metric label="研究用户（去重）" value={data.period.research_users} />
        <Metric label="研究任务" value={data.period.research_tasks} />
        <Metric label="新建对话" value={data.period.conversations} />
      </dl>
      <p className="field-hint">研究用户是期间至少登记一条研究的去重账号。任务含首问与追问，不代表完成，也不等于每条聊天消息。</p>
    </section>
    <section aria-labelledby="operations-daily">
      <h3 id="operations-daily">每日趋势</h3>
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
    <div className="section-head operations-heading"><div><h2>运营概览</h2><p className="field-hint">仅管理员可见 · 仅展示汇总数据</p></div>
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
