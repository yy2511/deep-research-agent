"""收尾临界额度、真实覆盖/缺口及完整主子诊断的离线回归。"""
import json
import threading
import time

import pytest

from dra import events, llm, simple_research as sr, timing
from dra.research_input import ResearchInput
from test_budget_wrapup import Script, _doc, _text_turn, _turn


QUOTE = "正文中的完整证据。" * 75 + "关键限制在尾部：暂未公开报价。"
PLAN = {"items": [
    {"id": "facts", "text": "核对参数", "status": "done"},
    {"id": "price", "text": "核对价格", "status": "todo"},
    {"id": "abroad", "text": "国外品牌", "status": "dropped"},
], "note": "参数已查到，价格仍缺；国外品牌不在范围内。"}
FINISH = {"draft": "已核对公开参数，报价尚缺 [c1]。",
          "coverage": [{"requirement_id": "goal", "status": "partial", "note": "尚缺报价"}],
          "gaps": ["没有找到公开报价"], "research_plan": PLAN}


def seeded(tmp_path, **kwargs):
    config = sr.SimpleConfig(max_llm_calls=8, max_tool_calls=4, **kwargs)
    store = sr.open_run(tmp_path / "run", query="核对参数和价格", run_id="r")
    budget = sr.Budget(config, deadline=None)
    context = sr.ToolContext(store, config, budget=budget, search_fn=lambda _: [_doc(raw=QUOTE)])
    context.search_sources({"query": "q"})
    assert context.read_source({"source_ref": "s1"})[1]
    context.tool_role = "subagent"
    assert context.save_evidence({"cards": [{"claim": "公开参数", "quote": QUOTE}]})[1]
    context.tool_role = "lead"
    return config, store, budget, context


def test_reserve_single_card_read_is_complete_and_leaves_saving_quota(tmp_path):
    config, store, budget, context = seeded(tmp_path)
    context.tool_calls = 2  # 刚进入最后两次保留区；c1 来自子任务。
    script = Script([
        _turn(("inspect_research", {"view": "cards", "card_id": "c1"})),
        _turn(("save_evidence", {"cards": [{"claim": "未公开报价", "quote": QUOTE}]})),
        _turn(("finish_research", FINISH)),
    ])
    outcome = sr.run_lead_loop(store, config, sr.requirements_from_query(store.query),
        budget=budget, context=context, model_call=script)
    reread = next((r for r in context.receipts if r["tool"] == "inspect_research"), None)
    assert reread and reread["ok"]
    assert json.loads(reread["payload"])["quotes"][0]["quote"] == QUOTE
    assert context.tool_calls == 3, "本地单卡回读不能吃掉存证额度"
    assert outcome["stop_reason"] == "finished"
    assert context.plan_items == PLAN["items"]


def test_repeated_or_unknown_free_reads_cannot_extend_the_loop(tmp_path):
    config, store, budget, context = seeded(tmp_path)
    config.max_invalid_turns = 2
    context.tool_calls = 2
    script = Script([
        _turn(("inspect_research", {"view": "cards", "card_id": "c1"})),
        _turn(("inspect_research", {"view": "cards", "card_id": "c1"})),
        _turn(("inspect_research", {"view": "cards", "card_id": "missing"})),
    ], finish=("finish_research", FINISH))
    outcome = sr.run_lead_loop(store, config, sr.requirements_from_query(store.query),
        budget=budget, context=context, model_call=script)
    assert len(context.receipts) == 3
    assert [r["ok"] for r in context.receipts] == [True, False, False]
    assert context.tool_calls == 2
    assert outcome["stop_reason"] == "no_progress+wrapup"
    assert len(script.seen) == 4


def test_early_metered_inspection_does_not_use_up_the_final_reread(tmp_path):
    config, store, budget, context = seeded(tmp_path)
    inspect = ("inspect_research", {"view": "cards", "card_id": "c1"})
    script = Script([
        _turn(inspect),  # 普通阶段正常计工具额度，不消耗最后的免费核对机会。
        _turn(("search_sources", {"query": "补其他问题"})),
        _turn(inspect),  # 此时进入保留区，即便之前看过也应能核对完整原文。
        _turn(inspect),  # 保留区内再重复才拒绝。
        _turn(("finish_research", FINISH)),
    ])
    outcome = sr.run_lead_loop(store, config, sr.requirements_from_query(store.query),
        budget=budget, context=context, model_call=script)
    reads = [r for r in context.receipts if r["tool"] == "inspect_research"]
    assert [r["ok"] for r in reads] == [True, True, False]
    assert context.tool_calls == 2
    assert outcome["stop_reason"] == "finished"


