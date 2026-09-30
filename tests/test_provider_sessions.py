"""OpenCode 会话路由：隔离 Worker，跨重试/恢复稳定，不污染其它 provider。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from dra import events, llm, timing


def test_headers_stable_across_calls_and_resume_and_isolate_workers(monkeypatch):
    monkeypatch.setattr(events, "get_run_id", lambda: "same-durable-run")
    first = llm._provider_headers("opencode")
    with llm.execution_scope(session_id="same-durable-run"):
        assert llm._provider_headers("opencode") == first
    with llm.execution_scope(session_id="same-durable-run"):
        assert llm._provider_headers("opencode") == first
        token = timing.set_ctx(usage_role="worker", sid="worker-one")
        try:
            worker_one = llm._provider_headers("opencode")
            assert worker_one != first
            assert llm._provider_headers("opencode") == worker_one
        finally:
            timing.reset_ctx(token)
        token = timing.set_ctx(usage_role="worker", sid="worker-two")
        try:
            assert llm._provider_headers("opencode") != worker_one
        finally:
            timing.reset_ctx(token)
    monkeypatch.setattr(events, "get_run_id", lambda: "different-run")
    assert llm._provider_headers("opencode") != first
    assert llm._provider_headers("deepseek") == {}
    assert llm._provider_headers("openai") == {}


def test_scope_session_propagates_to_threads_and_nested_scopes(monkeypatch):
    monkeypatch.setattr(events, "get_run_id", lambda: None)

    async def one():
        with llm.execution_scope():
            parent = llm._provider_headers("opencode")
            child = await asyncio.to_thread(llm._provider_headers, "opencode")
            with llm.execution_scope():
                nested = llm._provider_headers("opencode")
            assert parent == child == nested
            return parent

    async def both():
        return await asyncio.gather(one(), one())

    a, b = asyncio.run(both())
    assert a != b


def test_text_and_native_tools_both_send_own_identity(monkeypatch):
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))],
        usage=None,
    )
    monkeypatch.setattr(llm, "_get_client", lambda provider: client)
    monkeypatch.setattr(events, "get_run_id", lambda: "run-1")
    llm.chat([{"role": "user", "content": "hi"}], provider="opencode")
    llm.call_tools([{"role": "user", "content": "hi"}], tools=[], provider="opencode")
    sent = [c.kwargs["extra_headers"] for c in client.chat.completions.create.call_args_list]
    assert sent[0] == sent[1]
    assert sent[0]["User-Agent"] == "deep-research-agent/0.0.1"
    assert sent[0]["x-opencode-session"].startswith("dra-")
