import { useEffect, useState, type ReactNode } from "react";
import { getJSON } from "../api";
import { executionNowView, executionStatusView, minutesText, networkModeText, type ExecutionProbe, type ExecutionStatus, type Tone } from "../capabilities";
import { Icon, type IconName } from "./Icon";
import { ModelField } from "./ModelField";
import { Select } from "./Select";
import type { ConfigPayload, RuntimeSettings } from "./SettingsPage";
import { SettingRow, Switch } from "./ui";

interface SkillItem { name: string; title?: string; enabled: boolean; error?: string }
export interface SkillsSummary { names: string[] | null; error?: string }
export type { ExecutionProbe };

const message = (e: unknown) => e instanceof Error ? e.message : String(e);

/** 读取“新研究将使用”的执行状态；保存后（revision 变化）和手动刷新时重新检查。 */
function useExecutionStatus(revision: number) {
  const [probe, setProbe] = useState<ExecutionProbe>({ status: null, loading: true });
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    setProbe(p => ({ ...p, loading: true, error: undefined }));
    getJSON<ExecutionStatus>("/api/execution/status")
      .then(status => { if (alive) setProbe({ status, loading: false, checkedAt: Date.now() }); })
      .catch(e => { if (alive) setProbe({ status: null, loading: false, error: message(e), checkedAt: Date.now() }); });
    return () => { alive = false; };
  }, [revision, tick]);
  return { probe, refresh: () => setTick(t => t + 1) };
}

function useEnabledSkills(): SkillsSummary {
  const [summary, setSummary] = useState<SkillsSummary>({ names: null });
  useEffect(() => {
    let alive = true;
    getJSON<{ skills: SkillItem[] }>("/api/skills")
      .then(r => { if (alive) setSummary({ names: r.skills.filter(s => s.enabled && !s.error).map(s => s.title || s.name) }); })
      .catch(e => { if (alive) setSummary({ names: null, error: message(e) }); });
    return () => { alive = false; };
  }, []);
  return summary;
}

function Lamp({ tone }: { tone: Tone }) {
  return <span className={`cap-lamp is-${tone}`} aria-hidden="true" />;
}

interface Tile { id: string; icon: IconName; label: string; value: string; detail: string; tone: Tone }

/** 顶部总览：按**已保存**的设置说明新研究会用到什么；未保存的草稿不算数。 */
export function capabilityTiles(saved: ConfigPayload, execution: ExecutionProbe, skills: SkillsSummary): Tile[] {
  const vision = saved.vision;
  // 权限按已保存设置；旧服务没有这项摘要时，才用状态接口读到的最后已知值。
  const execEnabled = saved.execution_effective?.enabled ?? execution.status?.enabled;
  const exec = executionNowView(execEnabled, execution);
  const judgeOn = !!saved.evidence_judge?.enabled;
  const keyMissing = judgeOn && saved.evidence_judge_status?.key_configured === false;
  const retries = saved.retry_policy?.research.max_retries;
  return [
    { id: "cap-vision", icon: "image", label: "看图",
      value: vision?.enabled ?? true ? "开启" : "关闭",
      detail: vision?.enabled ?? true ? (vision?.mode === "force_auxiliary" ? "始终用备用模型" : "自动选择看图方式") : "不查看图片",
      tone: vision?.enabled ?? true ? "ok" : "off" },
    { id: "cap-execution", icon: "terminal", label: "运行代码",
      value: execEnabled == null ? "未知" : execEnabled ? "开启" : "关闭",
      detail: exec.short, tone: exec.tone },
    { id: "cap-browser", icon: "globe", label: "浏览器",
      value: saved.browser?.enabled ? "开启" : "关闭", detail: "动态网页与报告预览，按需启动",
      tone: saved.browser?.enabled ? "ok" : "off" },
    { id: "cap-judge", icon: "verify", label: "引文核验",
      value: judgeOn ? "开启" : "关闭", detail: keyMissing ? "密钥未配置" : judgeOn ? "保存证据时核验" : "不额外核验",
      tone: keyMissing ? "warn" : judgeOn ? "ok" : "off" },
    { id: "cap-skills", icon: "sparkles", label: "技能",
      value: skills.names == null ? (skills.error ? "读取失败" : "读取中") : skills.names.length ? `${skills.names.length} 个已启用` : "未启用",
      detail: skills.names?.length ? "按问题自动选用" : skills.error ? "请稍后刷新" : "不提供技能",
      tone: skills.error ? "bad" : skills.names == null ? "busy" : skills.names.length ? "ok" : "off" },
    { id: "cap-compaction", icon: "layers", label: "长对话整理",
      value: saved.compaction_enabled ? "开启" : "关闭",
      detail: saved.compaction_enabled ? "接近上限时压缩旧过程" : "本机不拦截长对话",
      tone: saved.compaction_enabled ? "ok" : "off" },
    { id: "cap-time", icon: "timer", label: "研究时限",
      value: minutesText(saved.wall_timeout_s ?? null),
      detail: retries != null ? `请求失败最多重试 ${retries} 次` : "含收尾时间",
      tone: "ok" },
  ];
}