@pytest.mark.parametrize("wrapup", [False, True])
@pytest.mark.parametrize("bad_metadata", [False, True])
def test_finish_paths_preserve_body_and_report_metadata_honestly(tmp_path, wrapup, bad_metadata):
    payload = FINISH if not bad_metadata else {
        **FINISH, "coverage": [{"requirement_id": "wrong", "status": "answered"}],
        "gaps": "错误格式", "research_plan": {"items": "错误格式"},
    }
    script = Script([
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
        _turn(("save_evidence", {"cards": [{"claim": "公开参数", "quote": QUOTE}]})),
        _turn(("finish_research", payload)),
    ])
    result = sr.run_simple_research("核对参数和价格", sr.requirements_from_query("核对参数和价格"),
        config=sr.SimpleConfig(max_llm_calls=4 if wrapup else 8, max_tool_calls=8),
        run_dir=tmp_path / "run", model_call=script, search_fn=lambda _: [_doc(raw=QUOTE)],
        initial_route=[{"id": "facts", "text": "核对参数"}, {"id": "price", "text": "核对价格"}])
    assert result.draft == FINISH["draft"]
    assert not result.draft_rejected
    if bad_metadata:
        assert result.research_coverage == {"goal": "unanswered"}
        assert "未报告" in " ".join(result.coverage_reasons["goal"])
        assert result.plan_sync_status == "missing"
        # 写错的覆盖条目只被丢掉（要求显示为未报告）；缺口写成一句话照收；清单不合格保留上一版。
        assert "错误格式" in result.gaps
        assert any("最终任务清单未同步" in gap for gap in result.gaps)
    else:
        assert result.research_coverage == {"goal": "partial"}
        assert result.coverage_reasons["goal"] == ["Lead 自报：尚缺报价"]
        assert result.gaps == FINISH["gaps"]
        assert "尚缺报价" in result.markdown
        assert result.plan_sync_status == "reported"
    ledger = json.loads((tmp_path / "run/run.json").read_text())
    plans = [a["payload"] for a in ledger["actions"] if a["action"] == "research_plan"]
    if bad_metadata:
        assert len(plans) == 1, "坏最终快照不得新增清单记录"
        assert plans[0]["items"] == [
            {"id": "facts", "text": "核对参数", "status": "todo"},
            {"id": "price", "text": "核对价格", "status": "todo"},
        ], "保留初始清单，不把任务标完成"
    else:
        assert plans[-1]["items"] == PLAN["items"]
    assert len(result.calls) == 4
    if wrapup:
        assert result.stop_reason == "call_budget+wrapup"
        assert script.tool_schemas[-1] == ["finish_research"]
        assert QUOTE in script.seen[-1][-1]["content"], "收尾不能只读到 240 字的卡片头部"


@pytest.mark.parametrize("signal", ["cancel", "deadline", "incomplete"])
def test_late_or_incomplete_wrapup_never_commits_a_finish(tmp_path, signal):
    config, store, budget, context = seeded(tmp_path)
    cancel = threading.Event()
    def model(*args, **kwargs):
        turn = _turn(("finish_research", FINISH))
        if signal == "cancel":
            cancel.set()
        elif signal == "deadline":
            budget.deadline = time.monotonic() - 1
        else:
            turn.incomplete = True
        return turn
    with llm.execution_scope(cancel_event=cancel):
        draft, coverage, gaps, note = sr._wrapup_turn(store, config, [], context,
            model_call=model, budget=budget)
    assert not draft and not coverage
    assert note
    assert context.plan_items == []
    assert not any(a["action"] == "wrapup_submitted" for a in store.state["actions"])


def test_text_only_wrapup_is_explicitly_missing_metadata(tmp_path):
    config, store, budget, context = seeded(tmp_path)
    context.update_research_plan({"items": [{"id": "todo", "text": "未结束", "status": "doing"}]})
    draft, coverage, gaps, _ = sr._wrapup_turn(store, config, [], context,
        model_call=lambda *a, **k: _text_turn("公开参数 [c1]。"), budget=budget)
    assert draft and coverage == []
    assert any("未报告" in gap for gap in gaps)
    assert context.plan_items[0]["status"] == "doing"


@pytest.mark.parametrize("with_scope", [False, True])
def test_child_plan_metadata_preserves_body_but_scope_changes_are_rejected(tmp_path, with_scope):
    config, store, budget, context = seeded(tmp_path)
    payload = FINISH
    if with_scope:
        context.user_input = ResearchInput("r", lambda _: None)
        context.user_input.accept("u1", "只核对参数")
        sr._receive_user_input(context, "initial")
        payload = {**FINISH, "research_plan": {**PLAN, "scope_update": {
            "goal": "只核对参数", "reason": "用户缩小范围",
            "requirements": [{"id": "facts", "text": "核对参数"}],
        }}}
    draft, _, gaps, note = sr._wrapup_turn(store, config, [], context,
        role="subagent", model_call=lambda *a, **k: _turn(("finish_research", payload)), budget=budget)
    if with_scope:
        # 收尾只有一次机会：子 Agent 越权的范围变更被忽略，正文照收并留下说明。
        assert draft == FINISH["draft"] and not note
        assert any("研究范围变更格式有误" in gap and "主 Agent" in gap for gap in gaps)
    else:
        assert draft == FINISH["draft"] and not note
        assert any("最终任务清单未同步" in gap for gap in gaps)
    assert not context.plan_items and not context.final_plan_reported
    assert not context.scope_history


