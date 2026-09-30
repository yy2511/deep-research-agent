"""旧执行退出后，现役入口、已有设置与历史报告继续可用。"""
import os
import subprocess
import sys

from fastapi.testclient import TestClient

from dra import web
from dra.runstore import RunStore


def test_current_entry_imports_do_not_load_retired_engines(tmp_path):
    script = """
import sys
from dra import research, runtime_config, web, research_history, context_compaction
runtime_config._code_defaults()
retired = {'nodes', 'orchestrator', 'planning', 'lead', 'toolloop', 'memory', 'initial_planner', 'postprocess'}
assert not (retired & {name.removeprefix('dra.') for name in sys.modules if name.startswith('dra.')} )
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "DRA_DATA_DIR": str(tmp_path), "DRA_EXECUTION_ENABLED": "false"})
    assert result.returncode == 0, result.stderr


def test_retired_routes_are_not_registered():
    routes = {route.path for route in web.app.routes}
    assert not {"/api/plan", "/api/plan/revise", "/api/research/{run_id}/resume"} & routes
    assert {"/api/brief", "/api/brief/plan", "/api/research"} <= routes


def test_plan_request_does_not_start_research(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("旧请求不能启动现役研究")
    monkeypatch.setattr(web.REGISTRY, "start_simple_research", unexpected)
    client = TestClient(web.app)
    assert client.post("/api/research", json={"query": "旧任务", "mode": "plan", "plan": {}}).status_code == 422


def test_current_report_is_readable(tmp_path, monkeypatch):
    store = RunStore(tmp_path / "runs")
    run_id = store.create_run("当前任务", {}, {"mode": "simple"})
    (store._dir(run_id) / "report.md").write_text("# 已有报告\n保留正文。", encoding="utf-8")
    store.finalize(run_id, "done", {})
    monkeypatch.setattr(web, "STORE", store)
    client = TestClient(web.app)
    response = client.get(f"/api/runs/{run_id}")
    assert response.status_code == 200
    assert response.json()["report_md"] == "# 已有报告\n保留正文。"
    assert client.get("/api/runs").json()[0]["run_id"] == run_id
