import { describe, expect, it } from "vitest";
import { settingsUpdate, SETTINGS_NODES, type ConfigPayload } from "./components/SettingsPage";

const config: ConfigPayload = {
  schema_version: 4,
  call_limits_enabled: false,
  planner: { provider: "opencode", model: "deepseek-v4.1-flash", effort: null },
  subagent: { provider: "opencode", model: "deepseek-v4.1-flash", reasoning: false, effort: null },
  simple_budget: { max_llm_calls: 20, max_tool_calls: 20, wall_timeout_s: 600,
    reserve_wrapup_calls: 1, storage_max_chars: 200000 },
  simple_model: { model: "previous", provider: "opencode" },
};

describe("模型设置保存契约", () => {
  it("次数限制开关显式保存，关闭不被漏掉", () => {
    expect(settingsUpdate(config).call_limits_enabled).toBe(false);
    expect(settingsUpdate({ ...config, call_limits_enabled: true }).call_limits_enabled).toBe(true);
  });
  it("只提交可写字段，未来增加的响应摘要也不能被回传", () => {
    const body = settingsUpdate({ ...config, future_readonly: "derived" } as ConfigPayload);
    expect(Object.keys(body).sort()).toEqual([
      "schema_version", "planner", "subagent", "call_limits_enabled", "compaction_enabled", "context_window_tokens", "evidence_judge",
    ].sort());
    expect(body.subagent.model).toBe("deepseek-v4.1-flash");
  });
  it("不修改原响应", () => {
    settingsUpdate(config);
    expect(config.simple_model?.model).toBe("previous");
  });
  it("现役界面不展示 writer 或已退役流程说明", () => {
    expect(SETTINGS_NODES.map(n => n.key)).toEqual(["planner", "subagent"]);
    expect(SETTINGS_NODES.map(n => n.label).join(" ")).not.toMatch(/独立核查|计划研究用|报告写作器/);
  });
});
