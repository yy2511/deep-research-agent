import { beforeEach, describe, expect, it } from "vitest";
import { apiStub, installBrowserStubs, jsxRuntimeStub, loadAppHandlers, reactStub } from "./testHarness";

const globals = globalThis as any;
const brief = { original_query: "question", objective: "answer", requirements: [], route: [], assumptions: [], needs_confirmation: false };
const plan = { kind: "plan", brief };
let render: () => Record<string, any>;
let app: Record<string, any>;
const posts = () => globals.__postBodies as {url: string; body: any}[];
const mock = (url: string, value: any) => globals.__posts.set(url, value);
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
function deferred() {
  let resolve!: (value: any) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  globals.__state = []; globals.__refs = []; globals.__memory = new Map();
  installBrowserStubs();
  globals.window.history.replaceState = (_state: unknown, _title: string, url: string) => {
    const next = new URL(url);
    globals.window.location = { href: next.href, search: next.search, pathname: next.pathname };
  };
  globals.__routes = new Map(); globals.__posts = new Map(); globals.__postBodies = [];
  globals.__streams = []; globals.__replays = [];
  globals.__reactStub = reactStub(); globals.__apiStub = apiStub(); globals.__jsxRuntime = jsxRuntimeStub();
  render = loadAppHandlers(["briefing", "submitting", "brief", "gaps", "briefError", "startError", "planProgress", "runId"]);
  app = render();
  mock("/api/skills/prepare", { skill_selection: "catalog" });
  mock("/api/brief", plan);
  mock("/api/research", { run_id: "new-run" });
  globals.__routes.set("/api/runs/history", { meta: { status: "done", replay_compatible: false } });
  globals.__routes.set("/api/runs/history/simple", { query: "history", markdown: "saved", cards: [], sources: [] });
});

async function navigate(to: string) {
  if (to === "new") app.openNewResearch();
  else if (to === "history") { app.openHistoryRun("history", "instant"); await flush(); }
  else app.openLiveRun("live");
}

describe("planning request navigation isolation", () => {
  it.each(["new", "history", "live"])("ignores a late brief after %s navigation", async to => {
    const pending = deferred(); mock("/api/brief", () => pending.promise);
    const request = app.handleSimpleSubmit("old question"); await flush();
    await navigate(to);
    expect(render().briefing).toBe(false);
    pending.resolve(plan); await request;
    expect(posts().filter(p => p.url === "/api/research")).toHaveLength(0);
    expect(render().runId).toBe(to === "new" ? null : to);
    expect(new URLSearchParams(globals.window.location.search).get("run")).toBe(to === "new" ? null : to);
    expect(globals.__memory.get("dra_active_run_id")).toBe(to === "new" ? undefined : to);
  });

  it("does not even request a brief when skill preparation finishes after navigation", async () => {
    const pending = deferred(); mock("/api/skills/prepare", () => pending.promise);
    const request = app.handleSimpleSubmit("old"); app.openNewResearch();
    pending.resolve({ skill_selection: "old" }); await request;
    expect(posts().map(p => p.url)).toEqual(["/api/skills/prepare"]);
  });

  it.each(["success", "error"])("old %s, progress and finally cannot alter a newer request", async outcome => {
    const old = deferred(); const next = deferred(); let oldEvent!: (e: any) => void;
    mock("/api/brief", (event: any) => { oldEvent = event; return old.promise; });
    const first = app.handleSimpleSubmit("old"); await flush();
    app.openNewResearch();
    mock("/api/brief", () => next.promise);
    const second = render().handleSimpleSubmit("new"); await flush();
    const progress = render().planProgress;
    oldEvent({ type: "reasoning", stage: "plan", text: "STALE" });
    if (outcome === "success") old.resolve(plan); else old.reject(new Error("STALE"));
    await first;
    expect(render().briefing).toBe(true);
    expect(render().planProgress).toEqual(progress);
    expect(render().briefError).toBeNull();
    expect(posts().filter(p => p.url === "/api/research")).toHaveLength(0);
    next.resolve(plan); await second;
    expect(posts().find(p => p.url === "/api/research")?.body.query).toBe("new");
    expect(render().runId).toBe("new-run");
    expect(render().briefing).toBe(false);
  });

  it("auto-starts normal submissions once, including repeated clicks before rerender", async () => {
    const first = app.handleSimpleSubmit("normal");
    const duplicate = app.handleSimpleSubmit("duplicate");
    await Promise.all([first, duplicate]);
    expect(posts().filter(p => p.url === "/api/research")).toHaveLength(1);
    expect(render().runId).toBe("new-run");
    expect(render().submitting).toBe(false);
  });

  it.each(["success", "error"])("late research start %s cannot take over the selected history", async outcome => {
    const pending = deferred(); mock("/api/research", () => pending.promise);
    const request = app.handleSimpleSubmit("old"); await flush();
    await navigate("history");
    if (outcome === "success") pending.resolve({run_id: "stale-run"}); else pending.reject(new Error("STALE"));
    await request;
    expect(render().runId).toBe("history");
    expect(render().startError).toBeNull();
    expect(globals.__streams).toHaveLength(0);
  });

  it.each(["gaps", "revise"])("discard invalidates an in-flight %s request", async kind => {
    const pending = deferred();
    mock("/api/brief", kind === "gaps" ? {kind: "clarify", gaps: [{id: "scope"}]} : {kind: "plan", brief: {...brief, needs_confirmation: true}});
    await app.handleSimpleSubmit("question"); app = render();
    mock(kind === "gaps" ? "/api/brief/plan" : "/api/brief/revise", () => pending.promise);
    const request = kind === "gaps" ? app.handleGapSubmit([]) : app.handleBriefRevise("change");
    if (kind === "gaps") app.handleDiscardGaps(); else app.handleDiscardBrief();
    pending.resolve(plan); await request;
    expect(render().brief).toBeNull(); expect(render().gaps).toBeNull();
    expect(render().submitting).toBe(false);
    expect(posts().filter(p => p.url === "/api/research")).toHaveLength(0);
  });

  it("unmount invalidates pending callbacks without state writes", async () => {
    const cleanups: (() => void)[] = [];
    globals.__reactStub.useEffect = (effect: () => unknown) => { const cleanup = effect(); if (typeof cleanup === "function") cleanups.push(cleanup as () => void); };
    app = render();
    const pending = deferred(); mock("/api/brief", () => pending.promise);
    const request = app.handleSimpleSubmit("old"); await flush();
    cleanups.forEach(cleanup => cleanup());
    const state = JSON.stringify(globals.__state);
    pending.resolve(plan); await request;
    expect(JSON.stringify(globals.__state)).toBe(state);
    expect(posts().filter(p => p.url === "/api/research")).toHaveLength(0);
  });
});
