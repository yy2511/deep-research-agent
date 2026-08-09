"""计划阶段 trace：落盘 + 422 带回 llm_call，供调试 UI 与 runs 对齐。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock

import pytest
from fastapi.testclient import TestClient

import dra.events as events
import dra.timing as timing
import dra.web as web
from dra.events import EventType
from dra.models import NodeKind, PlanNode, ResearchPlan, ResearchTask
from dra.nodes import PlanValidationError


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


def _valid_research_plan(query: str = "测试问题") -> ResearchPlan:
    return ResearchPlan(
        clarified_query=query,
        plan_nodes=[
            PlanNode(
                id="m1",
                objective="核验主题",
                kind=NodeKind.RESEARCH,
                dependency_ids=[],
                acceptance_criteria="有可引用证据",
            )
        ],
        initial_tasks=[
            ResearchTask(
                node_id="m1",
                objective="检索主题证据",
                search_query="topic evidence 2026",
            )
        ],
    )


def test_capture_planner_step_persists_llm_call_and_returns_trace(tmp_path, monkeypatch):
    cfg = MagicMock()
    cfg.planner_model = "mock-planner"
    cfg.planner_provider = "openai"
    cfg.planner_effort = None

    def fake_fn():
        # 模拟 chat 插桩：在 timing.step 内 emit llm_call
        events.emit(
            EventType.LLM_CALL,
            step="build_research_plan",
            model="mock-planner",
            ms=12.5,
            input="【system】plan",
            output='{"plan_nodes":[],"initial_tasks":[]}',
            in_tok=10,
            out_tok=20,
        )
        return {
            "clarified_query": "测试问题",
            "plan_nodes": [{"id": "m1"}],
            "initial_tasks": [{"node_id": "m1"}],
        }

    result, trace = web._capture_planner_step(
        kind="plan", query="测试问题", cfg=cfg, step_name="build_research_plan", fn=fake_fn,
    )
    assert result["clarified_query"] == "测试问题"
    assert trace["plan_id"]
    types = [e["type"] for e in trace["events"]]
    assert "llm_call" in types
    assert "step_done" in types
    llm = next(e for e in trace["events"] if e["type"] == "llm_call")
    assert llm["step"] == "build_research_plan"
    assert llm["output"].startswith("{")
    # 落盘
    meta = web.PLAN_STORE.get_meta(trace["plan_id"])
    assert meta["status"] == "ok"
    text = web.PLAN_STORE.read_events_text(trace["plan_id"])
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
        return {"clarified_query": "q", "plan_nodes": [], "initial_tasks": []}

    _, trace = web._capture_planner_step(
        kind="plan",
        query="q",
        cfg=cfg,
        step_name="build_research_plan",
        fn=fake_fn,
        full_llm_io=True,
    )

    call = next(e for e in trace["events"] if e["type"] == "llm_call")
    assert call["input"] == f"【system】\n{long_input}"
    assert call["output"] == long_output
    assert call["io_complete"] is True
    assert web.PLAN_STORE.get_meta(trace["plan_id"])["config_summary"]["full_llm_io"] is True


def test_capture_planner_step_attaches_trace_on_failure():
    cfg = MagicMock()
    cfg.planner_model = "mock-planner"
    cfg.planner_provider = "openai"
    cfg.planner_effort = None

    def boom():
        events.emit(
            EventType.LLM_CALL,
            step="build_research_plan",
            model="mock-planner",
            ms=1.0,
            input="in",
            output="{}",
        )
        raise PlanValidationError("planner 输出缺少有效 initial_tasks")

    with pytest.raises(PlanValidationError) as ei:
        web._capture_planner_step(
            kind="plan", query="坏计划", cfg=cfg, step_name="build_research_plan", fn=boom,
        )
    trace = ei.value.plan_trace
    assert trace["plan_id"]
    assert any(e["type"] == "llm_call" for e in trace["events"])
    meta = web.PLAN_STORE.get_meta(trace["plan_id"])
    assert meta["status"] == "failed"
    assert "initial_tasks" in meta["error"]


def test_api_plan_success_includes_trace(monkeypatch):
    monkeypatch.setattr(
        web, "_plan_payload",
        lambda q, *, full_llm_io=False: (
            {
                "clarified_query": q,
                "plan_nodes": [{
                    "id": "m1", "objective": "o", "kind": "research",
                    "dependency_ids": [], "acceptance_criteria": "c",
                }],
                "initial_tasks": [{
                    "id": "t1", "node_id": "m1", "objective": "o",
                    "search_query": "q",
                }],
            },
            {"plan_id": "p-test", "events": [
                {"type": "llm_call", "step": "build_research_plan", "ms": 1, "input": "i", "output": "o"},
            ]},
        ),
    )
    with TestClient(web.app) as client:
        r = client.post("/api/plan", json={"query": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["clarified_query"] == "hello"
    assert body["trace"]["plan_id"] == "p-test"
    assert body["trace"]["events"][0]["type"] == "llm_call"


def test_api_plan_422_includes_trace(monkeypatch):
    err = PlanValidationError("planner 输出缺少有效 initial_tasks")
    err.plan_trace = {
        "plan_id": "p-fail",
        "events": [
            {"type": "llm_call", "step": "build_research_plan", "ms": 2,
             "input": "sys", "output": "{}"},
        ],
    }

    def boom(q, *, full_llm_io=False):
        raise err

    monkeypatch.setattr(web, "_plan_payload", boom)
    with TestClient(web.app) as client:
        r = client.post("/api/plan", json={"query": "fail me"})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "缺少有效 initial_tasks" in detail["message"]
    assert detail["trace"]["plan_id"] == "p-fail"
    assert detail["trace"]["events"][0]["output"] == "{}"


def test_api_plan_forwards_debug_full_io_flag(monkeypatch):
    call = MagicMock(return_value=(
        {
            "clarified_query": "hello",
            "plan_nodes": [{
                "id": "m1", "objective": "o", "kind": "research",
                "dependency_ids": [], "acceptance_criteria": "c",
            }],
            "initial_tasks": [{
                "id": "t1", "node_id": "m1", "objective": "o",
                "search_query": "q",
            }],
        },
        {"plan_id": "p", "events": []},
    ))
    monkeypatch.setattr(web, "_plan_payload", call)

    with TestClient(web.app) as client:
        response = client.post(
            "/api/plan",
            json={"query": "hello", "trace_full_llm_io": True},
        )

    assert response.status_code == 200
    call.assert_called_once_with("hello", full_llm_io=True)


def test_payload_to_research_plan_strips_trace():
    valid = {
        "clarified_query": "q",
        "plan_nodes": [{
            "id": "m1", "objective": "o", "kind": "research",
            "dependency_ids": [], "acceptance_criteria": "c",
        }],
        "initial_tasks": [{
            "id": "t1", "node_id": "m1", "objective": "o",
            "search_query": "s",
        }],
        "budget": {
            "max_research_rounds": 3,
            "max_tasks_per_round": 5,
            "max_total_tasks": 18,
        },
        "trace": {"plan_id": "x", "events": [{"type": "llm_call"}]},
    }
    research_plan = web._payload_to_research_plan(valid)
    assert research_plan is not None
    assert research_plan.clarified_query == "q"
    assert len(research_plan.initial_tasks) == 1


def test_planner_max_tokens_raised():
    from dra import nodes
    assert nodes._RESEARCH_PLAN_MAX_TOKENS == 10000
    assert nodes._DECISION_MAX_TOKENS == 10000
