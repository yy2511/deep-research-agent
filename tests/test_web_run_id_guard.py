"""run_id 边界校验:非法 id 在进入文件路径构造前拒绝(2026-07-26 P1 修复)。

背景:runstore._dir = root / run_id 直接拼路径;此前唯一防线是 ASGI 层对
%2f 的路由处理(偶然安全)。修复后 4 个 {run_id} 端点显式复用
_PUBLIC_ID_PATTERN,「..」等非法 id 不再触达 RunStore/文件系统。

测法注记:httpx 客户端会把裸「..」路径段归一化掉(根本到不了服务器),
必须用 %2e%2e——服务端解码后才是「..」;并以守卫的 detail 文本
「run 不存在」区别于路由未命中的默认「Not Found」,证明请求确实进了 handler。
"""

from unittest.mock import MagicMock
import json

from fastapi.testclient import TestClient

from dra import web
from dra.runstore import RunStore

# 服务端解码后分别是 ".."、".hidden"、"a b"——全都不符合 _PUBLIC_ID_PATTERN
_BAD_IDS = ("%2e%2e", ".hidden", "a%20b")


def _spy_store(monkeypatch):
    spy = MagicMock(wraps=web.STORE)
    monkeypatch.setattr(web, "STORE", spy)
    return spy


def test_traversal_like_run_id_rejected_before_store(monkeypatch):
    spy = _spy_store(monkeypatch)
    with TestClient(web.app) as client:
        for bad in _BAD_IDS:
            r = client.get(f"/api/runs/{bad}")
            assert r.status_code == 404, bad
            assert r.json()["detail"] == "run 不存在", bad   # 守卫拒绝,非路由未命中
    spy.get_meta.assert_not_called()
    spy.read_report.assert_not_called()


def test_events_stream_cancel_reject_invalid_run_id(monkeypatch):
    spy = _spy_store(monkeypatch)
    with TestClient(web.app) as client:
        r = client.get("/api/runs/%2e%2e/events")
        assert (r.status_code, r.json()["detail"]) == (404, "run 不存在")
        r = client.post("/api/research/%2e%2e/cancel")
        assert (r.status_code, r.json()["detail"]) == (404, "run 不存在")
        r = client.get("/api/research/%2e%2e/stream")
        assert (r.status_code, r.json()["detail"]) == (404, "run 不存在")
    spy.read_events_text.assert_not_called()


def test_valid_but_missing_run_id_still_404_via_store(monkeypatch):
    """合法形态的 id 照常走 store 查询(不误伤正常路径)。"""
    spy = _spy_store(monkeypatch)
    with TestClient(web.app) as client:
        r = client.get("/api/runs/20990101-000000-deadbeef")
        assert r.status_code == 404
    spy.get_meta.assert_called_once_with("20990101-000000-deadbeef")


def test_old_event_schema_replay_returns_409(tmp_path, monkeypatch):
    store = RunStore(tmp_path)
    run_id = "20260728-000000-oldproto"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(
        json.dumps({"run_id": run_id, "status": "done", "started_at": 1}),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text('{"type":"scope"}\n', encoding="utf-8")
    monkeypatch.setattr(web, "STORE", store)

    with TestClient(web.app) as client:
        response = client.get(f"/api/runs/{run_id}/events")

    assert response.status_code == 409
    assert "不可回放" in response.json()["detail"]
