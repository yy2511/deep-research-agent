import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { executionNowView, executionStatusView, type ExecutionStatus } from "./capabilities";
import { CapabilitySettings, ExecutionStatusPanel, capabilityTiles, type ExecutionProbe } from "./components/CapabilitySettings";
import { RuntimeTab, settingsUpdate, type ConfigPayload, type RuntimeSettings } from "./components/SettingsPage";

const status = (patch: Partial<ExecutionStatus> = {}): ExecutionStatus => ({
  enabled: true, available: true, image_id: "sha256:abc123", image: "dra-sandbox:local", network_mode: "none",
  source: "environment", default_enabled: true, ...patch,
});
const probe = (s: ExecutionStatus | null, extra: Partial<ExecutionProbe> = {}): ExecutionProbe => ({ status: s, loading: false, ...extra });

const config: ConfigPayload = {
  schema_version: 4, call_limits_enabled: false,
  planner: { provider: "codex521", model: "gpt-6-sol", effort: null },
  subagent: { provider: "codex521", model: "gpt-6-sol", reasoning: true, effort: null },
  vision: { enabled: true, mode: "auto", provider: "gemini", model: "gemini-2.5-flash" },
  evidence_judge: { enabled: false }, evidence_judge_status: { key_configured: true, model: "jev", concurrency: 1, batch_timeout_s: 1 },
  compaction_enabled: false, context_window_mode: "manual", context_window_tokens: 128000,
  wall_timeout_s: 1200, wrapup_reserve_s: 120, summary_timeout_s: 120, summary_max_retries: 1,
  execution_enabled: null,
  execution_effective: { enabled: true, network_mode: "none", source: "environment", default_enabled: true },
  // 故意用 7：页面必须显示服务端给的数，而不是写死 10。
  retry_policy: { research: { max_retries: 7, max_attempts: 8 }, summary: { max_retries: 1, max_attempts: 2, timeout_s: 120 } },
};

function fakeSettings(draft: ConfigPayload, saved: ConfigPayload = draft): RuntimeSettings {
  const noop = () => {};
  return { config: draft, saved, setConfig: noop, providers: [], setProviders: noop, saving: false, error: null, setError: noop,
    notice: "", setNotice: noop, dirty: JSON.stringify(draft) !== JSON.stringify(saved), load: noop, save: async () => {}, revision: 0 } as unknown as RuntimeSettings;
}

describe("Bash/Docker 状态文案", () => {
  it.each([
    [status({ enabled: false, available: false, reason: "disabled" }), "已关闭"],
    [status(), "已开启，可以使用"],
    [status({ available: false, reason: "docker_unavailable", image_id: undefined }), "连不上 Docker"],
    [status({ available: false, reason: "image_missing" }), "还没有执行环境的镜像"],
    [status({ available: false, reason: "image_incompatible" }), "镜像版本不匹配"],
    [status({ available: false, reason: "cleanup_pending" }), "还没清理完"],
    [status({ available: false, reason: "something_new" }), "something_new"],
  ])("%#", (s, text) => {
    const view = executionStatusView(s);
    expect(`${view.title}${view.detail}`).toContain(text);
  });
  it("接口失败显示检查失败，不当成关闭", () => {
    expect(executionStatusView(null, { error: "502 Bad Gateway" })).toMatchObject({ tone: "bad", title: "检查失败", detail: "502 Bad Gateway" });
  });
  it("开关草稿与已生效状态不同时，明确说保存后才生效", () => {
    const html = renderToStaticMarkup(<ExecutionStatusPanel probe={probe(status())} savedEnabled draftEnabled={false} />);
    expect(html).toContain("还没保存");
    expect(html).toContain("现在开始的研究仍按「开启」处理");
    expect(renderToStaticMarkup(<ExecutionStatusPanel probe={probe(status())} savedEnabled draftEnabled />)).not.toContain("还没保存");
  });
  it("镜像 ID、状态代码只放在折叠的环境详情里", () => {
    const html = renderToStaticMarkup(<ExecutionStatusPanel probe={probe(status({ network_mode: "bridge" }))} savedEnabled draftEnabled />);
    const details = html.slice(html.indexOf("<details"));
    expect(html.indexOf("sha256:abc123")).toBeGreaterThan(html.indexOf("<details"));
    expect(details).toContain("dra-sandbox:local");
    expect(details).not.toContain(" open");
    expect(html).toContain("可以联网");
  });
});

