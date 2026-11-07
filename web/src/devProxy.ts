import type { ProxyOptions } from "vite";

/**
 * 开发时 Vite（5173）把 /api 转给后端（8765）。
 *
 * 必须保留浏览器原来的 Host（changeOrigin: false）：后端对修改类请求检查 Origin 与 Host
 * 是否同站（ADR 0005、dra.api.deps.same_origin）。Vite 的字符串写法会默认 changeOrigin: true，
 * 把 Host 改成 127.0.0.1:8765，而 Origin 仍是 5173，登录、注册都会被当成跨站请求拒绝。
 */
export function apiProxy(target: string): Record<string, ProxyOptions> {
  return { "/api": { target, changeOrigin: false } };
}
