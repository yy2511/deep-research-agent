"""逐服务搜索诊断：HTTP 回执、缺配置、重试和并发归属，全部离线。"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import requests

from dra import simple_research as sr, subagent, tools
from dra.search_observation import SearchCall, request_attempt


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch):
    monkeypatch.setattr(tools, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("EXA_API_KEY", "fake-exa-secret")
    monkeypatch.setattr(subagent, "exa_search", tools.exa_search)
    monkeypatch.setattr(subagent, "web_search", tools.web_search)
    client = Mock()
    client.search.return_value = {"results": [{"url": "https://example.com/tavily", "content": "fact"}]}
    monkeypatch.setattr(tools, "_get_client", lambda: client)


def response(status, payload=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload or {"error": "balance exhausted fake-exa-secret"}).encode()
    return result


def context(path, run_id="test-run"):
    store = sr.open_run(path, run_id=run_id, query="original question")
    ctx = sr.ToolContext(store, sr.SimpleConfig())
    ctx.active_tool_call_id = "tool-1"
    ctx.model_call_id = "lead-1"
    return ctx


def read_rows(ctx):
    return [json.loads(line) for line in (ctx.store.dir / "search_calls.jsonl").read_text().splitlines()]


def test_payment_failure_is_logged_even_when_combined_search_succeeds(tmp_path, monkeypatch):
    post = Mock(return_value=response(402))
    monkeypatch.setattr(tools.requests, "post", post)
    sleep = Mock()
    monkeypatch.setattr(tools.time, "sleep", sleep)
    ctx = context(tmp_path / "run")
    payload, ok = ctx.search_sources({"query": "county data"})
    assert ok and len(json.loads(payload)["results"]) == 1
    assert set(json.loads(payload)) == {"results"}  # 诊断不改变模型输入。
    rows = read_rows(ctx)
    exa = [row for row in rows if row["provider"] == "exa"]
    assert [row["phase"] for row in exa] == ["started", "request_started", "request_done", "done"]
    assert exa[-1]["http_status"] == 402
    assert exa[-1]["error_category"] == "payment_required"
    assert exa[-1]["response_received"] is True
    assert exa[-1]["attempts"] == 1 and exa[-1]["status"] == "failed"
    assert exa[-1]["result_count"] == 0 and exa[-1]["elapsed_ms"] >= 0
    assert all(row["run_id"] == "test-run" and row["agent_id"] == "lead"
               and row["tool_call_id"] == "tool-1" and row["model_call_id"] == "lead-1" for row in rows)
    tavily = next(row for row in rows if row["provider"] == "tavily" and row["phase"] == "done")
    assert tavily["status"] == "ok" and tavily["result_count"] == 1
    assert tavily["response_received"] is True and tavily["http_status"] is None
    assert "fake-exa-secret" not in json.dumps(rows)
    post.assert_called_once()
    sleep.assert_not_called()


def test_missing_key_does_not_claim_a_request_was_sent(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY")
    post = Mock(side_effect=AssertionError("must not request"))
    monkeypatch.setattr(tools.requests, "post", post)
    ctx = context(tmp_path / "run")
    ctx.search_sources({"query": "q"})
    exa = [row for row in read_rows(ctx) if row["provider"] == "exa"]
    assert [row["phase"] for row in exa] == ["started", "done"]
    assert exa[-1]["attempts"] == 0 and exa[-1]["response_received"] is False
    assert exa[-1]["error_category"] == "missing_credentials"
    post.assert_not_called()


def test_retry_keeps_each_http_result_and_final_success(tmp_path, monkeypatch):
    post = Mock(side_effect=[response(503), response(200, {"results": [{"url": "https://example.com/exa"}]})])
    monkeypatch.setattr(tools.requests, "post", post)
    monkeypatch.setattr(tools.time, "sleep", lambda _: None)
    ctx = context(tmp_path / "run")
    payload, ok = ctx.search_sources({"query": "q"})
    assert ok and len(json.loads(payload)["results"]) == 2
    exa = [row for row in read_rows(ctx) if row["provider"] == "exa"]
    requests_done = [row for row in exa if row["phase"] == "request_done"]
    assert [(row["attempt"], row["http_status"]) for row in requests_done] == [(1, 503), (2, 200)]
    assert exa[-1]["attempts"] == 2 and exa[-1]["status"] == "ok"
    assert exa[-1]["http_status"] == 200 and exa[-1]["result_count"] == 1


def test_network_timeout_is_not_a_provider_response(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.requests, "post", Mock(side_effect=requests.Timeout("secret")))
    monkeypatch.setattr(tools.time, "sleep", lambda _: None)
    ctx = context(tmp_path / "run")
    ctx.search_sources({"query": "q"})
    exa = [row for row in read_rows(ctx) if row["provider"] == "exa"]
    assert exa[-1]["error_category"] == "timeout" and exa[-1]["attempts"] == 4
    assert exa[-1]["http_status"] is None and exa[-1]["response_received"] is False


def test_concurrent_runs_keep_query_and_agent_identity(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    def post(*a, **kw):
        barrier.wait(timeout=5)
        return response(200, {"results": []})
    monkeypatch.setattr(tools.requests, "post", post)
    a, b = context(tmp_path / "a", "run-a"), context(tmp_path / "b", "run-b")
    b.tool_role, b.delegation_id = "subagent", "sub1"
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(ctx.search_sources, {"query": query}) for ctx, query in [(a, "qa"), (b, "qb")]]
        assert all(f.result()[1] for f in futures)
    for ctx, query, agent in [(a, "qa", "lead"), (b, "qb", "sub1")]:
        rows = read_rows(ctx)
        assert len(rows) == 8
        assert all(row["run_id"] == ctx.store.run_id and row["query"] == query
                   and row["agent_id"] == agent for row in rows)


def test_failed_observer_does_not_change_search_result(monkeypatch, caplog):
    monkeypatch.setattr(tools.requests, "post", lambda *a, **kw: response(200, {"results": []}))
    def fail(row):
        raise OSError("sensitive error text")
    assert len(subagent._retrieve("q", subagent.SubAgentConfig(), on_provider_event=fail)) == 1
    assert "写入失败" in caplog.text and "sensitive error text" not in caplog.text


def test_observation_scope_does_not_leak_between_calls():
    rows = []
    with SearchCall(rows.append, "exa", "q", 1).scope() as trace:
        with request_attempt(1) as attempt:
            attempt.received(200)
        trace.finish(result_count=0)
    count = len(rows)
    with request_attempt(1):
        pass
    assert len(rows) == count
