"""RunStore：runs/<id>/ 落盘 round-trip + 孤儿扫描（纯本地，零网络）。"""
import json

import pytest

from dra.events import EVENT_SCHEMA_VERSION
from dra.runstore import (
    IncompatibleEventSchemaError,
    PlanAttemptStore,
    RunStore,
)


def test_create_append_finalize_roundtrip(tmp_path):
    s = RunStore(tmp_path)
    rid = s.create_run("测试问题", {"clarified_query": "q", "initial_tasks": []}, {})
    assert (tmp_path / rid / "meta.json").exists()
    assert s.get_meta(rid)["status"] == "running"
    assert s.get_meta(rid)["event_schema_version"] == EVENT_SCHEMA_VERSION == 3
    s.append_event(rid, {"type": "scope", "ts": 1.0, "seq": 0})
    s.append_event(rid, {"type": "end", "ts": 2.0, "seq": 1})
    lines = s.read_events_text(rid).strip().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["seq"] == 0
    (tmp_path / rid / "report.md").write_text("# 报告", encoding="utf-8")
    assert s.read_report(rid) == "# 报告"
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


def test_plan_attempt_store_roundtrip(tmp_path):
    s = PlanAttemptStore(tmp_path)
    pid = s.create(query="芯片对比", kind="plan", config_summary={"planner": "x@y"})
    meta_path = tmp_path / pid / "meta.json"
    assert json.loads(meta_path.read_text(encoding="utf-8"))["status"] == "running"
    s.append_event(pid, {"type": "llm_call", "step": "research_brief", "seq": 0})
    s.append_event(pid, {"type": "step_done", "step": "research_brief", "seq": 1})
    text = (tmp_path / pid / "events.jsonl").read_text(encoding="utf-8")
    assert text is not None and "research_brief" in text
    s.finalize(
        pid, "ok",
        plan={"kind": "plan", "brief": {"goal": "芯片对比", "requirements": []}},
    )
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "ok"
    assert meta["error"] is None
    assert meta["plan"]["brief"]["goal"] == "芯片对比"
    assert "trace" not in meta["plan"]


def test_plan_attempt_store_failed(tmp_path):
    s = PlanAttemptStore(tmp_path)
    pid = s.create(query="q", kind="plan", config_summary={})
    s.finalize(pid, "failed", error="planner 输出缺少有效 initial_tasks")
    meta = json.loads((tmp_path / pid / "meta.json").read_text(encoding="utf-8"))
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
