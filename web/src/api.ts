/** 规划失败 422 时后端可在 detail 里附带 trace（llm_call 等），挂在 Error 上供调试 UI。 */
export class ApiError extends Error {
  trace?: { plan_id?: string | null; events?: any[] };
  constructor(message: string, trace?: { plan_id?: string | null; events?: any[] }) {
    super(message);
    this.name = "ApiError";
    this.trace = trace;
  }
}

/** 把后端的 detail（字符串或 {message, trace}）转成 ApiError。 */
function apiError(detail: unknown, fallback: string): ApiError {
  if (detail && typeof detail === "object" && typeof (detail as any).message === "string") {
    return new ApiError((detail as any).message, (detail as any).trace);
  }
  return new ApiError(typeof detail === "string" && detail ? detail : (fallback || "请求失败"));
}

export async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw apiError(body.detail, res.statusText);
  }
  return res.json();
}

/** 规划过程中推送的消息：进入哪一步，或一段思考内容。 */
export type PlannerEvent =
  | { type: "stage"; stage: "gaps" | "plan" }
  | { type: "reasoning"; stage: "gaps" | "plan"; text: string };

/**
 * 边算边推的规划请求：途中的进度交给 onEvent，最后返回结果（与 postJSON 的返回值相同）。
 * 出错时抛出与 postJSON 相同的 ApiError。
 */
export async function postStream<T>(url: string, body: unknown, onEvent: (e: PlannerEvent) => void): Promise<T> {
  const res = await fetch(`${url}?stream=true`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => ({}));
    throw apiError(data.detail, res.statusText);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, cut).trim();
      buffer = buffer.slice(cut + 1);
      if (!line) continue;
      const event = JSON.parse(line);
      if (event.type === "result") return event.data as T;
      if (event.type === "error") throw apiError(event.detail, "规划失败");
      onEvent(event as PlannerEvent);
    }
    if (done) break;
  }
  throw new ApiError("规划连接中断，请重试；你的问题仍保留在输入框中。");
}

export async function postJSON<T>(url: string, body: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

export async function getJSON<T>(url: string): Promise<T> {
  return request<T>(url);
}

export async function putJSON<T>(url: string, body: unknown): Promise<T> {
  return request<T>(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function fetchNdjson(url: string): Promise<any[]> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(res.statusText);
  const text = await res.text();
  return text.trim().split("\n").filter(Boolean).map((line) => JSON.parse(line));
}

export function openStream(
  runId: string, onEvent: (e: any) => void, onDrop: () => void
): () => void {
  const es = new EventSource(`/api/research/${runId}/stream?compact=true`);
  let gotEnd = false;
  es.onmessage = (msg) => {
    const evt = JSON.parse(msg.data);
    onEvent(evt);
    if (evt.type === "end") {
      gotEnd = true;
      es.close();
    }
  };
  es.onerror = () => {
    if (gotEnd) return;   // 我们自己 close() 触发的收尾错误，不是真掉线
    onDrop();
    // 未收到 end 就掉线：不主动 close，交给 EventSource 自身重连
    // （服务端补发 backlog + reducer 按 seq 去重兜底，见 Task 6）
  };
  return () => es.close();
}

export function replay(
  events: any[],
  dispatch: (e: any, atMs: number) => void,
  opts: { mode: "instant" | "paced"; targetMs?: number; onDone?: () => void }
): { stop: () => void } {
  if (opts.mode === "instant") {
    for (const e of events) dispatch(e, e.ts * 1000);
    opts.onDone?.();
    return { stop: () => {} };
  }
  const targetMs = opts.targetMs ?? 45000;
  const timers: ReturnType<typeof setTimeout>[] = [];
  if (events.length > 0) {
    const firstTs = events[0].ts;
    const lastTs = events[events.length - 1].ts;
    const span = lastTs - firstTs;
    dispatch(events[0], events[0].ts * 1000);   // 首事件同步立即派发，不进 setTimeout 队列
    const rest = events.slice(1);
    rest.forEach((e, i) => {
      const delay = span > 0 ? ((e.ts - firstTs) / span) * targetMs : 0;
      timers.push(setTimeout(() => {
        dispatch(e, e.ts * 1000);
        // 最后一条派发完才算"放完"：结果在这之后合并，快放不会一开始就摆出终稿。
        if (i === rest.length - 1) opts.onDone?.();
      }, delay));
    });
    if (rest.length === 0) opts.onDone?.();
  } else {
    opts.onDone?.();
  }
  return { stop: () => timers.forEach(clearTimeout) };
}
