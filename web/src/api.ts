/** 规划失败 422 时后端可在 detail 里附带 trace（llm_call 等），挂在 Error 上供调试 UI。 */
export class ApiError extends Error {
  trace?: { plan_id?: string | null; events?: any[] };
  constructor(message: string, trace?: { plan_id?: string | null; events?: any[] }) {
    super(message);
    this.name = "ApiError";
    this.trace = trace;
  }
}

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, options);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = body.detail;
    if (detail && typeof detail === "object" && typeof detail.message === "string") {
      throw new ApiError(detail.message, detail.trace);
    }
    throw new ApiError(
      typeof detail === "string" && detail ? detail : (res.statusText || "请求失败"),
    );
  }
  return res.json();
}

export async function postJSON<T>(url: string, body: unknown): Promise<T> {
  return request<T>(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
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
  const es = new EventSource(`/api/research/${runId}/stream`);
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
  opts: { mode: "instant" | "paced"; targetMs?: number }
): { stop: () => void } {
  if (opts.mode === "instant") {
    for (const e of events) dispatch(e, e.ts * 1000);
    return { stop: () => {} };
  }
  const targetMs = opts.targetMs ?? 45000;
  const timers: ReturnType<typeof setTimeout>[] = [];
  if (events.length > 0) {
    const firstTs = events[0].ts;
    const lastTs = events[events.length - 1].ts;
    const span = lastTs - firstTs;
    dispatch(events[0], events[0].ts * 1000);   // 首事件同步立即派发，不进 setTimeout 队列
    for (const e of events.slice(1)) {
      const delay = span > 0 ? ((e.ts - firstTs) / span) * targetMs : 0;
      timers.push(setTimeout(() => dispatch(e, e.ts * 1000), delay));
    }
  }
  return { stop: () => timers.forEach(clearTimeout) };
}
