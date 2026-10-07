"""Real inbox/wait lifecycle with deterministic workers; no model or network."""
import json
import threading
import time

import pytest

from dra import llm, simple_research as sr
from dra.research_delegation import DelegationManager
from test_budget_wrapup import Script, _doc, _turn

FINISH = {"draft": "资料不足。", "coverage": [], "gaps": ["待补充材料"]}
WAIT = ("wait_research", {"delegation_ids": ["sub1"], "timeout_s": 30})
BAD = ("unknown_tool", {})


@pytest.fixture
def research(tmp_path, monkeypatch):
    release, started = threading.Event(), threading.Event()
    def runner(*_):
        started.set()
        assert release.wait(5)
        return {"status": "finished", "findings": "原件已登记，需主线解析。", "card_ids": []}
    manager = DelegationManager(runner, lambda _: None)
    manager.submit([("读表格", "")], "dispatch")
    assert started.wait(2)
    real_wait = manager.wait
    monkeypatch.setattr(manager, "wait", lambda ids=None, timeout=30, **kw: real_wait(ids, 0, **kw))
    config = sr.SimpleConfig(max_llm_calls=20, max_tool_calls=20, tool_reserve_calls=0)
    store = sr.open_run(tmp_path / "run", query="核对表格", run_id="progress")
    budget = sr.Budget(config, None)
    ctx = sr.ToolContext(store, config, budget=budget, search_fn=lambda _: [_doc()])
    ctx.delegation_manager = manager
    def settle():
        release.set()
        real_wait(["sub1"], 2, ignore_inbox=True)
        assert not manager.active()
    yield ctx, budget, manager, settle
    release.set()
    manager.close()


def run(ctx, budget, model, **kwargs):
    return sr.run_lead_loop(ctx.store, ctx.config, [], context=ctx, budget=budget,
                            model_call=model, **kwargs)


def test_four_normal_waits_then_handoff_continue_in_research(research):
    ctx, budget, manager, settle = research
    seen = []
    def model(messages, **kwargs):
        seen.append((messages, kwargs))
        if len(seen) <= 4:
            if len(seen) == 4:
                settle()
            return _turn(WAIT)
        if len(seen) == 5:
            assert "原件已登记" in json.dumps(messages, ensure_ascii=False)
            assert "search_sources" in {t["function"]["name"] for t in kwargs["tools"]}
            return _turn(("search_sources", {"query": "补查指标"}))
        return _turn(("finish_research", FINISH))
    outcome = run(ctx, budget, model)
    assert outcome["stop_reason"] == "finished" and ctx.queries == ["补查指标"]
    assert len(seen) == 6


def test_wait_preserves_two_strikes_instead_of_clearing_them(research):
    ctx, budget, _, _ = research
    script = Script([_turn(BAD), _turn(BAD), *[_turn(WAIT) for _ in range(4)], _turn(BAD)],
                    finish=("finish_research", FINISH))
    outcome = run(ctx, budget, script)
    assert outcome["stop_reason"] == "no_progress+wrapup"
    assert len(script.seen) == 8  # seven research requests, one finish-only request
    assert script.tool_schemas[-1] == ["finish_research"]


@pytest.mark.parametrize("calls", [
    [WAIT, BAD], [("wait_research", {"timeout_s": 0})],
    [("wait_research", {"delegation_ids": ["missing"], "timeout_s": 30})],
    [("inspect_research", {"view": "delegations"})],
])
def test_invalid_mixed_or_status_only_turns_still_stop(research, calls):
    ctx, budget, _, _ = research
    script = Script([_turn(*calls) for _ in range(3)], finish=("finish_research", FINISH))
    assert run(ctx, budget, script)["stop_reason"] == "no_progress+wrapup"
    assert len(script.seen) == 4


@pytest.mark.parametrize("empty", [False, True])
def test_wait_on_completed_or_no_tasks_is_not_neutral(research, empty):
    ctx, budget, manager, settle = research
    settle()
    manager.drain("parent")
    if empty:
        manager.tasks.clear()
    script = Script([_turn(("wait_research", {"timeout_s": 30})) for _ in range(3)],
                    finish=("finish_research", FINISH))
    assert run(ctx, budget, script)["stop_reason"] == "no_progress+wrapup"


def test_inspecting_full_completed_result_is_valid_reading(research):
    ctx, budget, _, settle = research
    settle()
    script = Script([*[_turn(("inspect_research", {"view": "delegations", "delegation_id": "sub1"}))
                       for _ in range(4)], _turn(("finish_research", FINISH))])
    assert run(ctx, budget, script)["stop_reason"] == "finished"
    assert len(script.seen) == 5


@pytest.mark.parametrize("role", ["lead", "subagent"])
def test_new_message_received_before_no_progress_check(research, role):
    ctx, budget, manager, settle = research
    if role == "subagent":
        ctx = ctx.fork_for_subagent(ctx.config, budget=budget, delegation_id="sub1")
        ctx.delegation_manager = manager
    seen = []
    def model(messages, **kwargs):
        seen.append(messages)
        if len(seen) <= 3:
            if len(seen) == 3:
                sender, target = ("sub1", "parent") if role == "lead" else ("parent", "sub1")
                manager.send(sender, target, "请解析已经取得的ODS原件")
            return _turn(BAD)
        assert "请解析已经取得的ODS原件" in json.dumps(messages, ensure_ascii=False)
        if role == "lead":
            settle()
            manager.drain("parent")
        return _turn(("finish_research", FINISH))
    outcome = run(ctx, budget, model, role=role,
        system_prompt=sr._SUBAGENT_SYSTEM if role == "subagent" else None,
        schemas=sr.SUBAGENT_TOOL_SCHEMAS if role == "subagent" else None)
    assert outcome["stop_reason"] == "finished" and len(seen) == 4


def test_wait_does_not_override_cancellation(research):
    ctx, budget, _, _ = research
    cancelled = threading.Event()
    def model(*_, **kwargs):
        cancelled.set()
        return _turn(WAIT)
    with llm.execution_scope(cancel_event=cancelled):
        assert run(ctx, budget, model)["stop_reason"] == "cancelled"


def test_new_terminal_result_received_before_no_progress_check(research):
    ctx, budget, _, settle = research
    seen = []
    def model(messages, **kwargs):
        seen.append(messages)
        if len(seen) <= 3:
            if len(seen) == 3:
                settle()
            return _turn(BAD)
        assert '原件已登记' in json.dumps(messages, ensure_ascii=False)
        return _turn(('finish_research', FINISH))
    assert run(ctx, budget, model)['stop_reason'] == 'finished'
    assert len(seen) == 4


def test_normal_wait_cannot_exceed_call_budget(research):
    ctx, budget, _, _ = research
    ctx.config.max_llm_calls = 3
    script = Script([_turn(WAIT), _turn(WAIT)], finish=('finish_research', FINISH))
    assert run(ctx, budget, script)['stop_reason'] == 'call_budget+wrapup'
    assert len(script.seen) == 3 and script.tool_schemas[-1] == ['finish_research']


def test_deadline_reached_during_wait_stops_without_new_model_call(research, monkeypatch):
    ctx, budget, manager, _ = research
    wait = manager.wait
    def expired_wait(*args, **kwargs):
        result = wait(*args, **kwargs)
        budget.deadline = time.monotonic() - 1
        return result
    monkeypatch.setattr(manager, 'wait', expired_wait)
    script = Script([_turn(WAIT)], finish=('finish_research', FINISH))
    assert run(ctx, budget, script)['stop_reason'].startswith('deadline')
    assert len(script.seen) == 1
