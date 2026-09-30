"""真实 HTTP→调度→研究→落盘链；只替换外部模型和检索，不消耗模型额度。"""
import hashlib
import json
import time

from fastapi.testclient import TestClient
import pytest

from dra import web, research, runtime_config
from dra.simple_research import SimpleConfig
from test_research_history import history_fixture
from test_web_research_e2e import _wait_done
from test_budget_wrapup import Script, _turn
from test_document_library import library, add


@pytest.mark.parametrize("call_limits_enabled", [False, True])
def test_two_followups_preserve_original_and_import_provenance(tmp_path, monkeypatch, call_limits_enabled):
    store, parent, _, raw = history_fixture(tmp_path)
    registry = web.RunRegistry(store, max_active_runs=1)
    monkeypatch.setattr(web, "STORE", store)
    monkeypatch.setattr(web, "REGISTRY", registry)
    settings = runtime_config._code_defaults().model_copy(update={"call_limits_enabled": call_limits_enabled})
    monkeypatch.setattr(runtime_config, "current", lambda: settings)
    monkeypatch.setattr(runtime_config, "to_subagent_config", lambda _: SimpleConfig().adapter)
    calls = []
    original = research.run_minimal_research
    def run(query, requirements=None, **kwargs):
        assert kwargs["config"].max_llm_calls == (20 if call_limits_enabled else None)
        assert kwargs["config"].max_tool_calls == (20 if call_limits_enabled else None)
        script = Script([
            _turn(("read_research_history", {"view": "cards", "turn": 1, "card_ids": ["c1"]})),
            _turn(("finish_research", {"draft": "历史材料记载日志保留 43 天 [c1]。",
                "coverage": [{"requirement_id": r.id, "status": "answered", "note": "已核查历史快照"}
                             for r in requirements]})),
        ])
        calls.append(script)
        return original(query, requirements, model_call=script, **kwargs)
    monkeypatch.setattr(research, "run_minimal_research", run)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in store._dir(parent).iterdir() if p.is_file()}
    with TestClient(web.app) as client:
        latest = parent
        for turn in (2, 3):
            body = {"request_id": f"follow-{turn}", "text": "解释日志保留政策"}
            response = client.post(f"/api/research/{latest}/followup", json=body)
            assert response.status_code == 202, response.text
            new = response.json()["run_id"]
            assert response.json()["turn_index"] == turn
            done = _wait_done(client, new)
            assert done["meta"]["status"] == "done", done
            # final meta 可以先于后台 finally；真实句柄退出以后才接收下一轮。
            deadline = time.monotonic() + 2
            while registry.runs[new].busy and time.monotonic() < deadline:
                time.sleep(.01)
            assert client.post(f"/api/research/{latest}/followup", json=body).json()["run_id"] == new
            result = client.get(f"/api/runs/{new}/simple").json()
            assert result["cards"][0]["origin"]["run_id"] == parent
            assert result["cards"][0]["quote"] in raw
            assert result["sources"][0]["revalidated_at"] is None
            material = client.get(f"/api/runs/{new}/materials").json()
            assert material["cards"] == result["cards"]
            assert client.get(f"/api/runs/{new}/sources/s1").text == raw
            latest = new
        assert len(calls) == 2
        assert client.post(f"/api/research/{parent}/followup", json={"request_id":"stale", "text":"继续"}).status_code == 409
        turns = client.get(f"/api/conversations/{parent}").json()
        assert turns["total"] == 3 and turns["latest_run_id"] == latest
        assert len(client.get("/api/conversations").json()) == 1
        assert "read_research_history" in calls[0].tool_schemas[0]
        assert "⟦第1轮·c1⟧" in str(calls[0].seen[0])
    assert hashes == {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in store._dir(parent).iterdir() if p.is_file()}


def test_followup_waits_for_worker_and_persistence_failure_does_not_schedule(tmp_path, monkeypatch):
    from dra import durable
    from dra.research_history import HistoryError
    store, parent, _, _ = history_fixture(tmp_path)
    registry = web.RunRegistry(store)
    h = web.RunHandle(parent)
    h.busy = True
    registry.runs[parent] = h
    with pytest.raises(HistoryError, match="保存结果"):
        registry.start_followup(parent, "busy", "继续", config=SimpleConfig())
    h.busy = False
    original = durable.atomic_json
    def fail(path, data):
        if path.name == "followup_context.json":
            raise OSError("disk full")
        original(path, data)
    monkeypatch.setattr(durable, "atomic_json", fail)
    monkeypatch.setattr(web.asyncio, "get_running_loop", lambda: pytest.fail("不可启动外部执行"))
    with pytest.raises(OSError, match="disk full"):
        registry.start_followup(parent, "disk", "继续", config=SimpleConfig())
    latest = store.conversation_turns(parent)[-1]
    assert latest["status"] == "failed"
    assert registry.start_followup(parent, "disk", "继续", config=SimpleConfig()) == latest["run_id"]


