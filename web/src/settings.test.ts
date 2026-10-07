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
  it("浏览器开关保存false，旧响应没有字段时不补写", () => {
    expect(settingsUpdate({...config, browser: {enabled: false}}).browser).toEqual({enabled: false});
    expect(settingsUpdate(config)).not.toHaveProperty('browser');
  });
  it("扩展模式、浏览器和个人资料原样保存", () => {
    const browser = {enabled: true, mode: "extension" as const, extension_browser: "msedge" as const, profile_dir_name: "Profile 2"};
    expect(settingsUpdate({...config, browser}).browser).toEqual(browser);
    expect(settingsUpdate({...config, browser: {...browser, enabled: false}}).browser).toEqual({...browser, enabled: false});
  });
  it("次数限制开关显式保存，关闭不被漏掉", () => {
    expect(settingsUpdate(config).call_limits_enabled).toBe(false);
    expect(settingsUpdate({ ...config, call_limits_enabled: true }).call_limits_enabled).toBe(true);
  });
  it("只提交可写字段，未来增加的响应摘要也不能被回传", () => {
    const body = settingsUpdate({ ...config, future_readonly: "derived" } as ConfigPayload);
    expect(Object.keys(body).sort()).toEqual([
      "vision", "schema_version", "planner", "subagent", "call_limits_enabled", "compaction_enabled", "context_window_tokens", "context_window_mode", "max_output_tokens", "summary_max_output_tokens", "wrapup_max_output_tokens", "evidence_judge", "wall_timeout_s", "wrapup_reserve_s", "summary_timeout_s", "summary_max_retries",
    ].sort());
    expect(body.subagent.model).toBe("deepseek-v4.1-flash");
  });
  it("执行开关可写，null 原样提交表示跟随本机默认；只读摘要不回传", () => {
    const withExecution = { ...config, execution_enabled: null,
      execution_effective: { enabled: true, network_mode: "none", source: "environment" as const, default_enabled: true },
      retry_policy: { research: { max_retries: 10, max_attempts: 11 }, summary: { max_retries: 1, max_attempts: 2, timeout_s: 120 } } };
    const body = settingsUpdate(withExecution);
    expect(body).toHaveProperty("execution_enabled", null);
    expect(body).not.toHaveProperty("execution_effective");
    expect(body).not.toHaveProperty("retry_policy");
    expect(settingsUpdate({ ...withExecution, execution_enabled: false }).execution_enabled).toBe(false);
    // 服务端没返回这个字段（旧服务）时不提交，由服务端保留当前权限。
    expect(settingsUpdate(config)).not.toHaveProperty("execution_enabled");
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