export function CapabilityOverview({ saved, execution, skills, dirty }: {
  saved: ConfigPayload; execution: ExecutionProbe; skills: SkillsSummary; dirty: boolean;
}) {
  const jump = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  return <section className="cap-overview" aria-label="新研究将使用的能力">
    <div className="cap-overview-head">
      <strong>新研究将使用</strong>
      <small className={dirty ? "is-warn" : undefined}>{dirty ? "有未保存的修改，保存后这里才会变化" : "按已保存的设置显示"}</small>
    </div>
    <div className="cap-tiles">
      {capabilityTiles(saved, execution, skills).map(t => <button type="button" key={t.id} className="cap-tile" onClick={() => jump(t.id)}>
        <span className="cap-tile-label"><Icon name={t.icon} size={14} />{t.label}</span>
        <span className="cap-tile-value"><Lamp tone={t.tone} />{t.value}</span>
        <small>{t.detail}</small>
      </button>)}
    </div>
  </section>;
}

function CapCard({ id, icon, title, summary, control, children }: {
  id: string; icon: IconName; title: string; summary: ReactNode; control?: ReactNode; children?: ReactNode;
}) {
  return <section className="card cap-card" id={id} aria-labelledby={`${id}-title`}>
    <div className="cap-card-head">
      <span className="cap-icon"><Icon name={icon} size={18} /></span>
      <div className="cap-card-title"><strong id={`${id}-title`}>{title}</strong><small>{summary}</small></div>
      {control}
    </div>
    {children && <div className="cap-card-body">{children}</div>}
  </section>;
}

/** Bash/Docker 的实际状态面板。权限按已保存设置；草稿与之不同时提示“保存后才生效”；复查中或过期的检查结果标为“上次检查”。 */
export function ExecutionStatusPanel({ probe, savedEnabled, draftEnabled, onRefresh }: {
  probe: ExecutionProbe; savedEnabled: boolean | null; draftEnabled: boolean | null; onRefresh?: () => void;
}) {
  const { status } = probe;
  const view = executionNowView(savedEnabled, probe);
  const previous = status && savedEnabled !== false && (probe.loading || view.stale) ? executionStatusView(status) : null;
  const pending = draftEnabled != null && savedEnabled != null && draftEnabled !== savedEnabled;
  const checkedAt = probe.checkedAt ? new Date(probe.checkedAt).toLocaleTimeString() : null;
  return <div className={`cap-status is-${view.tone}`}>
    <div className="cap-status-head">
      <Lamp tone={view.tone} />
      <span className="cap-status-title" role="status">新研究的实际状态：<strong>{view.title}</strong></span>
      {onRefresh && <button type="button" className="btn btn-sm btn-ghost" disabled={probe.loading} onClick={onRefresh}>
        <Icon name={probe.loading ? "loading" : "refresh"} className={probe.loading ? "icon-spin" : undefined} size={13} />{probe.loading ? "检查中" : "重新检查"}
      </button>}
    </div>
    {view.detail && <p>{view.detail}</p>}
    {previous && <p className="field-hint">上次检查{checkedAt ? `（${checkedAt}）` : ""}：{previous.title}。仅供参考，不代表现在的状态。</p>}
    {savedEnabled !== false && status?.enabled && <p>{networkModeText(status.network_mode)}{status.network_mode === "bridge" ? "（只用本地资料的研究始终不联网）" : ""}</p>}
    {pending && <p className="cap-pending"><Icon name="info" size={13} />还没保存：保存后新研究才会{draftEnabled ? "开启" : "关闭"}运行代码{draftEnabled ? "，到时会重新检查 Docker" : ""}。现在开始的研究仍按「{savedEnabled ? "开启" : "关闭"}」处理。</p>}
    {status && <details className="disclosure disclosure-inline cap-env">
      <summary>环境详情{previous ? "（上次检查）" : ""}</summary>
      <dl className="cap-env-list">
        <div><dt>开关来源</dt><dd>{status.source === "settings" ? "在此页面设置" : "跟随本机默认"}{status.default_enabled != null ? `（本机默认：${status.default_enabled ? "开启" : "关闭"}）` : ""}</dd></div>
        <div><dt>网络模式</dt><dd className="mono">{status.network_mode}</dd></div>
        <div><dt>执行镜像</dt><dd className="mono">{status.image ?? "—"}</dd></div>
        <div><dt>镜像 ID</dt><dd className="mono">{status.image_id ?? "—"}</dd></div>
        <div><dt>状态代码</dt><dd className="mono">{status.available ? "available" : status.reason ?? "—"}</dd></div>
        {checkedAt && <div><dt>检查时间</dt><dd>{checkedAt}</dd></div>}
      </dl>
    </details>}
  </div>;
}

