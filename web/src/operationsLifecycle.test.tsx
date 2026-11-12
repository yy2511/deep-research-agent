/** 离线组件接线测试：只替换 React 调度与 fetch，不复制组件/请求业务逻辑。 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";

const hooks = vi.hoisted(() => ({ cells: [] as unknown[], cursor: 0, effects: [] as Array<() => void | (() => void)>, initialized: false }));
vi.mock("react", async importOriginal => ({
  ...await importOriginal<typeof import("react")>(),
  useState(initial: unknown) {
    const index = hooks.cursor++;
    if (!(index in hooks.cells)) hooks.cells[index] = typeof initial === "function" ? initial() : initial;
    return [hooks.cells[index], (next: unknown) => {
      hooks.cells[index] = typeof next === "function" ? next(hooks.cells[index]) : next;
    }];
  },
  useRef(initial: unknown) {
    const index = hooks.cursor++;
    if (!(index in hooks.cells)) hooks.cells[index] = { current: initial };
    return hooks.cells[index];
  },
  useEffect(effect: () => void | (() => void)) { if (!hooks.initialized) hooks.effects.push(effect); },
}));
import { OperationsDashboard, OperationsStatus } from "./components/OperationsDashboard";

function render() { hooks.cursor = 0; return OperationsDashboard(); }
function nodes(node: unknown): Array<ReactElement<any>> {
  if (Array.isArray(node)) return node.flatMap(nodes);
  if (!node || typeof node !== "object" || !("props" in node)) return [];
  const element = node as ReactElement<any>;
  return [element, ...nodes(element.props.children)];
}
function find(tree: unknown, type: unknown) { return nodes(tree).find(node => node.type === type)!; }
function status(tree: unknown) { return find(tree, OperationsStatus).props.state; }
const flush = () => new Promise(resolve => setTimeout(resolve, 0));
const empty = {
  as_of: "2026-11-12T10:00:00Z", timezone: "UTC", scope: "registered_database_records",
  range: { start_date: "2026-11-06", end_date: "2026-11-12" },
  totals: { users: 0, enabled_users: 0, disabled_users: 0, showcases: 0, roles: { guest: 0, user: 0, admin: 0 } },
  period: { new_users: 0, research_users: 0, research_tasks: 0, conversations: 0 }, daily: [],
};
let cleanup: Array<() => void> = [];
function mount() {
  const tree = render();
  cleanup = hooks.effects.map(effect => effect()).filter((f): f is () => void => typeof f === "function");
  hooks.initialized = true;
  return tree;
}

beforeEach(() => { hooks.cells = []; hooks.cursor = 0; hooks.effects = []; hooks.initialized = false; });
afterEach(() => { cleanup.forEach(fn => fn()); cleanup = []; vi.unstubAllGlobals(); });

describe("运营概览真实组件接线", () => {
  it("挂载请求默认范围，预设选择加载全部，离开时取消当前请求", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(empty)))
      .mockImplementationOnce(() => new Promise(() => {}));
    vi.stubGlobal("fetch", fetch);
    mount(); await flush();
    expect(fetch.mock.calls[0][0]).toBe("/api/admin/operations");
    const tree = render(); expect(status(tree).data).toEqual(empty);
    find(tree, "select").props.onChange({ target: { value: "all" } });
    expect(fetch.mock.calls[1][0]).toBe("/api/admin/operations?all_time=true");
    expect(status(render()).data).toBeNull();
    cleanup.forEach(fn => fn()); cleanup = [];
    expect(fetch.mock.calls[1][1].signal.aborted).toBe(true);
  });

  it("自定义日期只在提交时应用，刷新不误用未提交的编辑", async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify(empty))));
    vi.stubGlobal("fetch", fetch); mount(); await flush();
    find(render(), "select").props.onChange({ target: { value: "custom" } });
    let tree = render(); let inputs = nodes(tree).filter(node => node.type === "input");
    inputs[0].props.onChange({ target: { value: "2024-02-29" } });
    tree = render(); inputs = nodes(tree).filter(node => node.type === "input");
    inputs[1].props.onChange({ target: { value: "2024-03-02" } });
    expect(fetch).toHaveBeenCalledTimes(1);
    tree = render(); find(tree, "button").props.onClick(); await flush();
    expect(fetch.mock.calls[1][0]).toBe("/api/admin/operations");
    find(render(), "form").props.onSubmit({ preventDefault() {} }); await flush();
    expect(fetch.mock.calls[2][0]).toBe("/api/admin/operations?start_date=2024-02-29&end_date=2024-03-02");
  });

  it("加载期间禁用刷新，非权限错误不触发研究导航", async () => {
    const fetch = vi.fn().mockImplementation(() => new Promise(() => {}));
    vi.stubGlobal("fetch", fetch); mount();
    const tree = render(); expect(find(tree, "button").props.disabled).toBe(true);
    find(tree, "button").props.onClick(); expect(fetch).toHaveBeenCalledTimes(1);
    expect(nodes(tree).filter(node => node.type === "a")).toHaveLength(0);
  });
});
