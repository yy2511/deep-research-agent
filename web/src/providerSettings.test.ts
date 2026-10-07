import { describe, expect, it } from "vitest";
import type { ConfigPayload } from "./components/SettingsPage";
import { selectModel, effortOptions, type ProviderStatus } from "./providerSettings";

const config: ConfigPayload = {
  planner: { provider: "old", model: "old-model", effort: "high" },
  subagent: { provider: "old", model: "old-model", reasoning: true, effort: "high" },
  compaction_enabled: true, context_window_tokens: 200000,
};
const provider: ProviderStatus = {
  name: "custom_test", label: "测试连接", custom: true, key_configured: true,
  base_url: "https://test.invalid/v1", reachable: true, models: ["known", "unknown"],
  model_details: [{ id: "known", context_window: 96000, reasoning: true,
    reasoning_efforts: ["high", "max"], sources: { context_window: "api" } }],
};

describe("模型能力与切换", () => {
  it("切换研究模型保留用户工作目标，清空模型档位", () => {
    const next = selectModel(config, "subagent", provider, "known");
    expect(next.subagent).toEqual({ provider: "custom_test", model: "known", reasoning: true, effort: null });
    expect(next.context_window_tokens).toBe(200000);
    expect(next.compaction_enabled).toBe(true);
    expect(config.context_window_tokens).toBe(200000);
  });
  it("未知路由不抹掉已批准工作窗口或关闭整理；自动模式也不静默切换", () => {
    const next = selectModel(config, "subagent", provider, "unknown");
    expect(next.context_window_tokens).toBe(200000);
    expect(next.compaction_enabled).toBe(true);
    expect(next.subagent.effort).toBeNull();
    expect(selectModel({ ...config, context_window_mode: "auto" }, "subagent", provider, "unknown").context_window_mode).toBe("auto");
  });
  it("切换规划模型不改研究容量，获取已知容量不擅自开启自动整理", () => {
    expect(selectModel(config, "planner", provider, "known").context_window_tokens).toBe(200000);
    expect(selectModel({ ...config, compaction_enabled: false }, "subagent", provider, "known").compaction_enabled).toBe(false);
  });
  it("已知档位按服务返回，未知不猜 low/medium/high，旧值显示未确认", () => {
    expect(effortOptions(provider.model_details[0], null).map(x => x.value)).toEqual(["", "high", "max"]);
    expect(effortOptions(undefined, null).map(x => x.value)).toEqual([""]);
    expect(effortOptions(provider.model_details[0], "ultra").at(-1)?.label).toContain("未确认");
  });
});
