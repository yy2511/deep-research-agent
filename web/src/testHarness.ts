/**
 * 离线 App 行为测试夹具：只替换 React 调度与 HTTP/SSE 边界，业务代码用真实 App。
 *
 * 为什么需要它：导航的批次隔离、迟到响应、结果合并都是 async + 跨渲染的状态机，
 * 只测纯函数（BatchGuard / mergeSimpleResult）证明不了真实接线。这里把 useState 换成
 * 可读的单元格、把网络换成可控 route，既不引入 jsdom，也不复制任何 App 逻辑。
 */
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import type { ReactNode } from "react";

type Cell = { value: any };

/**
 * 把 App.tsx 源码插桩成"返回内部处理函数"的版本，
 * 用来在 vitest 里驱动真实的 async 导航序列。
 *
 * 插桩只做一件事：把 App 函数体最后一个 return（渲染 JSX）换成返回所有内部函数
 * 声明的对象。**不复制任何业务逻辑**，被测的始终是真实源码。
 */
export function loadAppHandlers(): () => Record<string, any> {
  const require_ = createRequire(path.join(process.cwd(), "package.json"));
  const ts = require_("typescript") as typeof import("typescript");
  void require_;
  const file = path.join(process.cwd(), "src", "App.tsx");
  const source = ts.createSourceFile(file, fs.readFileSync(file, "utf8"),
    ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let exposed: string[] = [];
  const transformed = ts.transform(source, [context => (tree: any) => ts.visitNode(tree, function visit(node: any): any {
    if (ts.isExportAssignment(node)) return undefined;
    if (ts.isFunctionDeclaration(node) && node.name?.text === "App" && node.body) {
      const body: any = node.body;
      exposed = body.statements.filter(ts.isFunctionDeclaration).map((s: any) => s.name.text);
      const statements = [...body.statements];
      const lastReturn = statements.map((s: any, i: number) => (ts.isReturnStatement(s) ? i : -1))
        .filter((i: number) => i >= 0).pop();
      if (lastReturn === undefined) throw new Error("App 没有可插桩的 return");
      statements[lastReturn] = ts.factory.createReturnStatement(
        ts.factory.createObjectLiteralExpression(exposed.map(name =>
          ts.factory.createShorthandPropertyAssignment(name))));
      return ts.factory.updateFunctionDeclaration(node, undefined, node.asteriskToken, node.name,
        node.typeParameters, node.parameters, node.type,
        ts.factory.updateBlock(body, statements));
    }
    return ts.visitEachChild(node, visit, context);
  })]);
  const printed = ts.createPrinter().printFile(transformed.transformed[0]) + "\nexport default App;\n";
  transformed.dispose();
  const js = ts.transpileModule(printed, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
  }).outputText;
  const factory = new Function("require", "module", "exports", js);
  const module_ = { exports: {} as any };
  const appRequire = (name: string) => load(name, file);
  // 依赖解析：只把"外部边界"换成替身（react / api / css），其余（reducer、组件、
  // 图标）全部按真实源码加载——测试驱动的是真实接线，不是复制品。
  const cache = new Map<string, any>();
  const load = (name: string, fromFile: string): any => {
    if (name.endsWith(".css")) return {};
    if (name === "react") return (globalThis as any).__reactStub;
    if (name === "react/jsx-runtime" || name === "react/jsx-dev-runtime") {
      return (globalThis as any).__jsxRuntime;
    }
    if (name.endsWith("/api") || name === "./api") return (globalThis as any).__apiStub;
    const base = path.resolve(path.dirname(fromFile), name);
    const found = [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts")]
      .find((candidate) => fs.existsSync(candidate) && fs.statSync(candidate).isFile());
    if (!found) throw new Error("无法解析模块: " + name + " from " + fromFile);
    if (cache.has(found)) return cache.get(found);
    const loaded = loadModule(found);
    cache.set(found, loaded);
    return loaded;
  };
  const loadModule = (file: string): any => {
    const src = fs.readFileSync(file, "utf8");
    const js = ts.transpileModule(src, {
      fileName: file,
      compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true },
    }).outputText;
    const module_ = { exports: {} as any };
    new Function("require", "module", "exports", js)(
      (name: string) => load(name, file), module_, module_.exports);
    return module_.exports;
  };
  factory(appRequire, module_, module_.exports);
  const exported = (module_.exports.default ?? module_.exports) as Record<string, any>;
  if (typeof exported !== "function") {
    throw new Error("插桩失败：App 未导出可调用组件（exports keys: "
      + Object.keys(module_.exports).join(",") + "）");
  }
  // 每次调用都算一次"渲染"：useState 槽位从 0 重新数（见 reactStub.useState）。
  // 这样同一个测试里可以先触发一次状态更新，再取一次 handlers 去驱动依赖最新 state 的交互。
  return (() => {
    (globalThis as any).__renderIndex = 0;
    return exported();
  }) as unknown as () => Record<string, any>;
}

