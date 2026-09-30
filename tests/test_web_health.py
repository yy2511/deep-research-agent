"""Web 健康检查：无外部调用、可写性探针与失败状态。"""

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from dra import web


def test_healthz_reports_capacity_without_external_calls(monkeypatch):
    monkeypatch.setattr(web.REGISTRY, "active_count", lambda: 1)
    monkeypatch.setattr(web.REGISTRY, "max_active_runs", 2)
    blocked_llm = MagicMock(side_effect=AssertionError("healthz 不得调用模型"))
    monkeypatch.setattr(web.llm, "chat", blocked_llm)

    with TestClient(web.app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "active_runs": 1,
        "max_active_runs": 2,
    }
    blocked_llm.assert_not_called()


def test_healthz_returns_503_without_exposing_server_path(monkeypatch):
    monkeypatch.setattr(web, "_first_unwritable_data_dir", lambda: "plans")

    with TestClient(web.app) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "plans data directory is not writable"
    }


def test_writability_probe_cleans_up_temporary_files(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    runs_dir = data_dir / "runs"
    plans_dir = data_dir / "plans"
    runs_dir.mkdir(parents=True)
    plans_dir.mkdir()
    monkeypatch.setattr(web, "DATA_DIR", data_dir)
    monkeypatch.setattr(web, "RUNS_DIR", runs_dir)
    monkeypatch.setattr(web, "PLANS_DIR", plans_dir)

    assert web._first_unwritable_data_dir() is None
    assert list(data_dir.rglob(".dra-health-*")) == []


def test_writability_probe_identifies_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "DATA_DIR", tmp_path)
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path / "missing-runs")
    monkeypatch.setattr(web, "PLANS_DIR", tmp_path / "missing-plans")

    assert web._first_unwritable_data_dir() == "runs"
