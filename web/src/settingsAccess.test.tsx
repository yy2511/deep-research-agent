import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SettingsPage } from "./components/SettingsPage";

/** 管理员权限（#32）：全站设置只有管理员能改，普通用户的设置页不显示修改入口。 */

const PREFS = { showRecords: false, showReplay: false, traceFull: false };
const GLOBAL_TABS = ["运营概览", "模型", "研究能力", "试用", "API 连接", "技能", "外部资料（MCP）"];

function render(isAdmin: boolean, tab: "models" | "skills" | "operations" = "models") {
  return renderToStaticMarkup(<SettingsPage isAdmin={isAdmin} tab={tab} onTab={() => {}} prefs={PREFS}
    onPrefs={() => {}} onDemo={() => {}} />);
}


describe("设置页按角色显示", () => {
  it("普通用户只看到通用设置和说明，没有全站设置的分页", () => {
    for (const tab of ["models", "skills", "operations"] as const) {
      const html = render(false, tab);
      for (const label of GLOBAL_TABS) expect(html).not.toContain(`>${label}</button>`);
      expect(html).toContain(">通用</button>");
      expect(html).toContain("由管理员统一设置");
      expect(html).toContain("演示研究");
    }
  });

  it("管理员保持全部分页", () => {
    const html = render(true);
    for (const label of [...GLOBAL_TABS, "通用"]) expect(html).toContain(`>${label}</button>`);
    expect(html).not.toContain("由管理员统一设置");
  });
});