def test_materials_are_readable_while_running_and_corruption_is_visible(tmp_path, monkeypatch):
    store, parent, _, _ = history_fixture(tmp_path)
    monkeypatch.setattr(web, "STORE", store)
    with TestClient(web.app) as client:
        assert client.get(f"/api/runs/{parent}/materials").json()["cards"]
        assert client.get("/api/runs/missing/materials").status_code == 404
        (store._dir(parent) / "run.json").write_text("broken")
        assert client.get(f"/api/runs/{parent}/materials").status_code == 503


def test_followup_private_scope_keeps_version_and_rejects_removed_or_missing_index(library):
    from dra.library import LibraryError
    old = add(library, "日志保留 43 天。")
    selection = library.pin([old["document_id"]])
    newer = add(library, "日志保留 9 天。", replace=old["document_id"])
    assert newer["version_id"] != old["version_id"]
    assert library.validate_selection(selection) == selection
    index = library.root / "versions" / old["version_id"] / "vectors.npy"
    content = index.read_bytes()
    index.unlink()
    with pytest.raises(LibraryError, match="索引缺失"):
        library.validate_selection(selection)
    index.write_bytes(content)
    library.remove(old["document_id"])
    with pytest.raises(LibraryError, match="移出"):
        library.validate_selection(selection)


@pytest.mark.parametrize("mode,max_calls", [("normal", 5), ("wrapup", 2)])
def test_followup_bad_coverage_keeps_body_and_reports_missing_metadata(tmp_path, monkeypatch, mode, max_calls):
    store, parent, _, _ = history_fixture(tmp_path)
    registry = web.RunRegistry(store, max_active_runs=1)
    monkeypatch.setattr(web, "STORE", store)
    monkeypatch.setattr(web, "REGISTRY", registry)
    monkeypatch.setattr(runtime_config, "current", runtime_config._code_defaults)
    monkeypatch.setattr(runtime_config, "to_subagent_config", lambda _: SimpleConfig().adapter)
    scripts = []
    original = research.run_minimal_research

    def run(query, requirements=None, **kwargs):
        bad = {"draft": "历史材料记载日志保留 43 天 [c1]。", "gaps": [],
               "coverage": [{"requirement_id": "goal", "status": "answered"}]}
        script = Script([
            _turn(("read_research_history", {"view": "cards", "turn": 1, "card_ids": ["c1"]})),
            _turn(("finish_research", bad)),
        ])
        scripts.append(script)
        kwargs["config"] = kwargs["config"].model_copy(update={"max_llm_calls": max_calls})
        return original(query, requirements, model_call=script, **kwargs)

    monkeypatch.setattr(research, "run_minimal_research", run)
    with TestClient(web.app) as client:
        response = client.post(f"/api/research/{parent}/followup",
                               json={"request_id": "coverage-unreported", "text": "解释日志保留政策"})
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        _wait_done(client, run_id)
        result = client.get(f"/api/runs/{run_id}/simple").json()
        rows = [json.loads(line) for line in client.get(f"/api/runs/{run_id}/events").text.splitlines()]
        finishes = [e for e in rows if e["type"] == "subagent_tool_call"
                    and e.get("tool") == "finish_research" and e.get("phase") == "done"]
        assert len(finishes) == 1 and finishes[0]["ok"] is True
        assert len(result["cards"]) == 1, "错编号不能丢掉已保存材料"
        terminal = next(e for e in rows if e["type"] == "research_status" and e["phase"] == "finished")
        assert terminal["coverage"] == result["research_coverage"]
        assert terminal["coverage_reasons"] == result["coverage_reasons"]
        assert terminal["gaps"] == result["gaps"]
        assert result["draft"] == "历史材料记载日志保留 43 天 [c1]。"
        assert result["delivery_status"] == "delivered" and not result["draft_rejected"]
        assert result["research_coverage"] == {"followup": "unanswered", "inherited-1": "unanswered"}
        assert all("未报告" in " ".join(reasons) for reasons in result["coverage_reasons"].values())
        assert len(scripts[0].seen) == len(result["calls"]) == 2, "元信息错误不额外请求修复"
        expected_stop = "finished" if mode == "normal" else "call_budget+wrapup"
        assert result["stop_reason"] == expected_stop
        # 历史重读与终态接口采用同一份已保存结果。
        assert json.loads((store._dir(run_id) / "simple.json").read_text()) == result