export function CapabilitySettings({ s, onManageSkills }: { s: RuntimeSettings; onManageSkills: () => void }) {
  const config = s.config!, saved = s.saved ?? config;
  const setConfig = (next: ConfigPayload) => s.setConfig(next);
  const { probe, refresh } = useExecutionStatus(s.revision);
  const skills = useEnabledSkills();
  const providers = s.providers;

  const vision = { enabled: true, mode: "auto" as const, provider: "", model: "gemini-2.5-flash", ...config.vision };
  const setVision = (patch: Partial<typeof vision>) => setConfig({ ...config, vision: { ...vision, ...patch } });
  const executionSupported = "execution_enabled" in config;
  // 已保存的权限；旧服务没有执行开关字段时，用状态接口读到的最后已知值，不拿“关闭”填未知。
  const savedExecution = saved.execution_effective?.enabled ?? probe.status?.enabled ?? null;
  const defaultEnabled = config.execution_effective?.default_enabled ?? probe.status?.default_enabled ?? null;
  const defaultText = defaultEnabled == null ? "" : `（${defaultEnabled ? "开启" : "关闭"}）`;
  const executionDraft = executionSupported ? config.execution_enabled ?? defaultEnabled : savedExecution;
  const windowMode = config.context_window_mode ?? "manual";
  const policy = saved.retry_policy;
  const summaryRetries = config.summary_max_retries ?? 1;

  const numberField = (key: "wall_timeout_s" | "wrapup_reserve_s" | "summary_timeout_s" | "summary_max_retries", label: string, unit: string, fallback: number, min: number, hint?: ReactNode) =>
    <label className="field" key={key}><span className="field-label">{label}（{unit}）</span>
      <input className="input input-num" aria-label={label} type="number" min={min} step={1}
        value={config[key] ?? ""} placeholder={key === "wall_timeout_s" ? "留空表示不限" : String(fallback)}
        onChange={e => setConfig({ ...config, [key]: e.target.value ? Number(e.target.value) : key === "wall_timeout_s" ? null : fallback })} />
      {hint && <small className="field-hint">{hint}</small>}
    </label>;

  return <div className="settings-section">
    <div className="section-head"><div><h2>研究能力</h2>
      <p>决定研究时 Agent 能用哪些额外本领，以及时间、重试和长对话怎么处理。保存后只影响之后开始的研究，进行中的研究保持开始时的设置。</p></div></div>
    {s.notice && <p className="notice" role="status">{s.notice}</p>}
    <CapabilityOverview saved={saved} execution={probe} skills={skills} dirty={s.dirty} />

    <div className="cap-group">
      <h3>能力开关</h3>

      <CapCard id="cap-vision" icon="image" title="看图"
        summary="让 Agent 查看网页图片和 PDF 页面。看图得到的内容会标为“视觉观察”，不会当作原文引用。"
        control={<Switch label="看图" checked={vision.enabled} onChange={enabled => setVision({ enabled })} />}>
        <div className="field-grid field-grid-3">
          <div className="field"><span className="field-label">看图方式</span>
            <Select ariaLabel="看图方式" value={vision.mode} onChange={v => setVision({ mode: v as "auto" | "force_auxiliary" })}
              options={[{ value: "auto", label: "自动选择" }, { value: "force_auxiliary", label: "始终用备用模型" }]} /></div>
          <div className="field"><span className="field-label">备用连接</span>
            <Select ariaLabel="备用识图连接" value={vision.provider}
              options={[{ value: "", label: "未选择" }, ...providers.map(p => ({ value: p.name, label: p.label || p.name, disabled: !p.key_configured, hint: p.key_configured ? "" : "无 Key" }))]}
              onChange={provider => setVision({ provider })} /></div>
          <div className="field"><span className="field-label">备用模型</span>
            <ModelField label="识图模型 ID" value={vision.model} models={providers.find(p => p.name === vision.provider)?.models ?? []} onChange={model => setVision({ model })} /></div>
        </div>
        <p className="field-hint">自动选择：研究模型确认能看图、并且当前连接确认能传图片时，由研究模型直接看；否则交给备用模型。{vision.enabled && !vision.provider
          ? <span className="is-warn-text">没有选备用连接时，只有研究模型和当前连接都确认支持图片才看得了图。</span> : null}</p>
      </CapCard>

      <CapCard id="cap-execution" icon="terminal" title="运行代码（Bash / Python）"
        summary="让主、子 Agent 在同一隔离工作区里下载文件、计算和制作报告。主 Agent 统一提交附件。"
        control={executionDraft == null ? <small className="field-hint">{probe.loading ? "读取中…" : "状态未知"}</small>
          : <Switch label="运行代码" checked={executionDraft} disabled={!executionSupported}
              onChange={v => setConfig({ ...config, execution_enabled: v })} />}>
        <p className="cap-source">
          {!executionSupported ? "当前服务不支持在网页里切换，按本机默认处理。"
            : config.execution_enabled == null ? `跟随本机默认${defaultText}。`
            : <>已在这里设为{config.execution_enabled ? "开启" : "关闭"}。<button type="button" className="link-btn"
                onClick={() => setConfig({ ...config, execution_enabled: null })}>改回跟随本机默认{defaultText}</button></>}
        </p>
        <ExecutionStatusPanel probe={probe} savedEnabled={savedExecution} draftEnabled={executionSupported ? executionDraft : null} onRefresh={refresh} />
      </CapCard>

      <CapCard id="cap-browser" icon="globe" title="浏览器"
        summary="需要时打开动态网页、定位原图，或预览报告并检查图片、布局和交互。连续操作保留页面状态。"
        control={<Switch label="浏览器" checked={!!config.browser?.enabled} onChange={enabled => setConfig({...config, browser: {enabled}})} />}>
        <p className="field-hint">使用独立 Playwright 浏览器，不使用你的登录会话。截图沿用上面的看图设置；主、子 Agent 各自操作页面，共享文件。需要本机安装 Node、Playwright MCP 和 Chromium。</p>
      </CapCard>

      <CapCard id="cap-judge" icon="verify" title="引文核验"
        summary="保存证据时，把结论和引文交给核验模型，判断引文是否撑得起结论。结果显示在证据卡上，也会反馈给 Agent 修正；不会拦下引用，超时显示“未核验”。"
        control={<Switch label="引文核验" checked={!!config.evidence_judge?.enabled} onChange={v => setConfig({ ...config, evidence_judge: { enabled: v } })} />}>
        <p className={`cap-source${config.evidence_judge?.enabled && config.evidence_judge_status?.key_configured === false ? " is-warn-text" : ""}`}>
          {config.evidence_judge_status?.key_configured ? "核验服务的密钥已配置" : "核验服务的密钥未配置，开启后无法核验"} · 核验模型 {config.evidence_judge_status?.model ?? "Jev"}
        </p>
      </CapCard>

      <CapCard id="cap-skills" icon="sparkles" title="技能"
        summary="技能是写好的研究套路。研究开始时，先把已启用技能的目录（名称和用途）提供给 Agent，由它按问题决定加载哪些技能的正文；新建研究时也可以手动指定。"
        control={<button type="button" className="btn btn-sm" onClick={onManageSkills}>管理技能</button>}>
        {skills.error ? <p className="cap-source is-bad-text" role="alert">技能列表读取失败：{skills.error}</p>
          : skills.names == null ? <p className="cap-source">正在读取技能…</p>
          : skills.names.length ? <div className="cap-tags">{skills.names.map(n => <span key={n} className="tag">{n}</span>)}</div>
          : <p className="cap-source">还没有启用的技能。</p>}
        <p className="field-hint">每次研究实际加载了哪些技能，在回答下方的「本轮技能」里查看。</p>
      </CapCard>
    </div>

    <div className="cap-group">
      <h3>研究过程</h3>

      <CapCard id="cap-time" icon="timer" title="时间与重试"
        summary="总时限包含收尾预留。进入收尾后停止新的检索、阅读和整理，用已有材料写回答。">
        <div className="field-grid">
          {numberField("wall_timeout_s", "总时限", "秒", 1200, 1, config.wall_timeout_s ? `约 ${minutesText(Math.round(config.wall_timeout_s / 60) * 60)}` : "不限总时长")}
          {numberField("wrapup_reserve_s", "收尾预留", "秒", 120, 0, "留给写回答的时间，不额外延长总时限")}
        </div>
        {policy && <div className="cap-readonly">
          <strong>研究模型请求失败时自动重试</strong>
          <p>每次请求失败后最多重试 <b>{policy.research.max_retries}</b> 次，也就是最多发出 <b>{policy.research.max_attempts}</b> 次请求。主 Agent、子任务和收尾写回答都按这个处理；当前阶段的剩余时间用完或你叫停研究后，不再发起新的重试（已经发出的请求不一定立刻中断）。</p>
          <small>这是系统固定的策略，这里只做说明。</small>
        </div>}
        <div className="cap-subsection">
          <strong>整理对话时的摘要请求</strong>
          <p className="field-hint">与上面的研究重试分开计算，只在「长对话整理」开启时用到；每次尝试也受剩余研究时间约束，只在超时、连接或服务故障时重试。{!config.compaction_enabled && <span className="is-warn-text">长对话整理目前关闭，这两项暂时不起作用。</span>}</p>
          <div className="field-grid">
            {numberField("summary_timeout_s", "摘要单次时限", "秒", 120, 1)}
            {numberField("summary_max_retries", "摘要重试次数", "次", 1, 0, `最多发出 ${summaryRetries + 1} 次请求`)}
          </div>
        </div>
      </CapCard>

      <CapCard id="cap-compaction" icon="layers" title="长对话自动整理"
        summary="对话太长时，把较早的过程压缩成摘要，保留最近几轮。证据和原始记录不受影响；整理本身会调用研究模型。"
        control={<Switch label="长对话自动整理" checked={!!config.compaction_enabled} onChange={v => setConfig({ ...config, compaction_enabled: v })} />}>
        {config.compaction_enabled
          ? <p className="field-hint">先为本次输出预留空间，再按剩余输入空间的 85% 触发整理，尽量降到 60%。</p>
          : <p className="cap-callout">已关闭：不整理对话，也不会按工作窗口在本机提前拦下过长的对话；对话超出模型容量时，以服务商的返回为准。</p>}
        <div className="cap-subsection">
          <strong>工作窗口</strong>
          <p className="field-hint">阅读单篇长文时按这个大小分段处理{config.compaction_enabled ? "；也用来判断何时整理对话" : "，关闭整理后仍然有效"}。自动模式只采用此连接的接口资料或你手动确认的资料。</p>
          <div className="field-grid">
            <div className="field"><span className="field-label">容量依据</span>
              <Select ariaLabel="容量依据" value={windowMode}
                onChange={v => setConfig({ ...config, context_window_mode: v as "manual" | "auto" })}
                options={[{ value: "auto", label: "使用连接确认的模型容量" }, { value: "manual", label: "手动设置工作窗口" }]} /></div>
            {windowMode === "manual" && <label className="field"><span className="field-label">工作窗口（token）</span>
              <input className="input input-num" type="number" min={1} step={1} value={config.context_window_tokens ?? ""}
                placeholder={config.compaction_enabled ? "例如 64000" : "留空则按连接确认的容量"} aria-label="工作窗口"
                onChange={e => setConfig({ ...config, context_window_tokens: e.target.value ? Number(e.target.value) : null })} /></label>}
          </div>
        </div>
      </CapCard>

      <CapCard id="cap-output" icon="page" title="各阶段输出额度"
        summary="每次请求允许模型写多长。摘要、收尾留空则沿用研究额度；额度过大导致没有输入空间时，需要手动调小。">
        <div className="field-grid field-grid-3">
          {([["max_output_tokens", "研究输出额度"], ["summary_max_output_tokens", "摘要输出额度"], ["wrapup_max_output_tokens", "收尾输出额度"]] as const).map(([key, label]) =>
            <label className="field" key={key}><span className="field-label">{label}（token）</span>
              <input className="input" aria-label={label} type="number" min={512} step={1}
                value={config[key] ?? ""} placeholder={key === "max_output_tokens" ? "32768" : "沿用研究额度"}
                onChange={e => setConfig({ ...config, [key]: e.target.value ? Number(e.target.value) : key === "max_output_tokens" ? 32768 : null })} /></label>)}
        </div>
      </CapCard>

      <section className="card cap-card" id="cap-limits">
        <SettingRow title="限制调用次数" description="开启后，主 Agent 和子任务的模型、工具调用次数都有上限，并留出收尾的次数。不论开关，研究总时限都有效。">
          <Switch label="限制调用次数" checked={!!config.call_limits_enabled} onChange={v => setConfig({ ...config, call_limits_enabled: v })} />
        </SettingRow>
      </section>
    </div>
    {s.error && <p className="field-hint is-bad" role="alert">{s.error}</p>}
  </div>;
}
