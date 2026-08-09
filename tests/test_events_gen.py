"""事件契约守卫 v2：EventType（Python 单一真相源）→ events.gen.ts 生成物新鲜度。

改 events.py 后必须重跑 `uv run python scripts/gen_events_ts.py`，否则本测试红——
把「前端类型表过期」从运行期 drift 提前到 CI。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.gen_events_ts import OUT_PATH, render


def test_gen_ts_is_fresh():
    assert OUT_PATH.exists(), "缺 web/src/events.gen.ts：跑 uv run python scripts/gen_events_ts.py"
    assert OUT_PATH.read_text(encoding="utf-8") == render()


def test_cancelled_event_declared():
    from dra.events import EventType
    assert EventType.CANCELLED.value == "cancelled"


def test_ready_set_and_research_round_events_declared():
    from dra.events import EventType
    assert EventType.READY_SET_COMPUTED.value == "ready_set_computed"
    assert EventType.TASK_BATCH_DISPATCHED.value == "task_batch_dispatched"
    assert EventType.RESEARCH_ROUND_COMPLETED.value == "research_round_completed"
