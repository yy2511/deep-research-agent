import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { NewResearch } from "./components/NewResearch";
import { settingsUpdate, TrialSettingsTab, type ConfigPayload, type RuntimeSettings, type TrialSettings } from "./components/SettingsPage";
import { TrialNotice } from "./components/TrialNotice";
import { LOCAL_RUN_URL, resetTime, trialBlocked, trialSummary, type TrialStatus } from "./trial";

/** 公开试用的前端（#50）：访客看到剩余次数、用完时的说明；管理员的“试用”分页。 */

const reset = "2026-11-10T00:00:00+08:00";
const open: TrialStatus = { open: true, runs_left: 1, runs_limit: 2, global_exhausted: false, resets_at: reset };

describe("试用次数", () => {
  it("还能问时不拦，并说明剩几次", () => {
    expect(trialBlocked(open)).toBeNull();
    expect(trialBlocked(null)).toBeNull();
    expect(trialSummary(open)).toBe("今天还可以问 1 次（每天 2 次，追问也算一次）");
  });

  it("用完或未开放时给出原因，区分个人与全站", () => {
    expect(trialBlocked({ ...open, runs_left: 0 })).toMatch(/^你今天的试用次数用完了，.+恢复。$/);
    expect(trialBlocked({ ...open, runs_left: 0, global_exhausted: true })).toMatch(/^今天的试用名额已经用完了/);
    expect(trialBlocked({ ...open, open: false })).toBe("试用暂未开放。");
    expect(resetTime(reset)).toMatch(/^\d+ 月 \d+ 日 \d\d:\d\d$/);
    expect(resetTime("not-a-date")).toBe("");
  });

  it("提示条：用完时给出看示例、本机运行两个出口", () => {
    const left = renderToStaticMarkup(<TrialNotice trial={open} onShowcases={() => {}} />);
    expect(left).toContain("今天还可以问 1 次");
    expect(left).not.toContain("看看示例");
    const used = renderToStaticMarkup(<TrialNotice trial={{ ...open, runs_left: 0 }} onShowcases={() => {}} />);
    expect(used).toContain("你今天的试用次数用完了");
    expect(used).toContain("看看示例");
    expect(used).toContain(`href="${LOCAL_RUN_URL}"`);
    expect(renderToStaticMarkup(<TrialNotice trial={{ ...open, runs_left: 0 }} />)).not.toContain("看看示例");
  });

  it("提问框下显示提示，按钮按 canSubmit 变灰", () => {
    const html = renderToStaticMarkup(<NewResearch busy={false} error={null} onSubmit={() => {}} canSubmit={false}
      initialQuery="问题" notice={<TrialNotice trial={{ ...open, runs_left: 0 }} />} />);
    expect(html).toContain("你今天的试用次数用完了");
    expect(html).toMatch(/<button class="btn btn-primary" disabled="">/);
  });
});

describe("管理员的试用分页", () => {
  const trial: TrialSettings = {
    provider: "deepseek", model: "deepseek-chat", per_user_daily_runs: 2, global_daily_runs: 20,
    per_user_daily_briefs: 10, global_daily_briefs: 100, max_concurrent_guest_runs: 2,
    wall_timeout_s: 900, max_llm_calls: 60, max_tool_calls: 60, timezone: "Asia/Shanghai",
  };
  const config = { planner: { model: "m", provider: "p", effort: null }, subagent: { model: "m", provider: "p", effort: null }, trial } as ConfigPayload;
  const runtime = { config, setConfig: () => {}, providers: [{ name: "deepseek", label: "DeepSeek", key_configured: true, models: ["deepseek-chat"] }],
    error: null, notice: "", dirty: false, saving: false, load: () => {}, save: async () => {} } as unknown as RuntimeSettings;

  it("显示试用模型、次数、预算与时区", () => {
    const html = renderToStaticMarkup(<TrialSettingsTab s={runtime} />);
    for (const text of ["试用模型", "每人每天研究次数", "全部访客每天规划次数", "访客同时进行的研究", "单次研究时间上限（秒）", "Asia/Shanghai", "deepseek-chat", 'value="20"', 'value="900"']) {
      expect(html).toContain(text);
    }
  });

  it("保存时把试用设置一起提交", () => {
    expect(settingsUpdate(config).trial).toEqual(trial);
    const { trial: _omit, ...withoutTrial } = config;
    void _omit;
    expect("trial" in settingsUpdate(withoutTrial as ConfigPayload)).toBe(false);
  });
});
