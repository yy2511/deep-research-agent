"""dra.events —— 结构化事件总线 + 管线埋点 的测试（纯逻辑，不调 LLM）。

- emit/subscribe/unsubscribe；无订阅 = no-op；订阅者抛错被吞（不弄崩主流程）
- tool-loop 的事件契约由 test_toolloop_loop.py 覆盖；这里保留事件总线与计时测试。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from dra import events as E  # noqa: E402


def _collector():
    got = []
    fn = E.subscribe(got.append)
    return got, fn


# ---------------------------------------------------------------------------
# 事件总线基础
# ---------------------------------------------------------------------------

def test_emit_to_subscriber():
    got, fn = _collector()
    try:
        E.emit("hello", x=1)
        assert len(got) == 1
        assert got[0]["type"] == "hello" and got[0]["x"] == 1
        assert "ts" in got[0]
    finally:
        E.unsubscribe(fn)


def test_no_subscriber_is_noop():
    # 没订阅者时 emit 不应抛、不应有副作用
    E.emit("nobody_listening", a=1)  # 不抛即通过


def test_unsubscribe_stops_delivery():
    got, fn = _collector()
    E.emit("a")
    E.unsubscribe(fn)
    E.emit("b")
    assert [e["type"] for e in got] == ["a"]


def test_subscriber_exception_swallowed():
    """一个订阅者抛错不能影响其它订阅者 / 主流程。"""
    good = []
    def boom(_):
        raise RuntimeError("boom")
    fn_boom = E.subscribe(boom)
    fn_good = E.subscribe(good.append)
    try:
        E.emit("evt", v=9)        # 不应抛
        assert good and good[0]["v"] == 9   # 好的订阅者照常收到
    finally:
        E.unsubscribe(fn_boom)
        E.unsubscribe(fn_good)


# ---------------------------------------------------------------------------
# 可观测上下文 + 步骤级耗时事件（trace：节点计时 + llm call 归属）
# ---------------------------------------------------------------------------


def test_set_ctx_merge_and_reset():
    """set_ctx 合并更新；reset_ctx 恢复到 set 前（step 不污染 worker iteration）。"""
    from dra import timing as T
    base = T.set_ctx(sid="s1", worker_iteration=1)
    try:
        assert T.get_ctx() == {"sid": "s1", "worker_iteration": 1}
        inner = T.set_ctx(step="extract")
        assert T.get_ctx()["step"] == "extract" and T.get_ctx()["sid"] == "s1"
        T.reset_ctx(inner)
        assert "step" not in T.get_ctx() and T.get_ctx()["sid"] == "s1"  # step 退出，sid 保留
    finally:
        T.reset_ctx(base)
    assert T.get_ctx() == {}


def test_timing_step_emits_step_done():
    """timing.step 结束 emit step_done，带 sid/worker_iteration 观测上下文。"""
    from dra import timing as T
    tok = T.set_ctx(sid="s9", worker_iteration=3)
    got, fn = _collector()
    try:
        with T.step("extract"):
            pass
    finally:
        E.unsubscribe(fn)
        T.reset_ctx(tok)
    sd = [e for e in got if e["type"] == "step_done"]
    assert sd and sd[-1]["step"] == "extract"
    assert sd[-1]["sid"] == "s9" and sd[-1]["worker_iteration"] == 3
    assert isinstance(sd[-1]["ms"], (int, float)) and sd[-1]["ms"] >= 0
