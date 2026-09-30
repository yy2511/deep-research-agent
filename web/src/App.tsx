import { useEffect, useRef, useState } from "react";
import { ApiError, fetchNdjson, getJSON, openStream, postJSON, postStream, replay, type PlannerEvent } from "./api";
import { BriefCard, type ResearchBrief } from "./components/BriefCard";
import { ConversationWorkspace } from "./components/ConversationWorkspace";
import { GapCard, type BriefGap, type GapAnswer } from "./components/GapCard";
import { Icon } from "./components/Icon";
import { LibraryButton, SourcePicker, type SourceSelection } from "./components/LibraryPanel";
import { SkillPicker } from "./components/SkillsPanel";
import { NewResearch } from "./components/NewResearch";
import { PlanningProgress, ThinkingReview } from "./components/PlanningProgress";
import { applyPlannerEvent, startProgress, thinkingText, type PlanProgress, type PlanStage } from "./planProgress";
import { Sidebar } from "./components/Sidebar";
import { RunStatus } from "./components/RunStatus";
import { SettingsPage, type LocalPrefs, type SettingsTab } from "./components/SettingsPage";
import type { RunMeta, SimplePayload } from "./reducer";
import {
  BatchGuard, initialView, mergeSimpleResult, mergeResearchInput, missingProcessNotice, planOpen, reduce, viewTimes,
} from "./reducer";
import "./styles.css";
import type { RunView, ResearchInputSnapshot, ResearchUserMessage, ResearchInputState } from "./types";
import { type PlanTrace } from "./types";

// 刷新恢复"当前运行的"钥匙：运行中与已结束的历史都记，刷新后回到同一条 run。
// 与 URL 的 ?run= 互为备份（URL 便于分享/前进后退，localStorage 兼容旧会话）。
const ACTIVE_RUN_KEY = "dra_active_run_id";

/** 网站黑名单记在本机浏览器里，下次研究沿用。 */
const BLOCKED_SITES_KEY = "dra_blocked_sites";
function savedBlockedSites(): string[] {
  try {
    const saved = JSON.parse(localStorage.getItem(BLOCKED_SITES_KEY) ?? "[]");
    return Array.isArray(saved) ? saved.filter((s): s is string => typeof s === "string") : [];
  } catch {
    return [];
  }
}

/** 开发者选项只存在本机浏览器里。 */
function readPrefs(): LocalPrefs {
  return {
    showRecords: localStorage.getItem("dra_show_records") !== "false",
    showReplay: localStorage.getItem("dra_show_replay") === "true",
    traceFull: localStorage.getItem("dra_trace_full") === "true",
  };
}

/** 开跑前那两段的返回：要么先问（还没生成计划），要么直接给计划。 */
type BriefResponse =
  | { kind: "clarify"; gaps: BriefGap[]; trace?: PlanTrace }
  | { kind: "plan"; brief: ResearchBrief; trace?: PlanTrace };

type RunDetail = {
  meta?: RunMeta;
  report_md?: string;
  executed_tool_calls?: number;
  research_input?: ResearchInputSnapshot;
};

