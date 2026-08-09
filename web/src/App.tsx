import { useEffect, useRef, useState } from "react";
import { ApiError, fetchNdjson, getJSON, openStream, postJSON, replay } from "./api";
import { FlowPane } from "./components/FlowPane";
import { FlowRail } from "./components/FlowRail";
import { Icon } from "./components/Icon";
import { NewResearch } from "./components/NewResearch";
import { PlanCard, type ResearchPlan, type PlanTrace } from "./components/PlanCard";
import { ResultBanner } from "./components/ResultPane";
import { Sidebar } from "./components/Sidebar";
import { TelemetryHeader, type ConfigPayload } from "./components/TelemetryHeader";
import { initialView, reduce } from "./reducer";
import type { RunView } from "./types";
import "./styles.css";

// /api/config 尚未拉回时的兜底默认，避免油表短暂以 max=0 渲染退化成一格都没有；
// 数字对应 STATUS.md 记录的当前后端默认值，仅在加载间隙生效，真值一到就被覆盖。
const DEFAULT_MAX_TOOL_CALLS = 12;
// 记住"当前活跃 run"以便刷新页面重连（补发 backlog 不丢现场）；run 收到 end 即清除，
// 不做跨天持久化——看历史已完成的 run 是 Task 16 Sidebar 的职责，不是这把钥匙的。
const ACTIVE_RUN_KEY = "dra_active_run_id";

