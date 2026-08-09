"""EventType → web/src/events.gen.ts 代码生成（契约守卫 v2 的生成端）。

为什么生成而不是前端手抄：旧版靠正则扫 _PAGE 对齐三处魔术字符串，仍吃过 3 处 drift；
生成物 + 新鲜度测试把对齐动作变成机械步骤，TS 侧再用 Record<EventTypeName, handler>
拿到编译期穷尽检查（漏 case 编不过）。
"""
from pathlib import Path

from dra.events import EventType

OUT_PATH = Path(__file__).resolve().parents[1] / "web" / "src" / "events.gen.ts"

def render() -> str:
    lines = [
        "// 由 scripts/gen_events_ts.py 从 dra.events.EventType 生成——手改无效。",
        "// 改 events.py 后重跑：uv run python scripts/gen_events_ts.py",
        "export const EVENT_TYPES = [",
        *[f'  "{e.value}",' for e in EventType],
        "] as const;",
        "export type EventTypeName = (typeof EVENT_TYPES)[number];",
    ]
    return "\n".join(lines) + "\n"

if __name__ == "__main__":
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(), encoding="utf-8")
    print(f"已生成 {OUT_PATH}")