@pytest.mark.parametrize("step", ["subagent_research", "subagent_wrapup"])
def test_full_child_trace_preserves_identity_and_entire_io(step):
    got = []
    sink = events.subscribe(got.append)
    token = llm.set_trace_full_io(True)
    ctx = timing.set_ctx(step=step, sid="run", usage_role="subagent", delegation_id="sub2",
                         parent_call_id="dispatch", call_id="model-7", turn=3)
    try:
        llm._trace_llm_call([{"role": "user", "content": "input" * 2000}],
                            "output" * 15000, time.monotonic(), "offline", None)
    finally:
        timing.reset_ctx(ctx)
        llm.reset_trace_full_io(token)
        events.unsubscribe(sink)
    assert len(got) == 1
    assert got[0]["input"] == "【user】\n" + "input" * 2000
    assert got[0]["output"] == "output" * 15000
    assert got[0]["io_complete"] is True
    assert {k: got[0][k] for k in ("delegation_id", "parent_call_id", "call_id", "turn")} == {
        "delegation_id": "sub2", "parent_call_id": "dispatch", "call_id": "model-7", "turn": 3}


@pytest.mark.parametrize("full", [True, False])
def test_tool_trace_keeps_raw_arguments_and_complete_receipt_separate_from_preview(tmp_path, full):
    _, _, _, context = seeded(tmp_path)
    got = []
    sink = events.subscribe(got.append)
    token = llm.set_trace_full_io(full)
    call = _turn(("save_evidence", {"cards": [{"quote": QUOTE}]})).tool_calls[0]
    call.arguments_raw = json.dumps(call.arguments, ensure_ascii=False, indent=2)
    receipt = json.dumps({"quote": QUOTE, "reason": "尾部保留"}, ensure_ascii=False)
    try:
        sr._emit_tool(context, "save_evidence", False, receipt, call, turn=4)
    finally:
        llm.reset_trace_full_io(token)
        events.unsubscribe(sink)
    event = got[0]
    assert len(event["args_summary"]) <= 400 and len(event["result_summary"]) <= 600
    if full:
        assert event["arguments_raw"] == call.arguments_raw
        assert event["result_full"] == receipt
        assert event["io_complete"] is True
    else:
        assert "arguments_raw" not in event and "result_full" not in event


def test_final_plan_written_as_bare_list_is_accepted(tmp_path):
    """模型常把最终清单直接写成条目列表；它只可能表示 items，应照常同步。"""
    script = Script([
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
        _turn(("save_evidence", {"cards": [{"claim": "公开参数", "quote": QUOTE}]})),
        _turn(("finish_research", {**FINISH, "research_plan": PLAN["items"]})),
    ])
    result = sr.run_simple_research("核对参数和价格", sr.requirements_from_query("核对参数和价格"),
        config=sr.SimpleConfig(max_llm_calls=8, max_tool_calls=8),
        run_dir=tmp_path / "run", model_call=script, search_fn=lambda _: [_doc(raw=QUOTE)],
        initial_route=[{"id": "facts", "text": "核对参数"}, {"id": "price", "text": "核对价格"}])
    assert result.plan_sync_status == "reported"
    assert not any("最终任务清单未同步" in gap for gap in result.gaps)


@pytest.mark.parametrize("query, expected", [
    ("请对人工智能在政府决策中的应用进行全面综述", "用中文撰写"),
    ("请比较 Naive SP、FSP、NFSP、PSRO、Pipeline PSRO 和 α-PSRO 的核心思想与优缺点", "用中文撰写"),
    ("Compare PSRO and NFSP for two-player zero-sum games", "用英文撰写"),
    ("12345", "使用用户提问的语言"),
])
def test_answer_language_is_named_explicitly(query, expected):
    hint = sr._answer_language_prompt(query)
    assert (expected in hint) if expected else hint == ""


def test_language_hint_reaches_lead_system_prompt(tmp_path):
    script = Script([_turn(("finish_research", {"draft": "没有查到可引用的来源。"}))])
    sr.run_simple_research("核对参数和价格", sr.requirements_from_query("核对参数和价格"),
        config=sr.SimpleConfig(max_llm_calls=4, max_tool_calls=4),
        run_dir=tmp_path / "run", model_call=script, search_fn=lambda _: [])
    assert "用中文撰写" in script.seen[0][0]["content"]