function App() {
  const [view, setView] = useState<RunView>(initialView());
  // 默认「简洁」(clean)：第一眼是干净摘要，不糊屏；想看遥测细节一键切「调试」。
  // 原始 tool I/O 只在 debug 档出现，且已收进可折叠限高块（见 ProcessPane 的 hand-detail）。
  const [mode, setMode] = useState<"debug" | "clean">(
    (localStorage.getItem("dra_mode") as "debug" | "clean") ?? "clean"
  );
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  // atMs 统一用事件自带的服务端时间戳 e.ts*1000，live/回放/demo 三路来源都一样——
  // 不用 Date.now()：刷新重连时补发的 backlog 是一批历史事件，若按"收到的这一刻"
  // 打点，startedAtMs 会被记成重连时刻，早前流逝的时间全部消失，秒表从 0 重新走
  // （已实测踩过这个坑，与 fixture 用真实时钟相减产生荒谬差值是同一类问题）。
  // nowMs 必须跟着 dispatch 走同一参照系，不能独立打点。
  const [nowMs, setNowMs] = useState(0);

  // HITL 前置流程：runId 出现前，plan===null 显示 NewResearch，否则显示 PlanCard；
  // runId 一出现两者都收起，ProcessPane/TelemetryHeader 接管（本节点是 Task 14 新增）。
  const [plan, setPlan] = useState<ResearchPlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  /** 规划失败时仍保留 trace（成功时 trace 挂在 plan 上）；调试档展示 llm I/O。 */
  const [planFailTrace, setPlanFailTrace] = useState<PlanTrace | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [reviseHint, setReviseHint] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [streamDropped, setStreamDropped] = useState(false);
  // isLive 区分"正连着真实 SSE"和"在放某条历史 run 的回放"——两者都会把 runId
  // 填成非 null，但只有前者能取消、只有前者要写 localStorage 供刷新重连。
  const [isLive, setIsLive] = useState(false);
  // 每次真正有 run 收尾（收到 end）就 +1，Sidebar 拿它当 useEffect 依赖去重拉 /api/runs。
  const [runsVersion, setRunsVersion] = useState(0);
  // 原始问题原文：确认计划后才随 /api/research 一起送出，
  // 用 ref 存而非 state——纯粹是"记一下待用"，变了不需要触发重渲染。
  const queryRef = useRef("");
  const stopStreamRef = useRef<(() => void) | null>(null);

  const dispatch = (e: any, atMs: number) => {
    setStreamDropped(false);
    setNowMs(atMs);
    setView((v) => reduce(v, e, atMs));
    if (e.type === "end") {
      localStorage.removeItem(ACTIVE_RUN_KEY);
      setRunsVersion((v) => v + 1);
    }
  };

  // 建流 + 记住 runId：handleStart（新起 run）、挂载时重连（刷新页面）、Sidebar 点"运行中"
  // 条目（中途加入活流）共用同一份逻辑，保证三条路径行为不会悄悄分叉。
  function connectToRun(id: string) {
    stopStreamRef.current?.();   // 先关掉可能还开着的旧连接/回放，避免两路事件一起灌进 view
    setIsLive(true);
    setRunId(id);
    localStorage.setItem(ACTIVE_RUN_KEY, id);
    stopStreamRef.current = openStream(
      id, (e) => dispatch(e, e.ts * 1000), () => setStreamDropped(true)
    );
  }

  // Sidebar 点已结束的历史条目：从头拉完整事件日志重放（instant=瞬间重现终态，
  // paced=按原时序快放）；不是 live，不写 localStorage、取消按钮也不使能。
  async function viewHistoricalRun(id: string, replayMode: "instant" | "paced") {
    stopStreamRef.current?.();
    setIsLive(false);
    setRunId(id);
    setView(initialView());
    setNowMs(0);
    const events = await fetchNdjson(`/api/runs/${id}/events`);
    const { stop } = replay(events, dispatch, { mode: replayMode, targetMs: 45000 });
    stopStreamRef.current = stop;
  }

  useEffect(() => {
    getJSON<ConfigPayload>("/api/config").then(setConfig);
  }, []);

  useEffect(() => {
    localStorage.setItem("dra_mode", mode);
  }, [mode]);

  // 挂载时若有上次留下的活跃 run（刷新页面/HMR 重挂载），先核对事件协议再重连。
  // 旧协议只保留为审计证据，不允许送进当前 reducer。
  useEffect(() => {
    const saved = localStorage.getItem(ACTIVE_RUN_KEY);
    if (saved) {
      getJSON<{ meta: { replay_compatible?: boolean; replay_error?: string | null } }>(`/api/runs/${saved}`)
        .then(({ meta }) => {
          if (meta.replay_compatible === true) connectToRun(saved);
          else {
            localStorage.removeItem(ACTIVE_RUN_KEY);
            setPlanError(meta.replay_error ?? "旧版本、不可回放");
          }
        })
        .catch(() => localStorage.removeItem(ACTIVE_RUN_KEY));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 卸载时关掉还开着的 SSE 连接，避免页面切走后连接悬空重连
  useEffect(() => () => stopStreamRef.current?.(), []);

  const running = view.startedAtMs !== undefined && view.endedAtMs === undefined;
  useEffect(() => {
    if (!running) return;
    // 事件之间补间的"平滑走字"：往当前参照系上加真实流逝的 1s，不跳到 Date.now()——
    // 下一个真实事件一来，dispatch 会把 nowMs 纠正回权威值，这里最多漂移 1s。
    const id = setInterval(() => setNowMs((prev) => prev + 1000), 1000);
    return () => clearInterval(id);
  }, [running]);

  function runDemo() {
    fetchNdjson("/api/demo/events").then((es) => {
      stopStreamRef.current?.();
      setIsLive(false);
      const { stop } = replay(es, dispatch, { mode: "paced", targetMs: 45000 });
      stopStreamRef.current = stop;
    });
  }

  async function handlePlanSubmit(q: string) {
    setPlanning(true);
    setPlanError(null);
    setPlanFailTrace(null);
    try {
      const p = await postJSON<ResearchPlan>("/api/plan", {
        query: q,
        trace_full_llm_io: mode === "debug",
      });
      queryRef.current = q;
      setPlan(p);
      setPlanFailTrace(null);
    } catch (e) {
      setPlanError(e instanceof Error ? e.message : String(e));
      if (e instanceof ApiError && e.trace) setPlanFailTrace(e.trace);
    } finally {
      setPlanning(false);
    }
  }

  async function handleRevise(feedback: string) {
    setReviseHint(null);
    setSubmitting(true);   // 修订飞行中也要挡住"开始研究"/"取消"，跟 handleStart 共用同一个忙态
    try {
      const newPlan = await postJSON<ResearchPlan>("/api/plan/revise", {
        plan,
        feedback,
        trace_full_llm_io: mode === "debug",
      });
      // 比较时忽略 trace（每次修订 trace.plan_id 都变）；计划本体字段顺序恒定。
      const strip = ({ trace: _t, ...rest }: ResearchPlan) => rest;
      if (JSON.stringify(strip(newPlan)) === JSON.stringify(strip(plan!))) {
        setReviseHint("计划未变化，试试换一种说法");
        // 仍更新 trace，方便调试看本次修订调用
        setPlan(newPlan);
        return;
      }
      setPlan(newPlan);
    } finally {
      setSubmitting(false);
    }
  }

  function handleDiscardPlan() {
    setPlan(null);
    setReviseHint(null);
    setPlanError(null);
    setPlanFailTrace(null);
    setStartError(null);
  }

  async function handleStart() {
    if (!plan || submitting) return;
    setSubmitting(true);
    setStartError(null);
    try {
      // 执行契约不吃 trace；后端也会 strip，这里前端先剥掉避免多余载荷。
      const { trace: _trace, ...planBody } = plan;
      const { run_id } = await postJSON<{ run_id: string }>("/api/research", {
        query: queryRef.current,
        plan: planBody,
        trace_full_llm_io: mode === "debug",
      });
      connectToRun(run_id);
    } catch (e) {
      setStartError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleCancelRun() {
    // 确认交互在 TelemetryHeader 的二段按钮上完成,这里不再弹原生 confirm
    if (!runId) return;
    try {
      await postJSON(`/api/research/${runId}/cancel`, {});
    } catch (e) {
      // 协作式取消：请求本身几乎不会失败（服务端对已结束/不存在的 run 都返回状态而非报错），
      // 真出错也无需打断用户——cancelled/done banner 最终以 SSE 事件为准。
      console.error("cancel 请求失败", e);
    }
  }

  // runId 只在 connectToRun 里置位，从不自动清空——run 结束后必须有个显式动作回到
  // 空白输入态，否则整页永远卡在这次的 ProcessPane/banner，演示按钮也永久禁用
  // （这条路径正是 Task 14 冒烟脚本"再跑一次"要求踩到的，不是可以往后拖的美化项）。
  function startNewResearch() {
    stopStreamRef.current?.();
    setRunId(null);
    setIsLive(false);
    setView(initialView());
    setNowMs(0);
    setPlan(null);
    setPlanError(null);
    setPlanFailTrace(null);
    setStartError(null);
    setReviseHint(null);
    setStreamDropped(false);
  }

  // hasContent = 有 run/demo/回放的内容可展示（runId 存在，或 view 已开始——demo 不置 runId）。
  // 无内容 = 真·空闲态，显示新研究/计划卡表单；有内容 = 单条编年研究流 + 右侧锚点仪表。
  const hasContent = runId !== null || view.startedAtMs !== undefined;

  return (
    <div className="app-shell">
      <nav className="sidebar">
        <div className="brand">
          <span className="brand-mark"><Icon name="brand" size={16} /></span>
          <span className="brand-text">DRA 研究工作台<small>DEEP RESEARCH</small></span>
        </div>
        <button className="demo-btn" disabled={hasContent} onClick={runDemo}>
          <Icon name="play" size={15} />演示
        </button>
        {hasContent && !running && (
          <button className="newrun-btn" onClick={startNewResearch}>
            <Icon name="restart" size={15} />新研究
          </button>
        )}
        <Sidebar
          refreshSignal={runsVersion} currentRunId={runId}
          onSelectLive={connectToRun} onSelectReplay={viewHistoricalRun}
        />
      </nav>
      <main className="workbench">
        <TelemetryHeader
          key={runId ?? "none"}
          view={view} nowMs={nowMs} mode={mode} onMode={setMode}
          onCancel={handleCancelRun} canCancel={running && runId !== null && isLive}
          config={config} onConfigChange={setConfig}
        />
        {streamDropped && <div className="conn-warn"><Icon name="warning" />连接中断，浏览器正在自动重连…</div>}
        {!hasContent ? (
          <div className="stage-main">
            {plan === null ? (
              <NewResearch
                busy={planning} error={planError} config={config}
                mode={mode} planTrace={planFailTrace}
                onSubmit={handlePlanSubmit}
              />
            ) : (
              <div className="stage-form">
                <PlanCard
                  plan={plan} busy={submitting} hint={reviseHint} error={startError}
                  mode={mode}
                  onStart={handleStart} onRevise={handleRevise} onCancel={handleDiscardPlan}
                />
              </div>
            )}
          </div>
        ) : (
          <>
            {view.banner && <div className="stage-banner"><ResultBanner view={view} /></div>}
            <div className="stage-main">
              <div className="flow-shell">
                <FlowPane
                  key={runId ?? "demo"}
                  view={view} mode={mode} nowMs={nowMs}
                  maxToolCalls={config?.max_tool_calls ?? DEFAULT_MAX_TOOL_CALLS}
                />
                <FlowRail view={view} nowMs={nowMs} />
              </div>
            </div>
          </>
        )}
      </main>
    </div>
  );
}

export default App;
