"""输出额度截断只结束本轮；完整历史、调用账本和原预算继续有效。"""
from copy import deepcopy
import json

import pytest

from dra import llm, simple_research as sr
from test_budget_wrapup import Script, _doc, _turn


FINISH = ("finish_research", {"draft": "尚未核实。", "coverage": [], "gaps": ["缺少证据"]})


def truncated():
    turn = _turn(("search_sources", {"query": "must not execute"}))
    turn.finish_reason = "length"
    turn.incomplete = True
    turn.content = "TRUNCATED_CONTENT"
    turn.assistant_message["content"] = turn.content
    turn.assistant_message["reasoning_content"] = "TRUNCATED_REASONING"
    turn.input_tokens = 100
    turn.output_tokens = 32768
    return turn


@pytest.mark.parametrize("role", ["lead", "subagent"])
def test_length_continues_from_complete_history_without_executing_partial_tools(tmp_path, role):
    config = sr.SimpleConfig(max_llm_calls=8, max_output_tokens=32768, delegation_enabled=False)
    store = sr.open_run(tmp_path, query="q", run_id="recover")
    budget = sr.Budget(config, deadline=None)
    searched = []
    context = sr.ToolContext(store, config, budget=budget,
                            search_fn=lambda q: searched.append(q) or [_doc()])
    script = Script([
        _turn(("search_sources", {"query": "earlier"})),
        truncated(),
        _turn(("read_source", {"source_ref": "s1"})),
        _turn(FINISH),
    ])
    result = sr.run_lead_loop(store, config, [], context=context, budget=budget,
                              model_call=script, role=role)

    assert result["stop_reason"] == "finished"
    assert searched == ["earlier"]
    resumed = json.dumps(script.seen[2], ensure_ascii=False)
    assert "单次额度上限" in resumed and "未执行任何工具" in resumed
    completed = lambda messages: [m for m in messages if m.get("tool_calls") or m["role"] == "tool"]
    assert completed(script.seen[2]) == completed(script.seen[1])
    assert "sqlite3_rsync" in resumed
    assert "TRUNCATED_CONTENT" not in resumed and "TRUNCATED_REASONING" not in resumed
    assert "must not execute" not in resumed
    assert [r["tool"] for r in context.receipts] == ["search_sources", "read_source"]
    assert store.state["calls"][1]["output_tokens"] == 32768
    assert budget.calls_used == 4
    actions = [a for a in store.state["actions"] if a["action"] == "output_truncated"]
    assert len(actions) == 1
    assert actions[0]["payload"]["call_id"] == store.state["calls"][1]["call_id"]
    assert actions[0]["payload"]["finish_reason"] == "length"
    assert not any(a["action"] == "protocol_error" for a in store.state["actions"])
    saved = json.loads((store.dir / actions[0]["payload"]["diagnostic_file"]).read_text())
    assert saved["content"] == "TRUNCATED_CONTENT" and saved["reasoning_content"] == "TRUNCATED_REASONING"


def test_repeated_length_uses_existing_call_budget_and_keeps_wrapup_reserve(tmp_path):
    script = Script([truncated() for _ in range(4)], finish=FINISH)
    config = sr.SimpleConfig(max_llm_calls=5, delegation_enabled=False)
    result = sr.run_simple_research("q", run_dir=tmp_path, config=config, model_call=script,
                                    search_fn=lambda _: pytest.fail("truncated tool executed"))
    assert result.stop_reason == "call_budget+wrapup"
    assert len(script.seen) == 5
    assert script.tool_schemas[-1] == ["finish_research"]


@pytest.mark.parametrize("signal", ["deadline", "cancelled"])
def test_length_does_not_extend_deadline_or_override_cancellation(tmp_path, monkeypatch, signal):
    now = [1000.0]
    cancelled = [False]
    monkeypatch.setattr(sr.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(llm, "cancellation_requested", lambda: cancelled[0])
    calls = []

    def model(messages, **kwargs):
        calls.append(deepcopy(messages))
        if signal == "deadline":
            now[0] = 2200.0
        else:
            cancelled[0] = True
        return truncated()

    config = sr.SimpleConfig(wall_timeout_s=1200, delegation_enabled=False)
    result = sr.run_simple_research("q", run_dir=tmp_path, config=config, model_call=model,
                                    search_fn=lambda _: pytest.fail("truncated tool executed"))
    assert result.stop_reason == signal
    assert len(calls) == 1 and not result.draft


@pytest.mark.parametrize("reason", [None, "content_filter", "tool_calls"])
def test_other_incomplete_turns_still_stop_with_protocol_error(tmp_path, reason):
    turn = truncated()
    turn.finish_reason = reason
    turn.note = "不完整协议"
    script = Script([turn], finish=FINISH)
    result = sr.run_simple_research("q", run_dir=tmp_path,
        config=sr.SimpleConfig(max_llm_calls=4, delegation_enabled=False), model_call=script,
        search_fn=lambda _: pytest.fail("incomplete tool executed"))
    assert result.stop_reason == "protocol_error+wrapup"
    assert len(script.seen) == 2 and script.tool_schemas[-1] == ["finish_research"]
