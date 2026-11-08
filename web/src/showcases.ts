/**
 * 示例（阶段 2 第 2 步 #47）：管理员挑出的研究，所有人只读可见，包括未登录。
 * 列表不要求登录；示例里各轮的内容走原来的查看接口。
 */
export type Showcase = {
  conversation_id: string;
  title: string;
  query: string;
  run_ids: string[];
  latest_run_id: string | null;
  turn_count: number;
  finished_at: number | null;
  available: boolean;
};

/** 示例列表；查不到时当作没有示例，不影响登录和使用。 */
export async function fetchShowcases(): Promise<Showcase[]> {
  try {
    const res = await fetch("/api/showcases");
    if (!res.ok) return [];
    const body = await res.json();
    return Array.isArray(body) ? body : [];
  } catch {
    return [];
  }
}

/** 这个研究属于哪个示例（不属于返回 null）。 */
export function showcaseOf(list: Showcase[], runId: string | null): Showcase | null {
  if (!runId) return null;
  return list.find((s) => s.run_ids.includes(runId)) ?? null;
}

/** 打开示例时先看哪一轮：最新一轮（对话页能切到前面各轮）。 */
export function showcaseEntry(showcase: Showcase): string | null {
  return showcase.available ? showcase.latest_run_id : null;
}
