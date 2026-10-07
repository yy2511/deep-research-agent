import json
import time
import threading

import pytest

from dra import simple_research as sr, runtime_config as rc
from test_simple_research import Script, _turn, _doc


def test_default_unlimited_runs_past_former_caps_and_records_every_round(tmp_path):
    script = Script([_turn(("search_sources", {"query": f"q{i}"})) for i in range(24)],
                    finish=("finish_research", {"draft": "没有足够证据", "coverage": []}))
    result = sr.run_simple_research("q", model_call=script, search_fn=lambda q: [_doc()], run_dir=tmp_path)
    assert result.stop_reason == "finished"
    assert len(result.calls) == 25
    assert sr.SimpleConfig().max_llm_calls is None
    assert sr.SimpleConfig().max_tool_calls is None
    assert sr.SimpleConfig().effective_tool_reserve() == 0
    for messages in script.seen:
        prompt = "\n".join(m.get("content", "") for m in messages if m["role"] != "tool")
        for forbidden in ("【工具额度】", "【模型额度】", "收尾保留区", "最后的工具保留区", "用户插话不增加预算"):
            assert forbidden not in prompt
    journal = [json.loads(s) for s in (tmp_path / "tool_calls.jsonl").read_text().splitlines()]
    completed = [row for row in journal if row["phase"] == "done"]
    assert len(completed) == 25
    assert len({row["record_id"] for row in completed}) == 25
    assert completed[0]["arguments"] == {"query": "q0"}
    assert completed[0]["model_call_id"] == result.calls[0].call_id
    assert completed[0]["agent_id"] == "lead"
    assert completed[0]["elapsed_ms"] >= 0
    assert json.loads(completed[0]["result"])
    stats = json.loads((tmp_path / "call_stats.json").read_text())
    assert stats["totals"]["model_calls"] == 25
    assert stats["totals"]["tool_calls"] == 25
    assert stats["totals"]["budget_counted_tool_calls"] == 24  # 交稿不占研究工具次数
    assert len(stats["rounds"]) == 25


def test_runtime_toggle_preserves_models_and_resolves_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "_CONFIG_PATH", tmp_path / "runtime_config.json")
    settings = rc._code_defaults()
    assert settings.call_limits_enabled is False
    for enabled in (True, False):
        updated = settings.model_copy(update={"call_limits_enabled": enabled})
        rc.save(updated)
        loaded = rc.load()
        assert loaded.subagent == settings.subagent
        config = rc.to_simple_config(loaded)
        assert config.max_llm_calls == (20 if enabled else None)
        assert config.max_tool_calls == (20 if enabled else None)
        assert config.wall_timeout_s == 1200
        assert loaded.call_limits_enabled is enabled


def test_unlimited_still_obeys_deadline_and_reserve_validation():
    budget = sr.Budget(sr.SimpleConfig(), time.monotonic() + 60)
    budget.calls_used = 1000
    assert budget.remaining_calls() is None
    assert budget.can_call(reserve=1)
    budget.deadline = time.monotonic() - 1
    assert not budget.can_call(reserve=0)


@pytest.mark.parametrize("limited", [False, True])
def test_child_inherits_switch_and_all_agent_tools_are_recorded(tmp_path, limited):
    child_round = 0
    parent_round = 0
    seen_child = []
    settled = threading.Event()

    def model(messages, **kwargs):
        nonlocal child_round, parent_round
        if sr.timing.get_ctx().get("usage_role") == "subagent":
            child_round += 1
            seen_child.append(messages)
            # 限额时真实循环会提前进入 finish-only 收尾；不限制时做满 24 次研究再交稿。
            if child_round <= 24 and len(kwargs["tools"]) > 1:
                return _turn(("search_sources", {"query": f"child-{child_round}"}))
            settled.set()
            return _turn(("finish_research", {"draft": "child completed", "coverage": []}))
        parent_round += 1
        if parent_round == 1:
            return _turn(("delegate_research", {"tasks": [{"goal": "child", "context": ""}]}))
        if parent_round == 2:
            assert settled.wait(5)
            return _turn(("wait_research", {"timeout_s": 1}))
        return _turn(("finish_research", {"draft": "已完成", "coverage": []}))

    config = rc.to_simple_config(rc._code_defaults().model_copy(update={"call_limits_enabled": limited}))
    result = sr.run_simple_research("q", config=config, model_call=model,
                                    search_fn=lambda _: [_doc()], run_dir=tmp_path)
    assert result.stop_reason == "finished"
    stats = json.loads((tmp_path / "call_stats.json").read_text())
    child_config = stats["agents"]["sub1"]
    if limited:
        assert child_config["max_tool_calls"] == 12
        assert child_round < 24
        assert any("【工具额度】" in json.dumps(m, ensure_ascii=False) for m in seen_child)
    else:
        assert child_round == 25
        assert child_config["max_llm_calls"] is child_config["max_tool_calls"] is None
        assert child_config["tool_reserve_calls"] == 0
        assert all("【工具额度】" not in json.dumps(m, ensure_ascii=False) for m in seen_child)
        assert all("【模型额度】" not in json.dumps(m, ensure_ascii=False) for m in seen_child)
    assert {r["agent_id"] for r in stats["rounds"]} == {"lead", "sub1"}
    assert stats["totals"]["unfinished"] == 0
    assert stats["totals"]["tool_calls"] == sum(r["tool_calls"] for r in stats["rounds"])


