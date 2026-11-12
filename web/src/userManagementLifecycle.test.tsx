/** 真实组件接线；只替换 React 调度和 fetch，浏览器焦点/布局另行验收。 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import type { Account } from "./auth";
import type { ManagedUser } from "./userManagement";
const hooks = vi.hoisted(() => ({ cells: [] as unknown[], cursor: 0, effects: [] as Array<() => void | (() => void)>, initialized: false }));
vi.mock("react", async original => ({
  ...await original<typeof import("react")>(),
  useState(initial: unknown) {
    const index = hooks.cursor++;
    if (!(index in hooks.cells)) hooks.cells[index] = typeof initial === "function" ? initial() : initial;
    return [hooks.cells[index], (next: unknown) => { hooks.cells[index] = typeof next === "function" ? next(hooks.cells[index]) : next; }];
  },
  useRef(initial: unknown) { const index = hooks.cursor++; if (!(index in hooks.cells)) hooks.cells[index] = { current: initial }; return hooks.cells[index]; },
  useEffect(effect: () => void | (() => void)) { if (!hooks.initialized) hooks.effects.push(effect); },
}));
import { UserDetail, UserManagement } from "./components/UserManagement";
import { OwnerManagement } from "./components/OwnerManagement";

const account: Account = { id: 1, email: "owner@synthetic.test", role: "owner" };
const sample: ManagedUser = { id: 2, email: "member@synthetic.test", display_name: "合成普通用户", role: "user", status: "active", management_version: 7, created_at: "", quota: { mode: "limited", daily_runs_limit: "9223372036854775807", daily_briefs_limit: 3, version: 5 }, usage: null };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
function nodes(node: unknown): Array<ReactElement<any>> {
  if (Array.isArray(node)) return node.flatMap(nodes);
  if (!node || typeof node !== "object" || !("props" in node)) return [];
  const element = node as ReactElement<any>; return [element, ...nodes(element.props.children)];
}
function text(node: unknown): string { if (Array.isArray(node)) return node.map(text).join(""); if (typeof node === "string" || typeof node === "number") return String(node); return node && typeof node === "object" && "props" in node ? text((node as ReactElement<any>).props.children) : ""; }
function button(tree: unknown, label: string) { const found = nodes(tree).find(node => node.type === "button" && text(node) === label); if (!found) throw Error(`按钮不存在: ${label}`); return found; }
let cleanup: Array<() => void> = [];
let component: () => ReactElement;
function render() { hooks.cursor = 0; return component(); }
function mount() { const tree = render(); cleanup = hooks.effects.map(effect => effect()).filter((f): f is () => void => typeof f === "function"); hooks.initialized = true; return tree; }
beforeEach(() => { hooks.cells = []; hooks.cursor = 0; hooks.effects = []; hooks.initialized = false; vi.stubGlobal("document", { activeElement: null }); vi.stubGlobal("HTMLElement", class {}); });
afterEach(() => { cleanup.forEach(fn => fn()); cleanup = []; vi.unstubAllGlobals(); });

function detail(role: Account["role"] = "owner") {
  const callbacks = { onClose: vi.fn(), onSaved: vi.fn(), onTrial: vi.fn(), onDenied: vi.fn() };
  component = () => UserDetail({ id: 2, account: { ...account, role }, ...callbacks }); return callbacks;
}

describe("用户详情保存与权限", () => {
  it("关闭与取消确认不写入；确认保存以字符串发送额度与两个版本，双击不重复写", async () => {
    let done!: (response: Response) => void;
    const fetch = vi.fn().mockResolvedValueOnce(response(sample)).mockImplementationOnce(() => new Promise(resolve => { done = resolve; })).mockResolvedValueOnce(response(sample)); vi.stubGlobal("fetch", fetch);
    const callbacks = detail(); mount(); await flush();
    button(render(), "保存个人额度").props.onClick(); button(render(), "取消修改").props.onClick();
    expect(fetch).toHaveBeenCalledTimes(1);
    button(render(), "关闭").props.onClick(); expect(callbacks.onClose).toHaveBeenCalledOnce();
    button(render(), "保存个人额度").props.onClick(); const confirm = button(render(), "确认保存"); confirm.props.onClick(); confirm.props.onClick();
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toMatchObject({ daily_runs_limit: "9223372036854775807", daily_briefs_limit: "3", expected_version: 5, expected_management_version: 7 });
    expect(button(render(), "关闭").props.disabled).toBe(true);
    done(response({ ok: true })); await flush(); expect(callbacks.onSaved).toHaveBeenCalledOnce(); expect(fetch).toHaveBeenCalledTimes(3);
  });
  it("管理员没有无限额度选项；负责人目标没有编辑控件", async () => {
    const fetch = vi.fn().mockResolvedValue(response(sample)); vi.stubGlobal("fetch", fetch); detail("admin"); mount(); await flush();
    expect(nodes(render()).filter(node => node.type === "option").map(node => node.props.value)).not.toContain("unlimited");
    cleanup.forEach(fn => fn()); cleanup = []; hooks.cells = []; hooks.effects = []; hooks.initialized = false;
    fetch.mockResolvedValue(response({ ...sample, role: "owner" })); mount(); await flush();
    expect(nodes(render()).some(node => node.type === "fieldset")).toBe(false); expect(text(render())).toContain("该账号只能查看");
  });
  it("409 不重试写入，重新加载后需要重新确认", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(sample)).mockResolvedValueOnce(response({ detail: "冲突" }, 409)).mockResolvedValueOnce(response({ ...sample, management_version: 8 })); vi.stubGlobal("fetch", fetch); detail(); mount(); await flush();
    button(render(), "停用账号").props.onClick(); button(render(), "确认保存").props.onClick(); await flush();
    expect(fetch).toHaveBeenCalledTimes(3); expect(text(render())).toContain("账号已被其他管理者修改");
    expect(nodes(render()).some(node => node.type === "button" && text(node) === "确认保存")).toBe(false);
    button(render(), "停用账号").props.onClick(); expect(text(render())).toContain("确认停用账号");
  });
  it("403 清除详情并让父页面销毁失效列表", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response(sample)).mockResolvedValueOnce(response({ detail: "无权限" }, 403)); vi.stubGlobal("fetch", fetch); const callbacks = detail(); mount(); await flush();
    button(render(), "停用账号").props.onClick(); button(render(), "确认保存").props.onClick(); await flush();
    expect(callbacks.onDenied).toHaveBeenCalledOnce(); expect(text(render())).not.toContain(sample.email); expect(fetch).toHaveBeenCalledTimes(2);
  });
});

describe("用户列表生命周期", () => {
  it("详情拒绝权限后，晚到的列表刷新不能重新显示账号", async () => {
    let late!: (response: Response) => void;
    const list = { items: [sample], next_cursor: null, governance: { owner_user_id: 1, version: 1 } };
    const fetch = vi.fn().mockResolvedValueOnce(response(list)).mockImplementationOnce(() => new Promise(resolve => { late = resolve; })); vi.stubGlobal("fetch", fetch);
    component = () => UserManagement({ account, onTrial: vi.fn() }); mount(); await flush();
    const view = nodes(render()).find(node => node.type === "button" && text(node).startsWith("查看/编辑"))!; view.props.onClick();
    button(render(), "刷新").props.onClick();
    nodes(render()).find(node => node.type === UserDetail)!.props.onDenied();
    late(response(list)); await flush();
    expect(text(render())).not.toContain(sample.email); expect(button(render(), "刷新").props.disabled).toBe(true);
    expect(fetch.mock.calls[1][1].signal.aborted).toBe(true);
  });
  it("编辑搜索框不影响刷新，只有查询提交才应用", async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(response({ items: [], next_cursor: null, governance: { owner_user_id: 1, version: 1 } }))); vi.stubGlobal("fetch", fetch);
    component = () => UserManagement({ account, onTrial: vi.fn() }); mount(); await flush();
    nodes(render()).find(node => node.type === "input")!.props.onChange({ target: { value: " new@synthetic.test " } }); button(render(), "刷新").props.onClick(); await flush();
    expect(fetch.mock.calls[1][0]).toBe("/api/admin/users"); nodes(render()).find(node => node.type === "form")!.props.onSubmit({ preventDefault() {} }); await flush();
    expect(fetch.mock.calls[2][0]).toBe("/api/admin/users?q=new%40synthetic.test");
  });
});

describe("负责人初始化生命周期", () => {
  it("StrictMode 清理后的旧初始化响应不得覆盖第二次挂载数据", async () => {
    let resolveOld!: (response: Response) => void;
    const governance = { owner_user_id: 1, version: 2, reauth_methods: ["password"], reauth_ttl_seconds: 300 };
    let first = true;
    const fetch = vi.fn((url: string) => {
      if (url === "/api/admin/governance") { if (first) { first = false; return new Promise(resolve => { resolveOld = resolve; }); } return Promise.resolve(response(governance)); }
      if (url === "/api/admin/users/1") return Promise.resolve(response({ ...sample, id: 1, role: "owner" }));
      return Promise.resolve(response({ ready: false }));
    }); vi.stubGlobal("fetch", fetch);
    component = () => OwnerManagement({ account }); mount(); cleanup.forEach(fn => fn()); cleanup = [];
    hooks.effects = []; hooks.initialized = false; mount(); await flush();
    resolveOld(response({ ...governance, owner_user_id: 999 })); await flush();
    expect(text(render())).toContain("账号 ID 1"); expect(text(render())).not.toContain("账号 ID 999");
  });
});
