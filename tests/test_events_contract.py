"""核对运行时 EventType 引用；前端生成物新鲜度见 test_events_gen.py。"""

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
    for path in _SRC.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for name in re.findall(r"EventType\.([A-Z_]+)", src):
            emitted.add(EventType[name].value)  # 名不存在会 KeyError = 源码引用了不存在的 EventType
    # web._run_research 补发（ev_q.put，非核心 emit，但属前端契约）
    emitted |= {EventType.REPORT_MD.value, EventType.ERROR.value, EventType.END.value}
    return emitted


def test_runtime_event_type_references_are_valid_members():
    """运行时源码里所有 EventType.X 引用
    都必须是真实存在的枚举成员——名字打错/改名漏更新在这里直接 KeyError，而不是
    等运行时才炸。这是 EventType 单一真相源在 Python 侧的一半（另一半——前端 JS
    是否消费了这些类型——见 tests/test_events_gen.py，Task 3）。"""
    emitted = _runtime_emitted_types()
    assert emitted <= _DECLARED
    assert emitted  # 非空：确认扫描确实找到了引用，不是意外全跳过