function App() {
  const [view, setView] = useState<RunView>(initialView());
  const [selectedSkills,setSelectedSkills] = useState<string[]>([]);
  const [sourceSelection,setSourceSelection] = useState<SourceSelection>(() => ({
    source_mode:'web', document_ids:[], library_rerank:false, blocked_sites:savedBlockedSites(),
  }));
  useEffect(() => {
    try { localStorage.setItem(BLOCKED_SITES_KEY, JSON.stringify(sourceSelection.blocked_sites ?? [])); } catch { /* 无痕模式等写不进去时，本次照常使用 */ }
  }, [sourceSelection.blocked_sites]);
  // 调试档（dra_mode=debug）会在开跑前的卡片里显示规划器的输入输出；界面上不再提供切换。
  const [mode] = useState<"debug" | "clean">(
    (localStorage.getItem("dra_mode") as "debug" | "clean") ?? "clean"
  );
  const [prefs, setPrefs] = useState<LocalPrefs>(readPrefs);
  const traceFull = prefs.traceFull;
  // 主区显示研究还是设置；设置页不打断正在进行的研究。
  const [page, setPage] = useState<"research" | "settings">("research");
  const [settingsTab, setSettingsTab] = useState<SettingsTab>("models");
  // atMs 统一用事件自带的服务端时间戳 e.ts*1000，live/回放/demo 三路来源都一样——
  // 不用 Date.now()：刷新重连时补发的 backlog 是一批历史事件，若按"收到的这一刻"
  // 打点，startedAtMs 会被记成重连时刻，早前流逝的时间全部消失，秒表从 0 重新走
  // nowMs 必须跟着 dispatch 走同一参照系，不能独立打点。
  const [nowMs, setNowMs] = useState(0);

  // 开跑前的两段（EXPERIMENT_PLAN §7 第 1 项）：gaps 非空 = 先澄清（这时计划还没生成），
  // 答完或跳过后才拿 brief；两者都为 null 才是输入态。runId 一出现全部收起，
  // 研究页接管。
  // 模型判"不需要确认"时不会在这里停——handleSimpleSubmit 直接把 brief 带进研究。
  const [gaps, setGaps] = useState<BriefGap[] | null>(null);
  const [brief, setBrief] = useState<ResearchBrief | null>(null);
  const [briefing, setBriefing] = useState(false);
  const [briefError, setBriefError] = useState<string | null>(null);
  const [briefFailTrace, setBriefFailTrace] = useState<PlanTrace | null>(null);
  const [briefHint, setBriefHint] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // 规划进行中的步骤与实时思考；结束后思考内容留在 planThinking 里供回看。
  const [planProgress, setPlanProgress] = useState<PlanProgress | null>(null);
  const [planThinking, setPlanThinking] = useState("");
  const planProgressRef = useRef<PlanProgress | null>(null);
  // 取消请求已发出、运行还没结束：按钮显示"取消中…"并禁用，直到本次运行收尾。
  const [cancelling, setCancelling] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [streamDropped, setStreamDropped] = useState(false);
  // isLive 区分"正连着真实 SSE"和"在放某条历史 run 的回放"——两者都会把 runId
  // 填成非 null，但只有前者能取消、只有前者要写 localStorage 供刷新重连。
  const [isLive, setIsLive] = useState(false);
  // 每次真正有 run 收尾（收到 end）就 +1，Sidebar 拿它当 useEffect 依赖去重拉 /api/runs。
  const [runsVersion, setRunsVersion] = useState(0);
  // 窄屏（≤900px）历史列表被 CSS 隐藏：给一个可展开入口，否则无法选历史/快放。
  const [historyOpen, setHistoryOpen] = useState(false);
  // 原始问题原文：确认计划后才随 /api/research 一起送出，
  // 用 ref 存而非 state——纯粹是"记一下待用"，变了不需要触发重渲染。
  /** 地址栏里的当前 run（轻量，不引入路由框架）：便于分享/前进后退/刷新回到同一条。 */
  function readRunParam(): string | null {
    try {
      return new URLSearchParams(window.location.search).get("run");
    } catch {
      return null;
    }
  }

  function writeRunParam(id: string | null) {
    try {
      const url = new URL(window.location.href);
      if (id) url.searchParams.set("run", id);
      else url.searchParams.delete("run");
      window.history.replaceState({}, "", url.toString());
    } catch {
      /* 地址栏不可用时忽略：localStorage 仍然记得当前 run */
    }
  }

  const queryRef = useRef("");
  const sourceRef = useRef(sourceSelection);
  const stopStreamRef = useRef<(() => void) | null>(null);
  // 每一次"选择/加载一条运行"都是一个批次：旧批次的结果、旧 SSE 回调一律不得提交。
  // 只比较 run id 不够——A→B→A 时两次 A 的 id 相同，但视图基底已经换过。
  const batchRef = useRef(new BatchGuard());
  // dispatch 会被 SSE 回调长期持有，闭包里读 state 会拿到旧值；当前 run id 走 ref。
  const runIdRef = useRef<string | null>(null);

  const dispatch = (e: any, atMs: number) => {
    setStreamDropped(false);
    setNowMs(atMs);
    setView(v => reduce(v, e, atMs));
    if (e.type === "end") {
      setCancelling(false);
      localStorage.removeItem(ACTIVE_RUN_KEY);
      setRunsVersion((v) => v + 1);
    }
  };

  // 建流 + 记住 runId：handleStart（新起 run）、挂载时重连（刷新页面）、Sidebar 点"运行中"
  // 条目（中途加入活流）共用同一份逻辑，保证三条路径行为不会悄悄分叉。
  useEffect(() => {
    runIdRef.current = runId;
  }, [runId]);

  function connectToRun(id: string) {
    const batch = batchRef.current.begin(id);
    setCancelling(false);
    setStreamDropped(false);
    runIdRef.current = id;
    stopStreamRef.current?.();   // 先关掉可能还开着的旧连接/回放，避免两路事件一起灌进 view
    setIsLive(true);
    // 新运行的模式未知：先清掉上一条运行的结论，随后由它自己的元数据/事件填上。
    const fresh = initialView();
    setView(fresh);
    setRunId(id);
    localStorage.setItem(ACTIVE_RUN_KEY, id);
    writeRunParam(id);
    // 不阻塞建流：迟到就丢弃（元数据与事件都是这条运行的，批次一致才采用）。
    getJSON<{ meta?: RunMeta; research_input?: ResearchInputSnapshot }>(`/api/runs/${id}`)
      .then(({ research_input }) => {
        if (!batchRef.current.isCurrent(batch, id)) return;
        // 活流的消息位置由 SSE 决定；元数据只补接收开关，不把用户消息提前排到工具之前。
        if (research_input) setView(v => mergeResearchInput(v, { ...research_input, messages: [] }));
      })
      .catch(() => { /* 元数据拿不到不影响研究；模式仍可由事件判定 */ });
    // 闭包捕获本次 batch；已关闭的旧流仍可能有排队回调。
    // A→B→A 时最新批次也是 A，读可变引用会让旧 A 的回调重新"通过校验"。
    stopStreamRef.current = openStream(
      id,
      (e) => {
        if (!batchRef.current.isCurrent(batch, id)) return;
        setStreamDropped(false);  // EventSource 自动重连补流后恢复输入能力。
        dispatch(e, e.ts * 1000);
        // 活流结束时补完整结果；历史回放由 onDone 统一加载。
        if (e.type === "end") void applySimpleResult(id, { live: true });
      },
      () => {
        if (batchRef.current.isCurrent(batch, id)) setStreamDropped(true);
      },
    );
  }

  // Sidebar 点已结束的历史条目：从头拉完整事件日志重放（instant=瞬间重现终态，
  // paced=按原时序快放）；不是 live，不写 localStorage、取消按钮也不使能。
  /** 读 simple 结果并**合并**进当前视图（结果缺失时保留已有过程）。 */
  async function applySimpleResult(id: string, opts?: { live?: boolean; detail?: RunDetail | null }): Promise<boolean> {
    // 同一次导航的第二步：复用当前批次，避免把上一段（事件回放）的派发作废。
    const batch = batchRef.current.currentOrBegin(id);
    let payload: SimplePayload;
    let detail: RunDetail | null;
    try {
      payload = await getJSON<SimplePayload>(`/api/runs/${id}/simple`);
      detail = opts?.detail !== undefined ? opts.detail
        : await getJSON<RunDetail>(`/api/runs/${id}`).catch(() => null);
    } catch {
      return false;
    }
    // 两次 await 之后可能已经切到别的运行：整份结果直接丢弃，绝不覆盖当前选择。
    if (!batchRef.current.isCurrent(batch, id)) return false;
    if (!opts?.live) {
      // 历史/刷新进入：切到自己这条运行上，别让引用/快照链接沿用上一个 run 的 id。
      stopStreamRef.current?.();
      setIsLive(false);
      setRunId(id);
      runIdRef.current = id;
      localStorage.setItem(ACTIVE_RUN_KEY, id);
      writeRunParam(id);
    }
    setView((current) => {
      if (!batchRef.current.isCurrent(batch, id)) return current;
      // live：视图里就是这条 run 的过程，直接合并（结束那一刻过程不能消失）。
      // 非 live：只有视图确实属于这条 run 才保留过程，否则用新基底——绝不把上一条
      // run 的过程并进这条 run 的结果（切换的中间态曾让 runIdRef 提前等于新 id）。
      const sameRun = batchRef.current.runId === id && runIdRef.current === id;
      const base = opts?.live || sameRun ? current : initialView();
      const merged = mergeSimpleResult(base, payload, detail?.meta ?? null, {
        executedToolCalls: detail?.executed_tool_calls,
        preserveEndedAt: Boolean(opts?.live),
      });
      return detail?.research_input ? mergeResearchInput(merged, detail.research_input) : merged;
    });
    return true;
  }

  async function sendResearchMessage(messageId: string, text: string) {
    const id = runId;
    if (!id || !isLive || runIdRef.current !== id) throw new Error('当前研究不能接收消息');
    const batch = batchRef.current.currentOrBegin(id);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const receipt = await postJSON<{message:ResearchUserMessage;state:ResearchInputState}>(
        `/api/research/${id}/messages`, {message_id:messageId,text}, controller.signal);
      if (!batchRef.current.isCurrent(batch, id)) return;
      setView(v => batchRef.current.isCurrent(batch, id)
        ? mergeResearchInput(v, {state:receipt.state,messages:[receipt.message]}) : v);
    } catch (error) {
      if (!batchRef.current.isCurrent(batch, id)) return;
      if (controller.signal.aborted) throw new Error('暂未确认发送结果，可重试同一条消息');
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  /**
   * 打开一条历史运行：**先按真实事件推进**，最后再把结果合并进同一视图。
   *
   * 顺序不能反：先装最终结果会让"快放"直接从终态开始，过程也再回不来。
   * 事件不可用（旧运行没有 events.jsonl、或旧协议不可回放）时，仍然打开已有结果，
   * 并明确说明没有过程记录——不能因为 404 让整条历史打不开，也不伪造过程。
   */
  async function viewHistoricalRun(id: string, replayMode: "instant" | "paced") {
    const batch = batchRef.current.begin(id);
    stopStreamRef.current?.();
    setIsLive(false);
    setRunId(id);
    runIdRef.current = id;
    localStorage.setItem(ACTIVE_RUN_KEY, id);
    writeRunParam(id);
    setNowMs(0);
    // 选择那一刻就换基底：旧 run 的报告/来源不能在新 run 的 id 下短暂可见
    // （来源链接会拼成新 run 的路径却指向旧材料）。结果在放完之后再合并进这份基底。
    setView(initialView());

    let detail: RunDetail | null = null;
    try {
      detail = await getJSON(`/api/runs/${id}`);
    } catch { /* meta 拿不到也继续：至少把结果打开 */ }
    if (!batchRef.current.isCurrent(batch, id)) return;
    const meta = detail?.meta ?? null;
    // 事件优先：能回放就一定是"从过程开始"，而不是先摆终稿。
    let events: any[] | null = null;
    if (meta?.replay_compatible !== false) {
      try {
        const rows = await fetchNdjson(`/api/runs/${id}/events?compact=true`);
        if (!batchRef.current.isCurrent(batch, id)) return;
        if (rows.length > 0) events = rows;
      } catch { /* 404/不可回放：走结果兜底 */ }
    }
    const plan = planOpen(id, meta, { hasEvents: events !== null });
    if (!batchRef.current.isCurrent(batch, id)) return;

    /** 结果只在过程放完之后合并：快放不能一开始就摆出终稿。 */
    const mergeResult = async () => {
      const simpleOk = await applySimpleResult(id, { live: false, detail });
      if (!simpleOk && detail) {
        if (!batchRef.current.isCurrent(batch, id)) return;
        setView((v) => {
          const merged: RunView = {
            ...v,
            stage: "done",
            ...viewTimes(meta),
            reportMd: v.reportMd ?? detail?.report_md,
            banner: v.banner ?? (detail?.report_md
              ? { kind: "unknown", text: "历史报告 · 本次运行没有可回放的过程记录" }
              : { kind: "unknown", text: "该历史运行未生成报告。" }),
          };
          return detail?.research_input ? mergeResearchInput(merged, detail.research_input) : merged;
        });
      }
      if (plan.kind === "result-only") {
        setView((v) => missingProcessNotice(v, { reason: plan.reason }));
      }
    };

    if (plan.kind === "replay" && events) {
      // 保留这条运行的模式：第一条 research_status 到达之前也要用对主视图。
      setView({ ...initialView(), replayOnly: replayMode==='paced' });
      const { stop } = replay(events, (e, atMs) => {
        if (!batchRef.current.isCurrent(batch, id)) return;
        dispatch(e, atMs);
      }, {
        mode: replayMode,
        targetMs: 45000,
        // 快放走完最后一条事件后再补结果；instant 同步放完，效果等同立即合并。
        onDone: () => { void mergeResult(); },
      });
      stopStreamRef.current = stop;
      if (meta?.status === "interrupted") {
        setView((v) => ({ ...v, endedAtMs: (meta.finished_at ?? Date.now() / 1000) * 1000,
          banner: { kind: "unknown", text: "研究中断，已提交结果保留。可从历史列表继续研究。" } }));
      }
      return;
    }

    // 没有可回放事件：清空旧 run 的过程，按已有结果兜底打开（并说明没有过程记录）。
    setView({ ...initialView(), replayOnly: replayMode==='paced' });
    await mergeResult();
  }

  // 刷新/重挂载恢复"当前运行的"那条 run：显式 URL 优先（分享/前进后退/直接粘贴），
  // localStorage 只是旧会话兜底。运行中重连 SSE，已结束的按事件回放并合并结果；
  // 走哪条路由 planOpen 按该 run 自己的 meta 判定，与"下次新建研究"的模式设置无关。
  // 恢复本身也是异步的：期间用户点了别的运行，这次恢复必须整条放弃（批次防护）。
  useEffect(() => {
    const fromUrl = readRunParam();
    const saved = fromUrl ?? localStorage.getItem(ACTIVE_RUN_KEY);
    if (!saved) return;
    if (!fromUrl) writeRunParam(saved);
    const batch = batchRef.current.begin(saved);
    getJSON<{ meta?: RunMeta }>(`/api/runs/${saved}`)
      .then(({ meta }) => {
        if (!batchRef.current.isCurrent(batch, saved)) return;
        const plan = planOpen(saved, meta ?? null);
        if (plan.kind === "live") connectToRun(saved);
        // 旧协议不可回放、或本次没有事件记录：照样打开已有结果，并说明没有过程记录。
        else void viewHistoricalRun(saved, "instant");
      })
      .catch(() => {
        if (!batchRef.current.isCurrent(batch, saved)) return;
        localStorage.removeItem(ACTIVE_RUN_KEY);
        if (readRunParam() === saved) writeRunParam(null);
      });
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
    // demo 也是一次"选择"：领批次，迟到返回时若用户已经选了别的运行就整条丢弃。
    const batch = batchRef.current.begin("demo");
    fetchNdjson("/api/demo/events").then((es) => {
      if (!batchRef.current.isCurrent(batch, "demo")) return;
      stopStreamRef.current?.();
      setIsLive(false);
      setRunId(null);
      runIdRef.current = null;
      localStorage.removeItem(ACTIVE_RUN_KEY);
      writeRunParam(null);
      setView(initialView());
      const { stop } = replay(es, (e, atMs) => {
        if (!batchRef.current.isCurrent(batch, "demo")) return;
        dispatch(e, atMs);
      }, { mode: "paced", targetMs: 45000 });
      stopStreamRef.current = stop;
    });
  }

  /** 开始一次规划请求：进度从空白开始，服务端推来的步骤和思考片段逐条合进去。 */
  function trackPlanning(steps: PlanStage[]) {
    planProgressRef.current = startProgress(steps);
    setPlanProgress(planProgressRef.current);
    setPlanThinking("");
    return (event: PlannerEvent) => {
      if (!planProgressRef.current) return;
      planProgressRef.current = applyPlannerEvent(planProgressRef.current, event);
      setPlanProgress(planProgressRef.current);
    };
  }

  /** 规划结束：收起进度框，思考内容留着供回看。 */
  function finishPlanning() {
    setPlanThinking(thinkingText(planProgressRef.current));
    planProgressRef.current = null;
    setPlanProgress(null);
  }

  /** simple 的第一步：跑一次开跑前的轻量规划（目标 / 硬要求 + 初步路线）。
   *
   *  模型判"不需要确认"时立刻接着开跑——简单问题不为确认多花一次往返；判"需要"才停下来
   *  让用户看清目标与路线。是否要确认由模型判、服务端只透传，前端不再二次裁决。
   */
  async function handleSimpleSubmit(q: string) {
    if (briefing || submitting) return;
    setBriefing(true);
    setBriefError(null);
    setBriefFailTrace(null);
    queryRef.current = q;
    sourceRef.current = sourceSelection;
    try {
      const prepared = await postJSON<{skill_selection:string}>("/api/skills/prepare", {selected:selectedSkills});
      sourceRef.current = {...sourceRef.current, skill_selection:prepared.skill_selection};
      const res = await postStream<BriefResponse>("/api/brief", {
        query: q,
        ...sourceRef.current,
        trace_full_llm_io: traceFull,
      }, trackPlanning(["gaps", "plan"]));
      if (res.kind === "clarify") {
        // 有缺口时服务端**没有**生成计划：先问清楚，再生成。
        setGaps(res.gaps);
        return;
      }
      await acceptPlan(res.brief);
    } catch (e) {
      setBriefError(e instanceof Error ? e.message : String(e));
      if (e instanceof ApiError && e.trace) setBriefFailTrace(e.trace);
    } finally {
      finishPlanning();
      setBriefing(false);
    }
  }

  /** 拿到计划后的统一出口：不需要确认就直接开跑，需要就停在卡片上。 */
  async function acceptPlan(next: ResearchBrief) {
    if (!next.needs_confirmation) {
      await startSimpleResearch(next);
      return;
    }
    setBrief(next);
  }

  /** 带着用户对缺口问题的回答生成计划；answers 为空表示"全部跳过"。 */
  async function handleGapSubmit(answers: GapAnswer[]) {
    if (submitting || gaps === null) return;
    setSubmitting(true);
    setBriefError(null);
    setBriefFailTrace(null);
    try {
      const res = await postStream<BriefResponse>("/api/brief/plan", {
        query: queryRef.current,
        gaps,
        ...sourceRef.current,
        answers,
        trace_full_llm_io: traceFull,
      }, trackPlanning(["plan"]));
      if (res.kind !== "plan") {
        // 这一段的契约就是"给答案 → 拿计划"；还在追问说明服务端出了问题，别静默吞掉。
        throw new Error("生成计划失败：服务端仍在追问缺口");
      }
      setGaps(null);
      await acceptPlan(res.brief);
    } catch (e) {
      setBriefError(e instanceof Error ? e.message : String(e));
      if (e instanceof ApiError && e.trace) setBriefFailTrace(e.trace);
    } finally {
      finishPlanning();
      setSubmitting(false);
    }
  }

  function handleDiscardGaps() {
    setPlanThinking("");
    setGaps(null);
    setBriefError(null);
    setBriefFailTrace(null);
    setStartError(null);
  }

  async function startSimpleResearch(confirmed: ResearchBrief) {
    setSubmitting(true);
    setStartError(null);
    try {
      // 执行契约不吃 trace；后端也会 strip，这里前端先剥掉避免多余载荷。
      const { trace: _trace, ...briefBody } = confirmed;
      const { run_id } = await postJSON<{ run_id: string }>("/api/research", {
        query: queryRef.current,
        mode: "simple",
        ...sourceRef.current,
        brief: briefBody,
        trace_full_llm_io: traceFull,
      });
      connectToRun(run_id);
    } catch (e) {
      setStartError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleBriefRevise(feedback: string) {
    setBriefHint(null);
    setSubmitting(true);   // 修订飞行中也要挡住"开始研究"/"取消"，跟 handleStart 共用同一个忙态
    try {
      const res = await postStream<BriefResponse>("/api/brief/revise", {
        brief,
        feedback,
        ...sourceRef.current,
        trace_full_llm_io: traceFull,
      }, trackPlanning(["plan"]));
      if (res.kind !== "plan") {
        throw new Error("修订失败：服务端没有返回计划");
      }
      const next = res.brief;
      // 比较时忽略 trace（每次修订 trace.plan_id 都变）。
      const strip = ({ trace: _t, ...rest }: ResearchBrief) => rest;
      if (JSON.stringify(strip(next)) === JSON.stringify(strip(brief!))) {
        setBriefHint("路线未变化，试试换一种说法");
        setBrief(next);   // 仍更新 trace，方便调试看本次修订调用
        return;
      }
      setBrief(next);
    } finally {
      finishPlanning();
      setSubmitting(false);
    }
  }

  function handleDiscardBrief() {
    setPlanThinking("");
    setBrief(null);
    setBriefHint(null);
    setBriefError(null);
    setBriefFailTrace(null);
    setStartError(null);
  }

  /** 输入框提交：进入自主研究的澄清与路线确认。 */
  function handleSubmit(q: string) {
    void handleSimpleSubmit(q);
  }

  async function handleCancelRun() {
    // 两步确认在 RunStatus 的停止按钮上完成，这里不再弹原生 confirm
    if (!runId) return;
    // 立刻进入"取消中"：后台同步请求还没退出，名额也还占着，重复点击没有意义。
    setCancelling(true);
    try {
      await postJSON(`/api/research/${runId}/cancel`, {});
    } catch (e) {
      // 协作式取消：请求本身几乎不会失败（服务端对已结束/不存在的 run 都返回状态而非报错），
      // 真出错也无需打断用户——cancelled/done banner 最终以 SSE 事件为准。
      console.error("cancel 请求失败", e);
      setCancelling(false);
    }
  }

  // runId 只在 connectToRun 里置位，从不自动清空——run 结束后必须有个显式动作回到
  // 空白输入态，否则整页永远卡在这次的 ProcessPane/banner，演示按钮也永久禁用
  // （这条路径正是 Task 14 冒烟脚本"再跑一次"要求踩到的，不是可以往后拖的美化项）。
  function startNewResearch() {
    // 先作废批次：旧流已排队的回调不能再往新页面写东西。
    batchRef.current.begin(null);
    setCancelling(false);
    stopStreamRef.current?.();
    stopStreamRef.current = null;
    setRunId(null);
    setPlanThinking("");
    setIsLive(false);
    const blank = initialView();
    setView(blank);
    setNowMs(0);
    // 清掉"当前运行"的两个入口：新研究不该带着上一条 run 的刷新目标。
    localStorage.removeItem(ACTIVE_RUN_KEY);
    writeRunParam(null);
    setGaps(null);
    setBrief(null);
    setBriefHint(null);
    setBriefError(null);
    setBriefFailTrace(null);
    setStartError(null);
    setStreamDropped(false);
  }

  /** 设置页里的“演示研究”：回到研究页，播放内置示例。 */
  function playDemo() {
    startNewResearch();
    setPage("research");
    runDemo();
  }

  function updatePrefs(next: LocalPrefs) {
    setPrefs(next);
    localStorage.setItem("dra_show_records", String(next.showRecords));
    localStorage.setItem("dra_show_replay", String(next.showReplay));
    localStorage.setItem("dra_trace_full", String(next.traceFull));
  }

  function openNewResearch() {
    setPage("research");
    setHistoryOpen(false);
    startNewResearch();
  }

  function openLiveRun(id: string) {
    setPage("research");
    setHistoryOpen(false);
    connectToRun(id);
  }

  function openHistoryRun(id: string, replayMode: "instant" | "paced") {
    setPage("research");
    setHistoryOpen(false);
    void viewHistoricalRun(id, replayMode);
  }

  // hasContent = 有 run/demo/回放的内容可展示（runId 存在，或 view 已开始——demo 不置 runId）。
  // 无内容 = 空闲态，显示新研究输入或开跑前的确认卡；有内容 = 研究页。
  const hasContent = runId !== null || view.startedAtMs !== undefined;
  const busy = briefing || submitting;
  const canSubmit = sourceSelection.source_mode === "web" || sourceSelection.document_ids.length > 0;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark"><Icon name="search" size={16} strokeWidth={2.2} /></span>
          <span className="brand-text">研究工作台</span>
        </div>
        <button className="btn sidebar-new" onClick={openNewResearch}>
          <Icon name="plus" size={16} /><span>新研究</span>
        </button>
        <button className="icon-btn history-toggle" aria-label="最近的研究" aria-expanded={historyOpen}
          onClick={() => setHistoryOpen((open) => !open)}>
          <Icon name="history" size={18} />
        </button>
        <div className={`sidebar-history${historyOpen ? " is-open" : ""}`}>
          <Sidebar
            refreshSignal={runsVersion} currentRunId={page === "research" ? runId : null}
            onSelectLive={openLiveRun} onSelectReplay={openHistoryRun} showReplay={prefs.showReplay}
          />
        </div>
        <button className={`sidebar-settings${page === "settings" ? " is-active" : ""}`} aria-label="设置"
          aria-current={page === "settings" ? "page" : undefined} onClick={() => setPage(page === "settings" ? "research" : "settings")}>
          <Icon name="sliders" size={16} /><span>设置</span>
        </button>
      </aside>
      <main className="workbench">
        {streamDropped && <div className="conn-warn"><Icon name="warning" size={15} />连接中断，浏览器正在自动重连…</div>}
        {page === "settings" ? (
          <SettingsPage tab={settingsTab} onTab={setSettingsTab} prefs={prefs} onPrefs={updatePrefs} onDemo={playDemo} />
        ) : !hasContent ? (
          <div className="stage-main">
            {brief !== null || gaps !== null ? (
              <div className="prerun">
                {queryRef.current && <div className="user-bubble">{queryRef.current}</div>}
                {!planProgress && <ThinkingReview text={planThinking} />}
                {brief !== null ? (
                  <BriefCard
                    brief={brief} busy={submitting} hint={briefHint}
                    error={briefError ?? startError} mode={mode}
                    onStart={() => { void startSimpleResearch(brief); }}
                    onRevise={handleBriefRevise} onCancel={handleDiscardBrief}
                  />
                ) : (
                  <GapCard
                    gaps={gaps ?? []} busy={submitting}
                    error={briefError ?? startError}
                    onSubmit={handleGapSubmit} onCancel={handleDiscardGaps}
                  />
                )}
                {planProgress && <PlanningProgress progress={planProgress} />}
              </div>
            ) : (
              <NewResearch
                initialQuery={queryRef.current} onDraftChange={q=>{queryRef.current=q}}
                busy={busy}
                error={briefError ?? startError}
                mode={mode} planTrace={briefFailTrace}
                progress={planProgress ? <PlanningProgress progress={planProgress} /> : undefined}
                onSubmit={handleSubmit}
                controls={<>
                  <SourcePicker value={sourceSelection} onChange={setSourceSelection} disabled={busy}/>
                  <LibraryButton value={sourceSelection} onChange={setSourceSelection} disabled={busy}/>
                  <SkillPicker selected={selectedSkills} onChange={setSelectedSkills} disabled={busy}/>
                </>}
                canSubmit={canSubmit}
                blockedHint="当前来源只查我的资料，但还没有选资料。请在「我的资料」里勾选，或把来源改成网页。"
              />
            )}
          </div>
        ) : (
          <div className="stage-main">
            <ConversationWorkspace key={runId ?? "current"} view={view} runId={runId} mode={mode}
              showRecords={prefs.showRecords}
              status={<RunStatus key={runId ?? "none"} view={view} nowMs={nowMs}
                onCancel={handleCancelRun} canCancel={running && runId !== null && isLive} cancelling={cancelling} />}
              canSend={isLive && !streamDropped && !cancelling && view.endedAtMs === undefined && view.inputState === 'open'}
              onSend={sendResearchMessage} onFollowupStarted={id=>{setRunsVersion(v=>v+1);connectToRun(id)}}
              onOpenRun={(id,status)=>{if(status==='running')connectToRun(id);else void viewHistoricalRun(id,'instant')}}/>
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