interface Harness {
  streams: { id: string; onEvent: (e: any) => void; onDrop: () => void; closed: boolean }[];
  replays: { events: any[]; dispatch: (e: any, atMs: number) => void; options: any; stopped: boolean }[];
}

/** JSX 自动运行时替身：只造一个轻量元素对象，本夹具不渲染 DOM。 */
export function jsxRuntimeStub() {
  const make = (type: any, props: any, key?: any) => ({ type, props: props ?? {}, key });
  return { jsx: make, jsxs: make, Fragment: "Fragment", jsxDEV: make };
}

/** vitest 的模块替身工厂（在测试文件里 `vi.mock("react", ...)` 里调用）。 */
export function reactStub() {
  const state = (): Cell[] => {
    if (!(globalThis as any).__state) (globalThis as any).__state = [];
    return (globalThis as any).__state as Cell[];
  };
  return {
    useState(initial: any) {
      const cells = state();
      // 按**渲染序号**复用单元格，而不是每次渲染都 push 一批新的：
      // 真实 React 的 useState 就按调用顺序复用槽位。push 式会让"状态更新后再取一次
      // handlers"落到一批全新单元格上，读到的仍是初始值——于是任何依赖最新 state 的
      // 交互（例如"卡片上点修订"）都无法驱动。首次渲染时 cells 为空，行为与原来一致。
      const index = ((globalThis as any).__renderIndex as number) ?? 0;
      (globalThis as any).__renderIndex = index + 1;
      if (!cells[index]) {
        cells[index] = { value: typeof initial === "function" ? initial() : initial };
      }
      const cell = cells[index];
      return [cell.value, (next: any) => {
        cell.value = typeof next === "function" ? next(cell.value) : next;
      }];
    },
    useRef(value: any) {
      // 与 useState 同一套槽位语义：重新渲染时拿到的是同一个 ref 对象，
      // 否则"状态更新后再取一次 handlers"会让 queryRef 之类的暂存值凭空消失。
      const refs = ((globalThis as any).__refs ??= []) as { current: any }[];
      const index = ((globalThis as any).__renderIndex as number) ?? 0;
      (globalThis as any).__renderIndex = index + 1;
      if (!refs[index]) refs[index] = { current: value };
      return refs[index];
    },
    useEffect: () => {},
    useCallback: (fn: any) => fn,
    useMemo: (fn: any) => fn(),
    default: {},
  };
}

