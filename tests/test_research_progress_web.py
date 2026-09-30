"""最终路线同步标记通过真实 HTTP、事件落盘与历史读取保持一致。"""
import json

import pytest
from fastapi.testclient import TestClient

from dra import research, web
from test_web_research_e2e import isolated_web, _wait_done  # noqa: F401
from test_research_progress import FINISH, ITEMS, PLAN
from test_budget_wrapup import _turn


@pytest.mark.parametrize("include_plan", [False, True])
def test_plan_sync_reaches_terminal_event_and_history(isolated_web, monkeypatch, include_plan):
    original = research.run_minimal_research
    def run(*args, **kwargs):
        kwargs["initial_route"] = ITEMS
        kwargs["model_call"] = lambda *_a, **_k: _turn(("finish_research",
            {**FINISH, **({"research_plan": PLAN} if include_plan else {})}))
        return original(*args, **kwargs)
    monkeypatch.setattr(research, "run_minimal_research", run)
    with TestClient(web.app) as client:
        response = client.post("/api/research", json={"query": "核对公开材料", "mode": "simple"})
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        _wait_done(client, run_id)
        payload = client.get(f"/api/runs/{run_id}/simple").json()
        rows = [json.loads(line) for line in client.get(f"/api/runs/{run_id}/events").text.splitlines()]
        terminal = next(e for e in rows if e["type"] == "research_status" and e["phase"] == "finished")
        expected = "reported" if include_plan else "missing"
        assert payload["plan_sync_status"] == terminal["plan_sync_status"] == expected
        assert payload["draft"] == FINISH["draft"]
        # 清除活跃记录后，重新读取仍由磁盘结果和事件重建，不依赖内存标记。
        web.REGISTRY.runs.pop(run_id, None)
        assert client.get(f"/api/runs/{run_id}/simple").json()["plan_sync_status"] == expected
        assert [json.loads(line) for line in client.get(f"/api/runs/{run_id}/events").text.splitlines()] == rows