def test_complete_io_and_rejections_recorded_without_debug(tmp_path):
    from dra import llm
    from dra.llm import ToolCall
    token = llm.set_trace_full_io(False)
    long_input = "long query " * 100
    call = ToolCall(id="real-id", name="search_sources", arguments={"query": long_input},
                    arguments_raw=json.dumps({"query": long_input}))
    turn = _turn()
    turn.tool_calls = [call]
    script = Script([turn, _turn(("finish_research", {"draft": "", "coverage": []}))],
                    finish=("finish_research", {"draft": "可用结果", "coverage": []}))
    try:
        sr.run_simple_research("q", model_call=script, search_fn=lambda q: [_doc(snippet="完整原文" * 1000)], run_dir=tmp_path)
    finally:
        llm.reset_trace_full_io(token)
    rows = [json.loads(s) for s in (tmp_path / "tool_calls.jsonl").read_text().splitlines()]
    done = [row for row in rows if row["phase"] == "done"]
    assert done[0]["arguments_raw"] == call.arguments_raw
    assert done[0]["arguments"] == call.arguments
    assert len(done[0]["result"]) > 600
    assert done[1]["status"] == "rejected"
    assert "draft 不能为空" in done[1]["result"]
    assert done[2]["status"] == "ok"


def test_tool_journal_failure_prevents_execution(tmp_path, monkeypatch):
    from pathlib import Path
    from dra.llm import LLMPersistenceError
    original = Path.open
    def fail_journal(path, *args, **kwargs):
        if path.name == "tool_calls.jsonl":
            raise OSError("disk full")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail_journal)
    searches = []
    with pytest.raises(LLMPersistenceError, match="工具调用记录保存失败"):
        sr.run_simple_research("q", run_dir=tmp_path, model_call=Script([_turn(("search_sources", {"query": "q"}))]),
                               search_fn=lambda q: searches.append(q))
    assert searches == []


def test_unlimited_cancel_preserves_pending_requests_without_executing(tmp_path):
    from dra import llm
    cancelled = threading.Event()
    def model(*args, **kwargs):
        cancelled.set()
        return _turn(("search_sources", {"query": "should not run"}))
    searches = []
    with llm.execution_scope(cancel_event=cancelled):
        result = sr.run_simple_research("q", run_dir=tmp_path, model_call=model,
                                       search_fn=lambda q: searches.append(q))
    assert result.stop_reason == "cancelled" and not searches
    stats = json.loads((tmp_path / "call_stats.json").read_text())
    assert stats["totals"]["tool_calls"] == stats["totals"]["unfinished"] == 1
    assert stats["totals"]["successful"] == 0


def test_zero_tool_rounds_and_no_progress_wrapup_are_preserved(tmp_path):
    script = Script([_turn(), _turn(), _turn()],
                    finish=("finish_research", {"draft": "无法继续", "coverage": []}))
    result = sr.run_simple_research("q", run_dir=tmp_path, model_call=script)
    assert result.stop_reason == "no_progress+wrapup"
    stats = json.loads((tmp_path / "call_stats.json").read_text())
    assert [r["tool_calls"] for r in stats["rounds"]] == [0, 0, 0, 1]
    assert stats["rounds"][-1]["step"] == "wrapup"
    prompt = json.dumps(script.seen[-1], ensure_ascii=False)
    assert "【剩余模型额度】" not in prompt and "工具额度用尽" not in prompt
