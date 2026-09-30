"""Cache telemetry at the real LLM boundary; offline, no request rewriting."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace as NS

import httpx
import pytest

from dra import events, llm, timing
from dra.runstore import RunStore
from dra.llm_observation import aggregate_usage
from test_web_research_e2e import isolated_web, _wait_done  # noqa: F401 -- shared fixture


@contextmanager
def capture():
    rows = []
    handle = events.subscribe(rows.append)
    token = timing.set_ctx(step="lead_research", usage_role="lead", call_id="budget-1")
    try:
        yield rows
    finally:
        timing.reset_ctx(token)
        events.unsubscribe(handle)


def chunk(content=None, *, usage=None, finish=None, **delta):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=None, **delta), finish_reason=finish)], usage=usage)


def usage(**extra):
    return NS(prompt_tokens=100, completion_tokens=5, **extra)


class Stream:
    def __init__(self, parts):
        self.parts = parts
        self.closed = False

    def __iter__(self):
        yield from self.parts

    def close(self):
        self.closed = True


def fake(monkeypatch, replies):
    calls = []
    replies = iter(replies)

    def create(**kwargs):
        calls.append(deepcopy(kwargs))
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    client = NS(chat=NS(completions=NS(create=create)), base_url="https://opencode.ai/zen/go/v1/")
    monkeypatch.setattr(llm, "_get_client", lambda *_: client)
    return calls


def measured(rows):
    return [r for r in rows if r["type"] == "llm_usage"]


def test_stream_records_wire_cache_once_without_changing_request(monkeypatch, tmp_path):
    raw = usage(prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20,
                prompt_tokens_details=NS(cached_tokens=80))
    stream = Stream([chunk(), chunk("hello"), chunk(usage=raw, finish="stop")])
    calls = fake(monkeypatch, [stream])
    messages = [{"role": "user", "content": "source data"}]
    tools = [{"type": "function", "function": {"name": "read_source", "parameters": {}}}]
    with capture() as rows:
        turn = llm.call_tools(messages, tools=tools, provider="opencode", on_chunk=lambda _: None)
    assert turn.content == "hello" and stream.closed
    assert calls[0]["messages"] == messages and calls[0]["tools"] == tools
    metric, = measured(rows)
    assert metric["raw_usage"]["prompt_cache_hit_tokens"] == 80
    assert metric["cache_read_tokens"] == 80 and metric["cache_miss_tokens"] == 20
    assert metric["cache_status"] == "reported"
    assert metric["first_token_ms"] is not None and metric["first_content_ms"] is not None
    assert metric["balance_charge_usd"] is None and metric["cost_status"] == "unknown"
    assert metric["call_id"] == "budget-1" and metric["usage_role"] == "lead"
    store = RunStore(tmp_path)
    rid = store.create_run("q", None, {})
    store.append_event(rid, metric)
    stats = {"cache": aggregate_usage(rows)}
    store.finalize(rid, "done", stats)
    assert store.get_meta(rid)["stats"]["cache"]["cache_hit_rate"] == 0.8
    assert stats["cache"]["cache_read_tokens"] == 80  # Not 160 from adding aliases.


@pytest.mark.parametrize("fields,status,hit,miss", [
    ({"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100}, "reported", 0, 100),
    ({}, "unknown", None, None),
    ({"prompt_tokens_details": NS(cached_tokens=60)}, "derived_miss", 60, 40),
    ({"prompt_cache_hit_tokens": 60, "prompt_tokens_details": NS(cached_tokens=50)}, "inconsistent", None, None),
    ({"prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 60}, "inconsistent", None, None),
    ({"prompt_cache_hit_tokens": 101}, "inconsistent", None, None),
    ({"prompt_cache_hit_tokens": -1}, "inconsistent", None, None),
])
def test_zero_missing_fallback_and_conflicts(monkeypatch, fields, status, hit, miss):
    response = NS(usage=usage(**fields), choices=[NS(message=NS(content="ok", tool_calls=[]))])
    fake(monkeypatch, [response])
    with capture() as rows:
        llm.call_tools([], tools=[], provider="opencode")
    metric, = measured(rows)
    assert metric["cache_status"] == status
    assert metric["cache_read_tokens"] == hit and metric["cache_miss_tokens"] == miss
    assert metric["first_token_ms"] is None  # Non-streaming is not TTFT.


def test_retry_records_each_attempt_and_does_not_hide_unknown_usage(monkeypatch):
    request = httpx.Request("POST", "https://opencode.ai/zen/go/v1/chat/completions")
    error = httpx.ReadTimeout("test timeout", request=request)
    response = NS(usage=usage(prompt_cache_hit_tokens=80), choices=[NS(message=NS(content="ok", tool_calls=[]))])
    fake(monkeypatch, [error, response])
    monkeypatch.setattr(llm.time, "sleep", lambda _: None)
    with capture() as rows:
        llm.call_tools([], tools=[], provider="opencode", max_retries=1)
    first, second = measured(rows)
    assert [first["attempt"], second["attempt"]] == [1, 2]
    assert first["outcome"] == "error" and first["cache_status"] == "unknown"
    assert second["outcome"] == "returned"
    assert first["request_id"] != second["request_id"]
    stats = aggregate_usage(rows)
    assert stats["requests"] == 2 and stats["cache_observed_requests"] == 1
    assert stats["cache_coverage"] == 0.5 and stats["cache_hit_rate"] == 0.8
    assert stats["balance_charge_usd"] is None


def test_cancelled_stream_is_recorded_and_closed(monkeypatch):
    import threading
    cancelled = threading.Event()
    stream = Stream([chunk("one"), chunk("two"), chunk(finish="stop", usage=usage())])
    fake(monkeypatch, [stream])
    with capture() as rows, llm.execution_scope(cancel_event=cancelled):
        turn = llm.call_tools([], tools=[], provider="opencode", on_chunk=lambda _: cancelled.set())
    metric, = measured(rows)
    assert turn.stopped and stream.closed
    assert metric["outcome"] == "cancelled" and metric["raw_usage"] is None
    assert metric["cache_read_tokens"] is None


def test_text_chat_and_tool_only_deltas_are_observed(monkeypatch):
    tc = NS(index=0, id="call1", type="function", function=NS(name="inspect_research", arguments="{}"))
    tool_chunk = NS(choices=[NS(delta=NS(content=None, tool_calls=[tc]), finish_reason="tool_calls")],
                    usage=usage(prompt_cache_hit_tokens=20))
    fake(monkeypatch, [Stream([chunk(), tool_chunk]), Stream([chunk("answer"), chunk(finish="stop", usage=usage())])])
    with capture() as rows:
        llm.call_tools([], tools=[], provider="opencode", on_chunk=lambda _: None)
        llm.chat([], provider="opencode", on_chunk=lambda _: None)
    tool, text = measured(rows)
    assert tool["first_token_ms"] is not None and tool["first_content_ms"] is None
    assert text["first_token_ms"] is not None and text["first_content_ms"] is not None


def test_concurrent_agents_keep_identity(monkeypatch):
    replies = [NS(usage=usage(prompt_cache_hit_tokens=25), choices=[NS(message=NS(content="ok", tool_calls=[]))]) for _ in range(2)]
    fake(monkeypatch, replies)

    async def run(role, sid):
        token = timing.set_ctx(usage_role=role, sid=sid, delegation_id=sid if role != "lead" else None)
        try:
            return await asyncio.to_thread(llm.call_tools, [], tools=[], provider="opencode")
        finally:
            timing.reset_ctx(token)

    async def both():
        await asyncio.gather(run("lead", "main"), run("subagent", "sub1"))

    with capture() as rows:
        asyncio.run(both())
    assert {(r["usage_role"], r["sid"]) for r in measured(rows)} == {("lead", "main"), ("subagent", "sub1")}


def test_old_logs_do_not_appear_as_zero_cache_hit():
    stats = aggregate_usage([{"type": "llm_call", "in_tok": 100, "out_tok": 5}])
    assert stats["cache_hit_rate"] is None and stats["cache_read_tokens"] is None
    assert stats["cache_coverage"] is None and stats["balance_charge_usd"] is None


def test_ttft_ignores_empty_role_chunk_and_distinguishes_reasoning(monkeypatch):
    from dra import llm_observation
    now = [1.0]
    monkeypatch.setattr(llm_observation.time, "monotonic", lambda: now[0])

    class TimedStream(Stream):
        def __iter__(self):
            for stamp, value in [(1.1, chunk()), (1.4, chunk(reasoning_content="reasoning")),
                                 (1.8, chunk("answer")), (2.0, chunk(finish="stop", usage=usage()))]:
                now[0] = stamp
                yield value

    fake(monkeypatch, [TimedStream([])])
    with capture() as rows:
        llm.call_tools([], tools=[], provider="opencode", on_chunk=lambda _: None)
    metric, = measured(rows)
    assert metric["first_token_ms"] == 400
    assert metric["first_content_ms"] == 800 and metric["duration_ms"] == 1000


def test_provider_zero_cost_is_balance_charge_not_subscription_usage(monkeypatch):
    response = NS(cost="0", usage=usage(), choices=[NS(message=NS(content="ok", tool_calls=[]))])
    fake(monkeypatch, [response])
    with capture() as rows:
        llm.call_tools([], tools=[], provider="opencode")
    metric, = measured(rows)
    assert metric["provider_cost"] == "0" and metric["balance_charge_usd"] == 0
    assert metric["cost_status"] == "opencode_balance_charge"
    stats = aggregate_usage(rows)
    assert stats["balance_charge_usd"] == 0 and stats["subscription_usage_usd"] is None


def test_usage_received_before_transport_failure_is_preserved(monkeypatch):
    class BrokenStream(Stream):
        def __iter__(self):
            yield chunk("partial", usage=usage(prompt_cache_hit_tokens=25))
            raise httpx.ReadError("broken connection")

    stream = BrokenStream([])
    fake(monkeypatch, [stream])
    with capture() as rows, pytest.raises(RuntimeError):
        llm.call_tools([], tools=[], provider="opencode", max_retries=0, on_chunk=lambda _: None)
    metric, = measured(rows)
    assert metric["outcome"] == "error" and metric["cache_read_tokens"] == 25
    assert metric["raw_usage"]["prompt_tokens"] == 100 and stream.closed


def test_rates_are_token_weighted_not_average_of_request_percentages():
    rows = [{"type": "llm_usage", "input_tokens": n, "output_tokens": 1,
             "cache_read_tokens": h, "cache_miss_tokens": n-h, "cache_status": "reported"}
            for n, h in [(100, 100), (900, 0)]]
    assert aggregate_usage(rows)["cache_hit_rate"] == 0.1


@pytest.mark.parametrize("raw", ["invalid usage", [100, 5]])
def test_malformed_usage_does_not_break_a_valid_response(monkeypatch, raw):
    response = NS(usage=raw, choices=[NS(message=NS(content="ok", tool_calls=[]))])
    fake(monkeypatch, [response])
    with capture() as rows:
        turn = llm.call_tools([], tools=[], provider="opencode")
    metric, = measured(rows)
    assert turn.content == "ok" and metric["raw_usage"] == raw
    assert metric["cache_status"] == "unknown" and metric["input_tokens"] is None


def test_real_web_entry_persists_cache_stats_and_replays_usage(isolated_web, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from dra import web

    args = {"draft": "没有可核实资料。", "coverage": [{"requirement_id": "goal", "status": "unavailable"}]}
    tc = NS(index=0, id="finish", type="function", function=NS(name="finish_research", arguments=json.dumps(args)))
    finish = NS(choices=[NS(delta=NS(content=None, tool_calls=[tc]), finish_reason="tool_calls")],
                usage=usage(prompt_cache_hit_tokens=80, prompt_cache_miss_tokens=20))
    fake(monkeypatch, [Stream([finish])])
    with TestClient(web.app) as client:
        rid = client.post("/api/research", json={"query": "离线验证统计接口", "mode": "simple"}).json()["run_id"]
        _wait_done(client, rid)
        stats = client.get(f"/api/runs/{rid}").json()["meta"]["stats"]["cache"]
        assert stats["requests"] == 1 and stats["cache_read_tokens"] == 80
        assert stats["cache_coverage"] == 1 and stats["cache_hit_rate"] == .8
        replay = [json.loads(line) for line in client.get(f"/api/runs/{rid}/events").text.splitlines()]
        assert measured(replay)[0]["raw_usage"]["prompt_cache_miss_tokens"] == 20
        persisted = isolated_web[0].get_meta(rid)["stats"]["cache"]
        assert persisted == stats
