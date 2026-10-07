"""路线控制额度、阶段提醒与最终同步状态；模型/检索使用可控边界。"""
import json
import threading

import pytest

from dra import events, llm, simple_research as sr
from dra.research_delegation import DelegationManager
from dra.research_input import ResearchInput
from test_budget_wrapup import Script, _doc, _turn


ITEMS = [{"id": "facts", "text": "核对公开材料", "status": "doing"}]
PLAN = {"items": ITEMS, "note": "开始核对"}
FINISH = {"draft": "目前资料不足，无法给出确定结论。", "gaps": ["仍缺原始材料"]}


def setup_context(tmp_path, **overrides):
    config = sr.SimpleConfig(**{"max_llm_calls": 10, "max_tool_calls": 4,
                               "tool_reserve_calls": 0, **overrides})
    store = sr.open_run(tmp_path / "run", query="核对公开材料", run_id="progress")
    budget = sr.Budget(config, deadline=None)
    context = sr.ToolContext(store, config, budget=budget, search_fn=lambda _: [_doc()])
    return config, store, budget, context


def messages(context, budget):
    return sr._build_messages([], context, [], [], seconds_left=30, budget=budget)[-1]["content"]


def test_route_updates_leave_all_research_tool_quota_available(tmp_path):
    config, store, budget, context = setup_context(tmp_path, max_tool_calls=3)
    queries = []
    context.search_fn = lambda q: queries.append(q) or [_doc()]
    script = Script([
        _turn(("update_research_plan", PLAN)),
        _turn(("search_sources", {"query": "first"})),
        _turn(("update_research_plan", {"items": [{**ITEMS[0], "status": "done"}]})),
        _turn(("search_sources", {"query": "second"})),
        # 同一批次先用完研究额度，仍能提交本地路线。
        _turn(("search_sources", {"query": "third"}), ("update_research_plan", PLAN)),
    ], finish=("finish_research", {**FINISH, "research_plan": PLAN}))
    outcome = sr.run_lead_loop(store, config, [], budget=budget, context=context, model_call=script)
    assert queries == ["first", "second", "third"]
    assert context.tool_calls == 3
    assert outcome["draft"] == FINISH["draft"]
    assert len(script.seen) == 6
    assert len([a for a in store.state["actions"] if a["action"] == "research_plan"]) == 3


def test_identical_plan_is_accepted_without_history_or_research_progress(tmp_path):
    config, store, budget, context = setup_context(tmp_path)
    context.update_research_plan(PLAN)
    got = []
    sink = events.subscribe(got.append)
    try:
        script = Script([_turn(("update_research_plan", {**PLAN, "note": f"重复说明 {i}"}))
                         for i in range(2)], finish=("finish_research", FINISH))
        outcome = sr.run_lead_loop(store, config, [], budget=budget, context=context, model_call=script)
    finally:
        events.unsubscribe(sink)
    assert outcome["stop_reason"] == "finished"
    assert context.tool_calls == 0
    assert not [e for e in got if e["type"] == "research_plan_updated"]
    assert len([a for a in store.state["actions"] if a["action"] == "research_plan"]) == 1
    receipts = [r for r in context.receipts if r["tool"] == "update_research_plan"]
    assert len(receipts) == 2 and all(r["ok"] for r in receipts)
    assert all(json.loads(r["payload"])["changed"] is False for r in receipts)


@pytest.mark.parametrize("status", ["finished", "failed"])
def test_new_child_result_requests_plan_review_without_marking_tasks_done(tmp_path, status):
    _, _, budget, context = setup_context(tmp_path)
    context.update_research_plan(PLAN)
    manager = DelegationManager(lambda *_: {"status": status, "findings": "子任务的材料", "card_ids": []},
                                lambda _: None)
    context.delegation_manager = manager
    context.delegation_messages = []
    try:
        manager.submit([("核对公开材料", "")], "dispatch")
        manager.close(cancel=False)
        sr._receive_delegation_messages(context)
        prompt = messages(context, budget)
        assert "【路线核对提醒】" in prompt and "sub1" in prompt
        assert context.plan_items == ITEMS
        # 模型核对后可明确提交相同清单：不勾完成，也不重复刷历史。
        assert context.update_research_plan(PLAN)[1]
        assert "【路线核对提醒】" not in messages(context, budget)
        sr._receive_delegation_messages(context)
        assert "【路线核对提醒】" not in messages(context, budget)
    finally:
        manager.close()


def test_received_user_message_requests_review_and_invalid_plan_does_not_clear_it(tmp_path):
    _, _, budget, context = setup_context(tmp_path)
    context.update_research_plan(PLAN)
    context.user_input = ResearchInput("progress", lambda _: None)
    context.user_input.accept("u1", "价格不用查了，重点看软件兼容。")
    sr._receive_user_input(context, "lead-1")
    assert "【路线核对提醒】" in messages(context, budget)
    assert "u1" in messages(context, budget)
    assert not context.update_research_plan({"items": [{**ITEMS[0], "status": "invalid"}]})[1]
    assert "【路线核对提醒】" in messages(context, budget)
    assert context.update_research_plan(PLAN)[1]
    assert "【路线核对提醒】" not in messages(context, budget)


def test_entering_reserve_prompts_once_without_extra_model_calls(tmp_path):
    _, store, budget, context = setup_context(tmp_path, tool_reserve_calls=2)
    context.update_research_plan(PLAN)
    context.tool_calls = 2
    prompt = messages(context, budget)
    assert "【路线核对提醒】" in prompt and "收尾" in prompt
    assert "不占" in prompt and "路线" in prompt
    context.update_research_plan(PLAN)
    assert "【路线核对提醒】" not in messages(context, budget)
    assert store.calls() == []


@pytest.mark.parametrize("wrapup", [False, True])
@pytest.mark.parametrize("include_plan", [False, True])
def test_final_plan_sync_marker_survives_ledger_and_does_not_block_report(tmp_path, wrapup, include_plan):
    args = {**FINISH, **({"research_plan": PLAN} if include_plan else {})}
    script = Script([_turn(("finish_research", args))])
    result = sr.run_simple_research("核对公开材料", run_dir=tmp_path / "result",
        initial_route=ITEMS, model_call=script,
        config=sr.SimpleConfig(max_llm_calls=1 if wrapup else 4, max_tool_calls=4))
    assert result.draft == FINISH["draft"]
    expected = "reported" if include_plan else "missing"
    assert result.plan_sync_status == expected
    ledger = json.loads((tmp_path / "result/run.json").read_text())
    assert ledger["final"]["plan_sync_status"] == expected
    if wrapup:
        assert "【路线核对提醒】" in json.dumps(script.seen[-1], ensure_ascii=False)


@pytest.mark.parametrize("signal", ["cancel", "deadline"])
def test_free_plan_updates_still_respect_cancellation_and_deadline(tmp_path, signal):
    import time
    config, store, budget, context = setup_context(tmp_path)
    cancel = threading.Event()
    def model(*_, **__):
        if signal == "cancel":
            cancel.set()
        else:
            budget.deadline = time.monotonic() - 1
        return _turn(("update_research_plan", PLAN))
    with llm.execution_scope(cancel_event=cancel):
        sr.run_lead_loop(store, config, [], budget=budget, context=context, model_call=model)
    assert not context.plan_items
    assert not [a for a in store.state["actions"] if a["action"] == "research_plan"]
