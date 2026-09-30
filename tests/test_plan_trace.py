"""计划阶段 trace：落盘 + 422 带回 llm_call，供调试 UI 与 runs 对齐。"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import dra.events as events
import dra.timing as timing
import dra.web as web
from dra.events import EventType


@pytest.fixture(autouse=True)
def _isolate_plan_store(tmp_path, monkeypatch):
    """每个测试独立 plans/ 目录，避免污染仓库 plans/。"""
    store = web.PlanAttemptStore(tmp_path / "plans")
    monkeypatch.setattr(web, "PLAN_STORE", store)
    # 清事件总线与 run_id，防测试间泄漏
    events._sinks.clear()
    events.set_run_id(None)
    timing.clear_ctx()
    yield
    events._sinks.clear()
    events.set_run_id(None)
    timing.clear_ctx()


def test_capture_planner_step_persists_llm_call_and_returns_trace(tmp_path, monkeypatch):
    cfg = MagicMock()
    cfg.planner_model = "mock-planner"
    cfg.planner_provider = "openai"
    cfg.planner_effort = None

    def fake_fn():
        # 模拟 chat 插桩：在 timing.step 内 emit llm_call
        events.emit(
            EventType.LLM_CALL,
            step="research_brief",
            model="mock-planner",
            ms=12.5,
            input="【system】plan",
            output='{"items":[],"requirements":[]}',
            in_tok=10,
            out_tok=20,
        )
        return {
            "goal": "测试问题",
            "items": [{"id": "m1"}],
            "requirements": [{"node_id": "m1"}],
        }

    result, trace = web._capture_planner_step(
        kind="plan", query="测试问题", cfg=cfg, step_name="research_brief", fn=fake_fn,
    )
    assert result["goal"] == "测试问题"
    assert trace["plan_id"]
    types = [e["type"] for e in trace["events"]]
    assert "llm_call" in types
    assert "step_done" in types
    llm = next(e for e in trace["events"] if e["type"] == "llm_call")
    assert llm["step"] == "research_brief"
    assert llm["output"].startswith("{")
    # 落盘
    plan_dir = web.PLAN_STORE.root / trace["plan_id"]
    meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "ok"
    text = (plan_dir / "events.jsonl").read_text(encoding="utf-8")
    assert text and "llm_call" in text


def test_capture_planner_step_full_io_is_scoped_and_recorded():
    import time
    import dra.llm as llm_module

    cfg = MagicMock()
    cfg.planner_model = "mock-planner"
    cfg.planner_provider = "openai"
    cfg.planner_effort = None
    long_input = "P" * 6000
    long_output = "O" * 70000

    def fake_fn():
        llm_module._trace_llm_call(
            [{"role": "system", "content": long_input}],
            long_output,
            time.monotonic(),
            "mock-planner",
            None,
        )
        return {"goal": "q", "items": [], "requirements": []}

    _, trace = web._capture_planner_step(
        kind="plan",
        query="q",
        cfg=cfg,
        step_name="research_brief",
        fn=fake_fn,
        full_llm_io=True,
    )

    call = next(e for e in trace["events"] if e["type"] == "llm_call")
    assert call["input"] == f"【system】\n{long_input}"
    assert call["output"] == long_output
    assert call["io_complete"] is True
    plan_dir = web.PLAN_STORE.root / trace["plan_id"]
    meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["config_summary"]["full_llm_io"] is True


def test_capture_planner_step_attaches_trace_on_failure():
    cfg = MagicMock()
    cfg.planner_model = "mock-planner"
    cfg.planner_provider = "openai"
    cfg.planner_effort = None

    def boom():
        events.emit(
            EventType.LLM_CALL,
            step="research_brief",
            model="mock-planner",
            ms=1.0,
            input="in",
            output="{}",
        )
        raise ValueError("planner 输出缺少有效 requirements")

    with pytest.raises(ValueError) as ei:
        web._capture_planner_step(
            kind="plan", query="坏计划", cfg=cfg, step_name="research_brief", fn=boom,
        )
    trace = ei.value.plan_trace
    assert trace["plan_id"]
    assert any(e["type"] == "llm_call" for e in trace["events"])
    plan_dir = web.PLAN_STORE.root / trace["plan_id"]
    meta = json.loads((plan_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert "requirements" in meta["error"]
