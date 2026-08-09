"""事件契约守卫：EventType 单一真相源 vs 管线消费方（真实 emit）。

为什么要这条测试（见 DEVLOG 2026-07-01 契约收敛）
------------------------------------------------
SSE 事件契约天然复制在多处：orchestrator 等**真实 emit**、以及（历史上）
web._run_demo 假发 / web._PAGE 前端 handle。Python 侧（emit + demo）曾由
EventType 枚举在导入期锁住类型名；但前端是 JS 字符串，跨语言 import 不了枚举，
这条测试曾用正则扫 _PAGE + 跑 _run_demo + 扫管线源码，把 Python↔JS 这一侧的
drift 也钉死。

2026-07-07（web 工作台重写，Task 2）：stdlib 单页 `_PAGE`/`_run_demo` 已随
web.py 换底 FastAPI 退役，依赖它们的契约测试一并移除。前端契约守卫已换代为
events.gen.ts 生成物比对，见 tests/test_events_gen.py（Task 3）。本文件只留
Python 侧仍然成立的两条：枚举值不重复、管线源码里的 EventType 引用都合法。

纯逻辑、不起服务器、不联网、秒级。
"""

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from dra.events import EventType  # noqa: E402

_SRC = _ROOT / "src" / "dra"
_DECLARED = {e.value for e in EventType}


def _runtime_emitted_types() -> set[str]:
    """扫当前运行时源码里 EventType.X 引用 + web 层补发的 3 个（report_md/error/end）。"""
    emitted: set[str] = set()
    for fname in ("orchestrator.py", "subagent.py", "llm.py", "timing.py", "toolloop.py"):
        src = (_SRC / fname).read_text(encoding="utf-8")
        for name in re.findall(r"EventType\.([A-Z_]+)", src):
            emitted.add(EventType[name].value)  # 名不存在会 KeyError = 源码引用了不存在的 EventType
    # web._run_research 补发（ev_q.put，非核心 emit，但属前端契约）
    emitted |= {EventType.REPORT_MD.value, EventType.ERROR.value, EventType.END.value}
    return emitted


def test_event_type_values_unique():
    """当前事件成员值互不重复（防手滑写重名值 → 别名静默吞事件）。"""
    values = [e.value for e in EventType]
    assert len(values) == len(set(values)) == 22


def test_runtime_event_type_references_are_valid_members():
    """运行时源码（orchestrator/subagent/llm/timing/toolloop）里所有 EventType.X 引用
    都必须是真实存在的枚举成员——名字打错/改名漏更新在这里直接 KeyError，而不是
    等运行时才炸。这是 EventType 单一真相源在 Python 侧的一半（另一半——前端 JS
    是否消费了这些类型——见 tests/test_events_gen.py，Task 3）。"""
    emitted = _runtime_emitted_types()
    assert emitted <= _DECLARED
    assert emitted  # 非空：确认扫描确实找到了引用，不是意外全跳过
