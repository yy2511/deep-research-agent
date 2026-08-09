"""RunStore：runs/<id>/ 落盘 round-trip + 孤儿扫描 + 统计聚合（纯本地，零网络）。"""
import json
from pathlib import Path

import pytest

from dra.events import EVENT_SCHEMA_VERSION
from dra.runstore import (
    IncompatibleEventSchemaError,
    PlanAttemptStore,
    RunStore,
    aggregate_stats,
)


def test_create_append_finalize_roundtrip(tmp_path):
    s = RunStore(tmp_path)
    rid = s.create_run("测试问题", {"clarified_query": "q", "initial_tasks": []}, {})
    assert (tmp_path / rid / "meta.json").exists()
    assert s.get_meta(rid)["status"] == "running"
    assert s.get_meta(rid)["event_schema_version"] == EVENT_SCHEMA_VERSION == 2
    s.append_event(rid, {"type": "scope", "ts": 1.0, "seq": 0})
    s.append_event(rid, {"type": "end", "ts": 2.0, "seq": 1})
    lines = s.read_events_text(rid).strip().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["seq"] == 0
    p = s.save_report(rid, "# 报告")
    assert p.read_text(encoding="utf-8") == "# 报告"
    s.finalize(rid, "done", {"wall_s": 1.0})
    m = s.get_meta(rid)
    assert m["status"] == "done" and m["stats"]["wall_s"] == 1.0 and m["finished_at"] is not None


def test_list_runs_sorted_desc(tmp_path):
    s = RunStore(tmp_path)
    a = s.create_run("先", None, {})
    b = s.create_run("后", None, {})
    ids = [m["run_id"] for m in s.list_runs()]
    assert ids.index(b) < ids.index(a)


def test_mark_orphans(tmp_path):
    s = RunStore(tmp_path)
    rid = s.create_run("孤儿", None, {})
    assert s.mark_orphans() == 1
    m = s.get_meta(rid)
    assert m["status"] == "failed" and m["fail_reason"] == "server restart"


def test_aggregate_stats_numbers():
    evts = [
        {"type": "scope", "ts": 100.0, "seq": 0},
        {"type": "llm_call", "ts": 101.0, "step": "extract", "model": "m1", "ms": 1000, "in_tok": 10, "out_tok": 5},
        {"type": "llm_call", "ts": 102.0, "step": "extract", "model": "m1", "ms": 3000, "in_tok": 20, "out_tok": 5},
        {"type": "step_done", "ts": 103.0, "step": "search", "ms": 500},
        {"type": "subagent_tool_call", "ts": 104.0, "tool": "save_evidence", "ok": True, "accepted": 2, "rejected": 1},
        {"type": "subagent_tool_call", "ts": 105.0, "tool": "search", "ok": True},
        {"type": "end", "ts": 110.0, "seq": 6},
    ]
    st = aggregate_stats(evts)
    llm = st["llm"][0]
    assert llm["step"] == "extract" and llm["calls"] == 2 and llm["total_ms"] == 4000 and llm["in_tok"] == 30
    assert st["steps"][0] == {"step": "search", "calls": 1, "total_ms": 500}
    tools = {t["tool"]: t for t in st["tools"]}
    assert tools["save_evidence"]["accepted"] == 2 and tools["save_evidence"]["rejected"] == 1
    assert st["wall_s"] == 10.0


def test_aggregate_stats_separates_summarize_and_extract():
    """summarize 与 extract 按 llm_call.step 分项聚合，不混在同一桶。"""
    evts = [
        {"type": "llm_call", "ts": 1.0, "step": "summarize", "model": "m1",
         "ms": 2000, "in_tok": 100, "out_tok": 50},
        {"type": "llm_call", "ts": 2.0, "step": "summarize", "model": "m1",
         "ms": 1000, "in_tok": 80, "out_tok": 40},
        {"type": "llm_call", "ts": 3.0, "step": "extract", "model": "m1",
         "ms": 5000, "in_tok": 200, "out_tok": 100},
    ]
    by_step = {row["step"]: row for row in aggregate_stats(evts)["llm"]}
    assert by_step["summarize"]["calls"] == 2
    assert by_step["summarize"]["total_ms"] == 3000
    assert by_step["summarize"]["in_tok"] == 180
    assert by_step["extract"]["calls"] == 1
    assert by_step["extract"]["total_ms"] == 5000


def test_plan_attempt_store_roundtrip(tmp_path):
    s = PlanAttemptStore(tmp_path)
    pid = s.create(query="芯片对比", kind="plan", config_summary={"planner": "x@y"})
    assert s.get_meta(pid)["status"] == "running"
    s.append_event(pid, {"type": "llm_call", "step": "build_research_plan", "seq": 0})
    s.append_event(pid, {"type": "step_done", "step": "build_research_plan", "seq": 1})
    text = s.read_events_text(pid)
    assert text is not None and "build_research_plan" in text
    s.finalize(
        pid, "ok",
        plan={
            "clarified_query": "芯片对比",
            "plan_nodes": [],
            "initial_tasks": [],
            "budget": {"max_research_rounds": 3},
            "trace": {"should": "not_be_in_meta"},
        },
    )
    meta = s.get_meta(pid)
    assert meta["status"] == "ok"
    assert meta["error"] is None
    assert meta["plan"]["clarified_query"] == "芯片对比"
    assert "trace" not in meta["plan"]


def test_plan_attempt_store_failed(tmp_path):
    s = PlanAttemptStore(tmp_path)
    pid = s.create(query="q", kind="plan", config_summary={})
    s.finalize(pid, "failed", error="planner 输出缺少有效 initial_tasks")
    meta = s.get_meta(pid)
    assert meta["status"] == "failed"
    assert "initial_tasks" in meta["error"]


def test_old_run_event_schema_is_explicitly_incompatible(tmp_path):
    s = RunStore(tmp_path)
    run_dir = tmp_path / "old-run"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(
        json.dumps({"run_id": "old-run", "status": "done", "started_at": 1}),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text('{"type":"scope"}\n', encoding="utf-8")

    [meta] = s.list_runs()
    assert meta["replay_compatible"] is False
    assert "不可回放" in meta["replay_error"]
    with pytest.raises(IncompatibleEventSchemaError, match="不可回放"):
        s.read_events_text("old-run")
