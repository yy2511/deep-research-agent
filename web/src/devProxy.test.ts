import http from "node:http";
import type { AddressInfo } from "node:net";
import { createServer, type ProxyOptions, type ViteDevServer } from "vite";
import { afterEach, expect, it } from "vitest";
import viteConfig from "../vite.config";
import { apiProxy } from "./devProxy";

/**
 * 经过真实的 Vite 开发代理发一次登录请求，看后端实际收到的 Host 与 Origin。
 * 后端的同站检查要求两者一致（dra.api.deps.same_origin）。
 */
const closers: (() => Promise<void>)[] = [];
afterEach(async () => { while (closers.length) await closers.pop()!(); });

async function probeBackend(): Promise<{ url: string; seen: http.IncomingHttpHeaders[] }> {
  const seen: http.IncomingHttpHeaders[] = [];
  const server = http.createServer((req, res) => {
    seen.push(req.headers);
    res.setHeader("Content-Type", "application/json");
    res.end("{}");
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  closers.push(() => new Promise((resolve) => server.close(() => resolve())));
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`, seen };
}

async function viteWith(proxy: Record<string, ProxyOptions>): Promise<string> {
  const vite: ViteDevServer = await createServer({
    configFile: false, logLevel: "silent", root: process.cwd(),
    server: { host: "127.0.0.1", port: 0, proxy },
  });
  await vite.listen();
  closers.push(() => vite.close());
  return `127.0.0.1:${(vite.httpServer!.address() as AddressInfo).port}`;
}

async function loginThrough(viteHost: string) {
  await fetch(`http://${viteHost}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Origin: `http://${viteHost}` },
    body: JSON.stringify({ email: "a@example.com", password: "x" }),
  });
}

it("开发代理保留浏览器的 Host，后端看到的 Origin 与 Host 同站", async () => {
  const backend = await probeBackend();
  const viteHost = await viteWith(apiProxy(backend.url));
  await loginThrough(viteHost);
  expect(backend.seen).toHaveLength(1);
  expect(backend.seen[0].host).toBe(viteHost);
  expect(new URL(backend.seen[0].origin as string).host).toBe(backend.seen[0].host);
});

it("对照：Vite 的字符串写法会把 Host 改成后端地址，正是 #34 审查发现的问题", async () => {
  const backend = await probeBackend();
  const viteHost = await viteWith({ "/api": backend.url } as Record<string, ProxyOptions>);
  await loginThrough(viteHost);
  expect(backend.seen[0].host).toBe(new URL(backend.url).host);
  expect(new URL(backend.seen[0].origin as string).host).not.toBe(backend.seen[0].host);
});

it("vite.config.ts 用的就是这份代理配置", () => {
  expect((viteConfig as { server?: { proxy?: unknown } }).server?.proxy).toEqual(apiProxy("http://127.0.0.1:8765"));
});
