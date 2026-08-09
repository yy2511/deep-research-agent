"""run_id 隔离：并发 run 的事件互不串流；无 run_id 的旧路径行为不变。"""
import contextvars

from dra import events
from dra.events import EventType


def test_filtered_subscriber_isolation():
    got_a, got_b = [], []
    ha = events.subscribe(got_a.append, run_id="run-a")
    hb = events.subscribe(got_b.append, run_id="run-b")
    try:
        def _run(rid, payload):
            events.set_run_id(rid)
            events.emit(EventType.SCOPE, query=payload)
        contextvars.copy_context().run(_run, "run-a", "qa")
        contextvars.copy_context().run(_run, "run-b", "qb")
        assert [e["query"] for e in got_a] == ["qa"]
        assert [e["query"] for e in got_b] == ["qb"]
        assert got_a[0]["run_id"] == "run-a"
    finally:
        events.unsubscribe(ha)
        events.unsubscribe(hb)


def test_unfiltered_subscriber_sees_all():
    got = []
    h = events.subscribe(got.append)
    try:
        def _run(rid):
            events.set_run_id(rid)
            events.emit(
                EventType.TASK_BATCH_DISPATCHED,
                count=1,
                round_index=0,
                objectives=[],
                phase="research",
            )
        contextvars.copy_context().run(_run, "run-a")
        events.emit(
            EventType.TASK_BATCH_DISPATCHED,
            count=2,
            round_index=0,
            objectives=[],
            phase="research",
        )   # 无 run_id 上下文
        assert len(got) == 2
        assert "run_id" not in got[1]
    finally:
        events.unsubscribe(h)