export function apiStub() {
  return {
    ApiError: class ApiError extends Error {
      trace?: unknown;
    },
    getJSON: async (url: string) => {
      const routes = (globalThis as any).__routes as Map<string, any>;
      if (!routes.has(url)) throw new Error("Unmocked HTTP boundary: " + url);
      const response = routes.get(url);
      return typeof response === "function" ? response() : response;
    },
    postJSON: async (url: string, body?: unknown) => {
      const posts = (globalThis as any).__posts as Map<string, any>;
      if (!posts?.has(url)) throw new Error("Unmocked POST boundary: " + url);
      // 记录真实请求体：前端把什么送给后端（例如 brief）是契约的一部分，
      // 只断言"发过请求"证明不了载荷对不对。
      (globalThis as any).__postBodies?.push({ url, body });
      const response = posts.get(url);
      return typeof response === "function" ? response() : response;
    },
    /** 与 postJSON 同一张路由表；路由函数收到 onEvent，可模拟规划途中的进度推送。 */
    postStream: async (url: string, body: unknown, onEvent: (e: any) => void) => {
      const posts = (globalThis as any).__posts as Map<string, any>;
      if (!posts?.has(url)) throw new Error("Unmocked POST boundary: " + url);
      (globalThis as any).__postBodies?.push({ url, body });
      const response = posts.get(url);
      return typeof response === "function" ? response(onEvent) : response;
    },
    putJSON: async () => {
      throw new Error("No PUT in this harness");
    },
    fetchNdjson: async (url: string) => {
      const routes = (globalThis as any).__routes as Map<string, any>;
      if (!routes.has(url)) throw new Error("Unmocked event boundary: " + url);
      const rows = routes.get(url);
      if (typeof rows === "function") return rows();
      if (rows instanceof Error) throw rows;
      return rows;
    },
    openStream: (id: string, onEvent: (e: any) => void, onDrop: () => void) => {
      const streams = (globalThis as any).__streams as Harness["streams"];
      const stream = { id, onEvent, onDrop, closed: false };
      streams.push(stream);
      return () => { stream.closed = true; };
    },
    /** 与真实 api.replay 同构，并允许测试手动推进定时器。 */
    replay: (events: any[], dispatch: (e: any, atMs: number) => void,
             options: { mode: "instant" | "paced"; targetMs?: number; onDone?: () => void }) => {
      const replays = (globalThis as any).__replays as Harness["replays"];
      const timers = (globalThis as any).__timers as (() => void)[];
      const item = { events, dispatch, options, stopped: false };
      replays.push(item);
      if (options.mode === "instant") {
        events.forEach((e) => dispatch(e, e.ts * 1000));
        options.onDone?.();
        return { stop: () => { item.stopped = true; } };
      }
      if (events.length > 0) {
        dispatch(events[0], events[0].ts * 1000);
        const rest = events.slice(1);
        rest.forEach((e, i) => {
          timers.push(() => {
            dispatch(e, e.ts * 1000);
            if (i === rest.length - 1) options.onDone?.();
          });
        });
        if (rest.length === 0) options.onDone?.();
      } else {
        options.onDone?.();
      }
      return { stop: () => { item.stopped = true; } };
    },
  };
}

export function installBrowserStubs() {
  if (!(globalThis as any).__memory) (globalThis as any).__memory = new Map<string, string>();
  if (!(globalThis as any).__routes) (globalThis as any).__routes = new Map<string, any>();
  if (!(globalThis as any).__streams) (globalThis as any).__streams = [];
  if (!(globalThis as any).__replays) (globalThis as any).__replays = [];
  if (!(globalThis as any).__timers) (globalThis as any).__timers = [];
  if (!(globalThis as any).__state) (globalThis as any).__state = [];
  // useRef 与 useState 共用一套按渲染序号复用的 hook 槽位（见 reactStub）。
  if (!(globalThis as any).__refs) (globalThis as any).__refs = [];
  (globalThis as any).__renderIndex = 0;
  const memory = (globalThis as any).__memory as Map<string, string>;
  (globalThis as any).localStorage = {
    getItem: (k: string) => memory.get(k) ?? null,
    setItem: (k: string, v: string) => { memory.set(k, v); },
    removeItem: (k: string) => { memory.delete(k); },
  };
  (globalThis as any).window = {
    location: { href: "http://localhost/", search: "", pathname: "/" },
    history: { replaceState() {}, pushState() {} },
    addEventListener() {},
    removeEventListener() {},
    innerHeight: 800,
  };
  (globalThis as any).document = { scrollingElement: { scrollTop: 0 } };
}

export type { ReactNode };