describe("研究能力页", () => {
  it("浏览器扩展草稿显示安装授权说明，总览仍按已保存模式", () => {
    const saved: ConfigPayload = {...config, browser: {enabled: true, mode: "isolated"}};
    const draft: ConfigPayload = {...saved, browser: {...saved.browser!, mode: "extension", extension_browser: "msedge", profile_dir_name: "Profile 2"}};
    const html = renderToStaticMarkup(<CapabilitySettings s={fakeSettings(draft, saved)} onManageSkills={() => {}} />);
    expect(html).toContain('aria-label="浏览器连接方式"');
    expect(html).toContain("Playwright 官方扩展");
    expect(html).toContain("由你选择允许访问的标签页");
    expect(html).toContain("Microsoft Edge");
    expect(html).toContain('value="Profile 2"');
    expect(html).toContain("选择此模式不代表已经连接成功");
    expect(capabilityTiles(saved, probe(status()), {names: []}).find(t => t.id === "cap-browser")?.detail).toContain("独立后台");
    expect(capabilityTiles(draft, probe(status()), {names: []}).find(t => t.id === "cap-browser")?.detail).toContain("复用登录状态");
  });
  it("总览按已保存设置显示，并取服务端重试次数", () => {
    const tiles = capabilityTiles(config, probe(status()), { names: ["方案对比", "论文评审"] });
    const byId = Object.fromEntries(tiles.map(t => [t.id, t]));
    expect(byId["cap-execution"]).toMatchObject({ value: "开启", detail: "Docker 可用", tone: "ok" });
    expect(byId["cap-skills"].value).toBe("2 个已启用");
    expect(byId["cap-compaction"].detail).toBe("本机不拦截长对话");
    expect(byId["cap-time"].detail).toContain("最多重试 7 次");
    const failed = capabilityTiles(config, probe(null, { error: "boom" }), { names: null, error: "x" });
    expect(failed.find(t => t.id === "cap-execution")!.tone).toBe("bad");
    expect(failed.find(t => t.id === "cap-skills")!.value).toBe("读取失败");
  });
  it("压缩关闭时说明本机容量拦截也关闭，工作窗口仍用于单篇长文；不写 1M", () => {
    const html = renderToStaticMarkup(<CapabilitySettings s={fakeSettings(config)} onManageSkills={() => {}} />);
    expect(html).toContain("也不会按工作窗口在本机提前拦下过长的对话");
    expect(html).toContain("阅读单篇长文时按这个大小分段处理");
    expect(html).toContain("关闭整理后仍然有效");
    expect(html).toContain('value="128000"');
    expect(html).not.toMatch(/1M|1,000,000|100 万/);
  });
  it("研究重试是服务端只读策略，摘要重试单独可调", () => {
    const html = renderToStaticMarkup(<CapabilitySettings s={fakeSettings(config)} onManageSkills={() => {}} />);
    expect(html).toContain("最多重试 <b>7</b> 次");
    expect(html).toContain("最多发出 <b>8</b> 次请求");
    expect(html).toContain("当前阶段的剩余时间用完或你叫停研究后，不再发起新的重试");
    expect(html).not.toContain("立刻停止");
    expect(html).not.toContain("<b>10</b>");
    expect(html).toContain('aria-label="摘要重试次数"');
    expect(html).not.toContain('aria-label="研究重试次数"');
  });
  it("总时限是开关：默认关闭时不显示时限输入，开启后显示总时限和交稿时间", () => {
    const off = { ...config, wall_timeout_s: null };
    const offHtml = renderToStaticMarkup(<CapabilitySettings s={fakeSettings(off)} onManageSkills={() => {}} />);
    expect(offHtml).toMatch(/aria-checked="false" aria-label="限制研究总时长"/);
    expect(offHtml).not.toContain('aria-label="总时限"');
    expect(offHtml).not.toContain('aria-label="交稿时间"');
    expect(offHtml).not.toContain("收尾预留");
    expect(capabilityTiles(off, probe(status()), { names: [] }).find(t => t.id === "cap-time")?.value).toBe("不限");
    const onHtml = renderToStaticMarkup(<CapabilitySettings s={fakeSettings({ ...config, wall_timeout_s: 900 })} onManageSkills={() => {}} />);
    expect(onHtml).toMatch(/aria-checked="true" aria-label="限制研究总时长"/);
    expect(onHtml).toMatch(/aria-label="总时限"[^>]*value="900"/);
    expect(onHtml).toContain('aria-label="交稿时间"');
    expect(onHtml).toContain("总时限里留给交稿的秒数，不延长总时限");
  });
  it("执行开关跟随本机默认时如实说明；草稿未保存时总览仍显示已保存状态", () => {
    const html = renderToStaticMarkup(<CapabilitySettings s={fakeSettings(config)} onManageSkills={() => {}} />);
    expect(html).toContain("跟随本机默认（开启）");
    expect(html).toContain('aria-label="运行代码"');
    const draft = { ...config, execution_enabled: false };
    const dirty = renderToStaticMarkup(<CapabilitySettings s={fakeSettings(draft, config)} onManageSkills={() => {}} />);
    expect(dirty).toContain("已在这里设为关闭");
    expect(dirty).toContain("改回跟随本机默认");
    expect(dirty).toContain("有未保存的修改，保存后这里才会变化");
  });
  it("技能卡说明先给目录、由 Agent 决定加载正文；自动看图保留连接条件", () => {
    const html = renderToStaticMarkup(<CapabilitySettings s={fakeSettings({ ...config, vision: { ...config.vision!, provider: "" } })} onManageSkills={() => {}} />);
    expect(html).toContain("先把已启用技能的目录");
    expect(html).toContain("由它按问题决定加载哪些技能的正文");
    expect(html).toContain("当前连接确认能传图片");
    expect(html).toContain("研究模型和当前连接都确认支持图片");
  });
});

