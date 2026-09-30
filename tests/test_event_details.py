"""摘要传输保留身份/统计；完整记录只在展开时读取，磁盘事件不改写。"""
import json

from fastapi.testclient import TestClient

from dra import web
from test_research_history import history_fixture


def test_compact_history_and_event_details_preserve_original(tmp_path, monkeypatch):
    store, run_id, _, _ = history_fixture(tmp_path)
    monkeypatch.setattr(web, "STORE", store)
    rows = [
        {"type": "llm_usage", "seq": 0, "request_id": "r1", "call_id": "m1",
         "input_tokens": 300, "cache_read_tokens": 240, "io_complete": True,
         "request_messages": [{"role": "system", "content": "完整提示词"}],
         "request_tools": [{"name": "read_source"}]},
        {"type": "llm_call", "seq": 1, "call_id": "m1", "input": "完整输入",
         "output": "完整输出", "tools": [], "io_complete": True},
        {"type": "subagent_tool_call", "seq": 2, "call_id": "t1", "phase": "done",
         "args_summary": "输入摘要", "result_summary": "输出摘要",
         "arguments_raw": "完整工具输入", "result_full": "完整工具输出", "io_complete": True},
        {"type": "agent_reasoning", "seq": 3, "text": "公开的可读推理", "msg_id": "a1"},
    ]
    original = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path = store._dir(run_id) / "events.jsonl"
    path.write_text(original)
    with TestClient(web.app) as client:
        response = client.get(f"/api/runs/{run_id}/events?compact=true")
        assert response.status_code == 200
        compact = [json.loads(line) for line in response.text.splitlines()]
        assert "request_messages" not in compact[0]
        assert compact[0]["input_tokens"] == 300 and compact[0]["detail_seq"] == 0
        assert compact[1]["call_id"] == "m1" and "output" not in compact[1]
        assert compact[2]["args_summary"] == "输入摘要" and "arguments_raw" not in compact[2]
        assert compact[3] == rows[3]
        for seq, row in enumerate(rows):
            assert client.get(f"/api/runs/{run_id}/events/{seq}").json() == row
        assert client.get(f"/api/runs/{run_id}/events/99").status_code == 404
        assert client.get(f"/api/runs/{run_id}/events").text == original
    assert path.read_text() == original


def test_compact_sse_matches_history_without_mutating_live_buffer(tmp_path, monkeypatch):
    store, run_id, _, _ = history_fixture(tmp_path)
    monkeypatch.setattr(web, "STORE", store)
    registry = web.RunRegistry(store)
    monkeypatch.setattr(web, "REGISTRY", registry)
    handle = web.RunHandle(run_id)
    registry.runs[run_id] = handle
    handle.done = True
    handle.buffer = [
        {"seq": 0, "type": "llm_usage", "call_id": "m1", "request_messages": [{"role": "user", "content": "正文"}]},
        {"seq": 1, "type": "end"},
    ]
    with TestClient(web.app) as client:
        response = client.get(f"/api/research/{run_id}/stream?compact=true")
        projected = [json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")]
        assert projected[0].get("detail_seq") == 0
        assert "request_messages" not in projected[0]
        assert projected[-1]["type"] == "end"
    assert handle.buffer[0]["request_messages"][0]["content"] == "正文"
