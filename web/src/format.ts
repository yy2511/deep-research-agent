import type { SourceSelection } from './components/LibraryPanel';

/** 把毫秒时长说成人话：48 秒 / 2 分 14 秒 / 1 小时 3 分。 */
export function durationText(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600), m = Math.floor((total % 3600) / 60), s = total % 60;
  if (h) return `${h} 小时 ${m} 分`;
  if (m) return `${m} 分 ${s} 秒`;
  return `${s} 秒`;
}

/** 今天 14:02 / 昨天 / 9月25日 / 2025年9月25日 */
export function relativeTime(ts?: number, now: Date = new Date()): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(now) - day(d)) / 86400000);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (diff === 0) return `今天 ${hm}`;
  if (diff === 1) return "昨天";
  if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}月${d.getDate()}日`;
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

/** 来源按钮上的文字：网页 / 仅我的资料 / 网页 + 资料，再加外部资料数量。 */
export function sourceLabel(value: SourceSelection): string {
  const base = value.source_mode === 'web' ? '网页' : value.source_mode === 'library' ? '仅我的资料' : '网页 + 资料';
  if (value.source_mode === 'library') return base;
  const mcp = value.mcp_server_ids?.length ?? 0;
  const blocked = value.blocked_sites?.length ?? 0;
  return [base, mcp ? `外部资料 ${mcp}` : '', blocked ? `屏蔽 ${blocked} 个网站` : ''].filter(Boolean).join(' · ');
}