describe("执行状态：权限按已保存设置，旧检查结果不冒充现在", () => {
  const savedOff = { ...config, execution_enabled: false,
    execution_effective: { enabled: false, network_mode: "none", source: "settings" as const, default_enabled: true } };
  const savedOn = { ...config, execution_enabled: true,
    execution_effective: { enabled: true, network_mode: "none", source: "settings" as const, default_enabled: false } };
  const tile = (saved: ConfigPayload, p: ExecutionProbe) => capabilityTiles(saved, p, { names: [] }).find(t => t.id === "cap-execution")!;

  it("已保存关闭 + 旧检查仍是开启：总览和面板都按关闭", () => {
    const old = probe(status({ enabled: true, available: true }));
    expect(tile(savedOff, old)).toMatchObject({ value: "关闭", detail: "不提供 Bash", tone: "off" });
    const html = renderToStaticMarkup(<ExecutionStatusPanel probe={old} savedEnabled={false} draftEnabled={false} />);
    expect(html).toContain("<strong>已关闭</strong>");
    expect(html).not.toContain("可以使用");
    expect(html).not.toContain("还没保存");
  });
  it("已保存关闭、复查中：仍是关闭，不显示检查中", () => {
    expect(tile(savedOff, probe(status(), { loading: true }))).toMatchObject({ value: "关闭", tone: "off" });
  });
  it("已保存开启 + 复查中：显示检查中，旧结果只标为上次检查", () => {
    const rechecking = probe(status({ enabled: false, available: false, reason: "disabled" }), { loading: true, checkedAt: 1 });
    expect(tile(savedOn, rechecking)).toMatchObject({ value: "开启", detail: "检查中…", tone: "busy" });
    const html = renderToStaticMarkup(<ExecutionStatusPanel probe={rechecking} savedEnabled draftEnabled />);
    expect(html).toContain("已开启，正在检查执行环境…");
    expect(html).toContain("上次检查");
    expect(html).toContain("不代表现在的状态");
    expect(html).toContain("环境详情（上次检查）");
    // 开启 + 可用的旧结果在复查中也不能显示成“可用”
    const wasOk = probe(status(), { loading: true });
    expect(tile(savedOn, wasOk).detail).toBe("检查中…");
    expect(renderToStaticMarkup(<ExecutionStatusPanel probe={wasOk} savedEnabled draftEnabled />)).not.toContain("<strong>已开启，可以使用</strong>");
  });
  it("已保存开启 + 不在复查但检查结果是关闭：标为过期，不当作关闭", () => {
    const stale = probe(status({ enabled: false, available: false, reason: "disabled" }));
    expect(tile(savedOn, stale)).toMatchObject({ value: "开启", detail: "等待重新检查", tone: "busy" });
    expect(executionNowView(true, stale)).toMatchObject({ title: "检查结果已过期", stale: true });
    expect(renderToStaticMarkup(<ExecutionStatusPanel probe={stale} savedEnabled draftEnabled />)).not.toContain("<strong>已关闭</strong>");
  });
  it("检查结果与已保存一致时才采纳 Docker 可用性", () => {
    expect(tile(savedOn, probe(status({ available: false, reason: "image_missing" })))).toMatchObject({ detail: "暂时用不了", tone: "warn" });
    expect(tile(savedOn, probe(status()))).toMatchObject({ detail: "Docker 可用", tone: "ok" });
  });
  it("旧服务没有执行开关字段：用状态接口读到的已知值，不提示待保存，不拿关闭填未知", () => {
    const legacy = { ...config } as ConfigPayload;
    delete legacy.execution_enabled; delete legacy.execution_effective;
    // 旧状态接口没有 default_enabled，环境实际是开启。
    const legacyStatus = status({ default_enabled: undefined, source: undefined });
    expect(tile(legacy, probe(legacyStatus))).toMatchObject({ value: "开启", tone: "ok" });
    const panel = renderToStaticMarkup(<ExecutionStatusPanel probe={probe(legacyStatus)} savedEnabled draftEnabled={null} />);
    expect(panel).not.toContain("还没保存");
    expect(panel).toContain("已开启，可以使用");
    const page = renderToStaticMarkup(<CapabilitySettings s={fakeSettings(legacy)} onManageSkills={() => {}} />);
    expect(page).toContain("当前服务不支持在网页里切换");
    expect(page).not.toContain("还没保存");
    expect(page).not.toContain('aria-checked="false" aria-label="运行代码"');
    // 状态还没读到时，开关位置写“读取中”，不画成关闭。
    expect(page).toContain("读取中…");
    expect(page).not.toContain('aria-label="运行代码"');
  });
});

