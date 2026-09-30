import { beforeEach, describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { BriefCard, type ResearchBrief } from "./components/BriefCard";
import { GapCard, type BriefGap } from "./components/GapCard";
import {
  apiStub, installBrowserStubs, jsxRuntimeStub, loadAppHandlers, reactStub,
} from "./testHarness";

/**
 * 开跑前的两段接进"直接研究"入口（EXPERIMENT_PLAN §7 第 1 项）。
 *
 * 三条必须成立的行为：
 * - 有缺口就**只问**：这时没有计划，也不该开跑；
 * - 答完（或跳过）才由第二段生成计划；
 * - 模型判"不需要确认"时不打断，直接带着计划开跑。
 */

const BRIEF: ResearchBrief = {
  original_query: "对比 A 和 B 两个方案",
  objective: "说清取舍并给出选型建议",
  requirements: [
    { id: "goal", text: "对比 A 和 B 两个方案", kind: "goal" },
    { id: "offline", text: "必须能离线跑", kind: "constraint" },
  ],
  route: [
    { id: "capability", text: "先看清两者的能力边界" },
    { id: "cost", text: "再比成本与维护代价" },
  ],
  assumptions: [],
  needs_confirmation: true,
  confirmation_reason: "路线存在真实取舍",
  trace: { plan_id: "plan-1", events: [] },
};

const GAPS: BriefGap[] = [
  {
    id: "scale",
    header: "确认",
    question: "你的服务规模大概是哪一档？这决定对比的重点。",
    options: [
      { label: "日活 10 万以内 (Recommended)", description: "重点放在迁移成本。" },
      { label: "日活百万级", description: "重点放在扩展性。" },
    ],
  },
];

let handlers: Record<string, any>;
let appFactory: () => Record<string, any>;

beforeEach(() => {
  installBrowserStubs();
  (globalThis as any).__state = [];
  (globalThis as any).__routes = new Map<string, any>();
  (globalThis as any).__streams = [];
  (globalThis as any).__replays = [];
  (globalThis as any).__posts = new Map<string, any>();
  (globalThis as any).__postBodies = [];
  (globalThis as any).__reactStub = reactStub();
  (globalThis as any).__apiStub = apiStub();
  (globalThis as any).__jsxRuntime = jsxRuntimeStub();
  appFactory = loadAppHandlers();
  handlers = appFactory();
  mockPost("/api/skills/prepare", () => ({skill_selection:"frozen-catalog"}));
});

function mockPost(path: string, response: unknown) {
  ((globalThis as any).__posts as Map<string, any>).set(path, response);
}

/** 发出去的 POST 请求体（前端送给后端的真实载荷）。 */
function bodies(): { url: string; body: any }[] {
  return (globalThis as any).__postBodies;
}

function cellMatching(predicate: (value: any) => boolean): any {
  const cells = (globalThis as any).__state as { value: any }[];
  // useRef 与 useState 共用编号，状态数组里可能有空位。
  return cells.find((c) => c?.value && predicate(c.value))?.value;
}

/** 页面停在计划确认卡上（brief 状态有值）。 */
function briefCell(): any {
  return cellMatching(
    (v) => typeof v === "object" && !Array.isArray(v) && "needs_confirmation" in v,
  );
}

/** 页面停在澄清卡上（gaps 状态有值）。 */
function gapsCell(): any {
  return cellMatching(
    (v) => Array.isArray(v) && v.length > 0 && typeof v[0]?.question === "string",
  );
}

async function flush() {
  for (let i = 0; i < 12; i++) await Promise.resolve();
}

describe("直接研究的开跑前流程", () => {
  it("没有缺口且不需要确认：不打断，直接带着计划开跑", async () => {
    mockPost("/api/brief", () => ({
      kind: "plan",
      brief: { ...BRIEF, needs_confirmation: false },
    }));
    mockPost("/api/research", () => ({ run_id: "run-direct" }));

    await handlers.handleSubmit("对比 A 和 B 两个方案");
    await flush();

    const research = bodies().find((b) => b.url === "/api/research");
    expect(research, "应当直接开跑").toBeTruthy();
    expect(research!.body.skill_selection).toBe("frozen-catalog");
    expect(bodies().find(b=>b.url==="/api/brief")!.body.skill_selection).toBe("frozen-catalog");
    expect(research!.body.brief.needs_confirmation).toBe(false);
    expect(research!.body.brief.trace).toBeUndefined();   // trace 不进执行契约
    expect(gapsCell(), "没有缺口就不该停在澄清卡上").toBeUndefined();
    expect(briefCell()).toBeUndefined();
  });

  it("规划途中推送的思考过程在结束后保留，供回看", async () => {
    mockPost("/api/brief", (onEvent: (e: any) => void) => {
      onEvent({ type: "stage", stage: "gaps" });
      onEvent({ type: "reasoning", stage: "gaps", text: "先读题" });
      return { kind: "clarify", gaps: GAPS };
    });

    await handlers.handleSubmit("对比 A 和 B 两个方案");
    await flush();

    expect(gapsCell()?.[0]?.id).toBe("scale");
    expect(cellMatching((v) => typeof v === "string" && v.includes("先读题"))).toContain("判断要不要先问你几个问题");
    expect(cellMatching((v) => typeof v === "object" && v !== null && "stageStartedAt" in v), "进度框已收起").toBeUndefined();
  });

  it("有缺口：停下来问，不开跑，也不拿计划来充数", async () => {
    mockPost("/api/brief", () => ({ kind: "clarify", gaps: GAPS }));
    mockPost("/api/research", () => ({ run_id: "不该开跑" }));

    await handlers.handleSubmit("对比 A 和 B 两个方案");
    await flush();

    expect(gapsCell()?.[0]?.id).toBe("scale");
    expect(briefCell(), "有缺口时计划还没生成").toBeUndefined();
    expect(bodies().some((b) => b.url === "/api/research")).toBe(false);
  });

  it("答完缺口：带着回答生成计划，再按需判断开跑还是确认", async () => {
    mockPost("/api/brief", () => ({ kind: "clarify", gaps: GAPS }));
    mockPost("/api/brief/plan", () => ({
      kind: "plan",
      brief: { ...BRIEF, needs_confirmation: false },
    }));
    mockPost("/api/research", () => ({ run_id: "run-answered" }));

    await handlers.handleSubmit("对比 A 和 B 两个方案");
    await flush();
    handlers = appFactory();
    await handlers.handleGapSubmit([
      { id: "scale", selected: ["日活 10 万以内 (Recommended)"], custom: "" },
    ]);
    await flush();

    const planPost = bodies().find((b) => b.url === "/api/brief/plan");
    expect(planPost!.body.gaps[0].id).toBe("scale");
    expect(planPost!.body.answers[0].selected).toEqual(["日活 10 万以内 (Recommended)"]);
    expect(bodies().some((b) => b.url === "/api/research")).toBe(true);
    expect(gapsCell()).toBeUndefined();
  });

  it("全部跳过：照样生成计划，并把没答的前提交给 assumptions", async () => {
    mockPost("/api/brief", () => ({ kind: "clarify", gaps: GAPS }));
    mockPost("/api/brief/plan", () => ({
      kind: "plan",
      brief: { ...BRIEF, assumptions: ["假设服务规模中等"] },
    }));

    await handlers.handleSubmit("对比 A 和 B 两个方案");
    await flush();
    handlers = appFactory();
    await handlers.handleGapSubmit([]);
    await flush();

    const planPost = bodies().find((b) => b.url === "/api/brief/plan");
    expect(planPost!.body.answers).toEqual([]);
    expect(briefCell()?.assumptions).toEqual(["假设服务规模中等"]);
  });

  it("修订：把这一轮反馈送进规划器，并替换计划内容", async () => {
    mockPost("/api/brief", () => ({ kind: "plan", brief: BRIEF }));
    mockPost("/api/brief/revise", () => ({
      kind: "plan",
      brief: {
        ...BRIEF,
        requirements: [
          ...BRIEF.requirements,
          { id: "budget", text: "预算不超过 1 万", kind: "constraint" },
        ],
      },
    }));

    await handlers.handleSubmit("对比 A 和 B 两个方案");
    await flush();
    handlers = appFactory();
    await handlers.handleBriefRevise("预算不超过 1 万");
    await flush();

    const revise = bodies().find((b) => b.url === "/api/brief/revise");
    expect(revise!.body.feedback).toBe("预算不超过 1 万");
    expect(briefCell().requirements.map((r: any) => r.id)).toEqual(["goal", "offline", "budget"]);
  });

  it("取消澄清：清掉卡片状态，回到输入态", async () => {
    mockPost("/api/brief", () => ({ kind: "clarify", gaps: GAPS }));
    await handlers.handleSubmit("对比 A 和 B 两个方案");
    await flush();
    expect(gapsCell()).toBeTruthy();

    handlers.handleDiscardGaps();
    await flush();
    expect(gapsCell()).toBeUndefined();
  });
});

describe("澄清卡的内容", () => {
  it("选项带取舍说明，且常驻自由输入与跳过入口都在", () => {
    const html = renderToStaticMarkup(
      <GapCard
        gaps={GAPS} busy={false} error={null}
        onSubmit={() => {}} onCancel={() => {}}
      />
    );
    expect(html).toContain(GAPS[0].question);
    expect(html).toContain("日活 10 万以内 (Recommended)");
    expect(html).toContain("重点放在迁移成本。");
    expect(html).toContain("补充说明");          // 选了也能再补一段话
    expect(html).toContain("跳过，直接开始");
    expect(html).toContain("继续");
    expect(html).toContain('role="radio"');     // 单选题用圆点
  });

  it("多选题用方框，可以同时选多个", () => {
    const html = renderToStaticMarkup(
      <GapCard
        gaps={[{ ...GAPS[0], multi_select: true }]} busy={false} error={null}
        onSubmit={() => {}} onCancel={() => {}}
      />
    );
    expect(html).toContain('role="checkbox"');
    expect(html).toContain("可多选");
  });
});

describe("计划卡的内容", () => {
  it("目标、硬要求、初步路线分列，并说明为什么需要确认", () => {
    const html = renderToStaticMarkup(
      <BriefCard
        brief={BRIEF} busy={false} hint={null} error={null}
        onStart={() => {}} onRevise={async () => {}} onCancel={() => {}}
      />
    );
    expect(html).toContain(BRIEF.objective);
    expect(html).toContain("要做到");
    expect(html).toContain("必须");
    expect(html).toContain("必须能离线跑");
    expect(html).toContain("打算怎么查");
    expect(html).toContain("先看清两者的能力边界");
    expect(html).toContain("需要你确认：路线存在真实取舍");
    expect(html).toContain("开始研究");
  });

  it("跳过了澄清时，把“它在猜什么”单列出来", () => {
    const html = renderToStaticMarkup(
      <BriefCard
        brief={{ ...BRIEF, assumptions: ["假设服务规模中等、痛点在 JSON 查询"] }}
        busy={false} hint={null} error={null}
        onStart={() => {}} onRevise={async () => {}} onCancel={() => {}}
      />
    );
    expect(html).toContain("会按这些假设进行");
    expect(html).toContain("假设服务规模中等、痛点在 JSON 查询");
  });

  it("干净档不显示规划器调试 I/O", () => {
    const html = renderToStaticMarkup(
      <BriefCard
        brief={BRIEF} busy={false} hint={null} error={null} mode="clean"
        onStart={() => {}} onRevise={async () => {}} onCancel={() => {}}
      />
    );
    expect(html).not.toContain("规划器调用");
  });
});