describe("模型页与能力页共用一份草稿", () => {
  it("能力页改过但未保存，切到模型页仍可保存，回到能力页仍是草稿值", () => {
    const saved = { ...config, compaction_enabled: true };
    const draft = { ...saved, compaction_enabled: false, subagent: { ...saved.subagent, model: "gpt-6-sol-mini" } };
    const s = { ...fakeSettings(draft, saved), dirty: true } as RuntimeSettings;
    const models = renderToStaticMarkup(<RuntimeTab tab="models" s={s} onTab={() => {}} />);
    expect(models).toContain("有未保存的修改");
    expect(models).toMatch(/<button class="btn btn-primary">保存<\/button>/);
    const caps = renderToStaticMarkup(<RuntimeTab tab="capabilities" s={s} onTab={() => {}} />);
    expect(caps).toContain('aria-checked="false" aria-label="长对话自动整理"');
    // 一次保存同时带上两页的修改。
    const body = settingsUpdate(s.config!);
    expect(body).toMatchObject({ compaction_enabled: false, subagent: { model: "gpt-6-sol-mini" } });
  });
});

describe('执行后端切换', () => {
  it('保存请求保留 native，旧客户端不生成新字段', () => {
    expect(settingsUpdate({...config,execution_backend:'native'}).execution_backend).toBe('native');
    expect('execution_backend' in settingsUpdate(config)).toBe(false);
  });
  it('后端改变后不能沿用旧 Docker 检查结果', () => {
    expect(executionNowView(true,probe(status({backend:'docker'})),'native').stale).toBe(true);
  });
  it('native 显示实际隔离能力，不把 Docker 镜像/资源配额说成已生效', () => {
    const html=renderToStaticMarkup(<ExecutionStatusPanel savedEnabled={true} draftEnabled={true}
      savedBackend="native" draftBackend="docker"
      probe={probe(status({backend:'native',image:null,image_id:undefined,isolation:'macOS Seatbelt / SRT',session_dependencies:true}))} />);
    expect(html).toContain('本机系统沙箱');
    expect(html).toContain('没有 Docker 的 CPU');
    expect(html).toContain('尚未保存');
    expect(html).not.toContain('<dt>镜像 ID</dt>');
  });
});
