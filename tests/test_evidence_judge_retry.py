"""原卡补判的暂时故障、时限与落盘合同；完全离线。"""
import asyncio
import json
import threading
import time

import httpx2
import pytest

from dra import simple_research as sr
from test_evidence_judge import context, install_transport, response

CARD = {"card_id": "c1", "claim": "室内使用", "quote": "产品仅在室内使用。"}


def configure(monkeypatch, handler):
    ej = install_transport(monkeypatch, handler)
    monkeypatch.setattr(ej, "RETRY_BACKOFF_S", .001, raising=False)
    return ej


@pytest.mark.parametrize("failure", ["timeout", "connect", 408, 429, 500, 502, 503, 504, 529])
def test_transient_failure_retries_identical_evidence(monkeypatch, failure):
    requests = []
    async def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            if failure == "timeout":
                raise httpx2.ReadTimeout("secret", request=request)
            if failure == "connect":
                raise httpx2.ConnectError("secret", request=request)
            return httpx2.Response(failure, text="secret")
        return httpx2.Response(200, json=response("A"))
    ej = configure(monkeypatch, handler)
    result = ej.judge_cards([CARD])["c1"]
    assert result.relation == "supported"
    assert requests[0] == requests[1] and len(requests) == 2
    assert len(result.attempts) == 2
    assert result.usage == {"input_tokens": 120, "output_tokens": 0}
    assert result.usage_unknown  # 前次失败的消耗不能被成功结果覆盖。
    assert "secret" not in result.model_dump_json()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_errors_do_not_retry(monkeypatch, status):
    seen = []
    async def handler(_):
        seen.append(True)
        return httpx2.Response(status)
    ej = configure(monkeypatch, handler)
    result = ej.judge_cards([CARD])["c1"]
    assert len(seen) == 1 and result.relation is None
    assert len(result.attempts) == 1


def test_exhausted_attempts_remain_unverified(monkeypatch):
    seen = []
    async def handler(_):
        seen.append(True)
        return httpx2.Response(503)
    ej = configure(monkeypatch, handler)
    result = ej.judge_cards([CARD])["c1"]
    assert len(seen) == 3 and len(result.attempts) == 3
    assert result.error_code == "http_503" and result.relation is None
    assert result.retry_stop_reason == "attempt_limit"


def test_request_timeout_leaves_time_for_retry(monkeypatch):
    seen = []
    cleaned = []
    async def handler(_):
        seen.append(True)
        if len(seen) == 1:
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.append(True)
        return httpx2.Response(200, json=response("A"))
    ej = configure(monkeypatch, handler)
    monkeypatch.setattr(ej, "REQUEST_TIMEOUT_S", .02, raising=False)
    result = ej.judge_cards([CARD], deadline=time.monotonic() + .5)["c1"]
    assert result.relation == "supported" and cleaned == [True]
    assert result.attempts[0].error_code == "request_timeout"


def test_retry_after_beyond_deadline_is_not_shortened(monkeypatch):
    seen = []
    async def handler(_):
        seen.append(True)
        return httpx2.Response(429, headers={"Retry-After": "120"})
    ej = configure(monkeypatch, handler)
    result = ej.judge_cards([CARD], deadline=time.monotonic() + .3)["c1"]
    assert len(seen) == 1 and result.error_code == "http_429"
    assert result.retry_stop_reason == "insufficient_time"
    assert result.attempts[0].retry_after_s == 120


def test_cancel_during_backoff_preserves_attempt_and_releases_slot(monkeypatch):
    cancelled = threading.Event()
    async def handler(_):
        asyncio.get_running_loop().call_later(.01, cancelled.set)
        return httpx2.Response(503)
    ej = configure(monkeypatch, handler)
    monkeypatch.setattr(ej, "RETRY_BACKOFF_S", .2)
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(ej, "_slots", slots)
    result = ej.judge_cards([CARD], cancelled=cancelled.is_set)["c1"]
    assert result.status == "cancelled" and len(result.attempts) == 1
    assert result.attempts[0].error_code == "http_503"
    assert slots.acquire(blocking=False)
    slots.release()


def test_queue_deadline_has_no_attempt_or_usage(monkeypatch):
    ej = configure(monkeypatch, lambda _: pytest.fail("queued request sent"))
    monkeypatch.setattr(ej, "_slots", threading.BoundedSemaphore(0))
    result = ej.judge_cards([CARD], deadline=time.monotonic() + .03)["c1"]
    assert result.status == "timeout" and result.error_code == "queue_timeout"
    assert not result.attempts and not result.attempted and not result.usage_unknown
    assert result.retry_stop_reason == "research_deadline" and result.queue_ms > 0


def test_retry_persists_on_original_card_and_receipt(tmp_path, monkeypatch):
    calls = []
    async def handler(_):
        calls.append(True)
        return httpx2.Response(503) if len(calls) == 1 else httpx2.Response(200, json=response("A"))
    configure(monkeypatch, handler)
    ctx, ref = context(tmp_path)
    text, ok = ctx.save_evidence({"cards": [{"claim": CARD["claim"], "quote": CARD["quote"], "source_ref": ref}]})
    row = json.loads(text)["results"][0]
    stored = json.loads((tmp_path / "run.json").read_text())
    assert ok and len(stored["cards"]) == 1 and row["card_id"] == "c1"
    assert row["judgment"]["relation"] == "supported"
    assert row["judgment"] == sr._judgment_for_model(stored["cards"][0]["judgment"])
    assert len(stored["cards"][0]["judgment"]["attempts"]) == 2

@pytest.mark.parametrize("choice", ["A", "B", "C"])
def test_valid_semantic_judgment_is_not_retried(monkeypatch, choice):
    seen = []
    async def handler(_):
        seen.append(True)
        return httpx2.Response(200, json=response(choice))
    ej = configure(monkeypatch, handler)
    result = ej.judge_cards([CARD])["c1"]
    assert len(seen) == 1 and result.status == "complete" and not result.usage_unknown


def test_successful_peer_finishes_while_failed_card_waits(monkeypatch):
    seen = []
    async def handler(request):
        claim = json.loads(request.content)["state"]["claim"]
        seen.append(claim)
        if claim == "first" and seen.count("first") == 1:
            return httpx2.Response(503)
        return httpx2.Response(200, json=response("A"))
    ej = configure(monkeypatch, handler)
    monkeypatch.setattr(ej, "RETRY_BACKOFF_S", .03)
    monkeypatch.setattr(ej, "_slots", threading.BoundedSemaphore(1))
    rows = ej.judge_cards([{**CARD, "claim": "first"}, {**CARD, "card_id": "c2", "claim": "peer"}])
    assert seen == ["first", "peer", "first"]
    assert all(r.relation == "supported" for r in rows.values())


def test_retry_after_http_date_obeys_deadline(monkeypatch):
    from email.utils import formatdate
    async def handler(_):
        return httpx2.Response(503, headers={"Retry-After": formatdate(time.time()+120, usegmt=True)})
    ej = configure(monkeypatch, handler)
    result = ej.judge_cards([CARD], deadline=time.monotonic()+.1)["c1"]
    assert len(result.attempts) == 1 and result.retry_stop_reason == "insufficient_time"
    assert result.attempts[0].retry_after_s > 118


def test_global_parallelism_with_retries_across_threads(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    active = peak = 0
    counts = {}
    lock = threading.Lock()
    async def handler(request):
        nonlocal active, peak
        claim = json.loads(request.content)["state"]["claim"]
        with lock:
            counts[claim] = counts.get(claim, 0)+1
            first = counts[claim] == 1
            active += 1
            peak = max(peak, active)
        try:
            await asyncio.sleep(.01)
            return httpx2.Response(503) if first else httpx2.Response(200, json=response("A"))
        finally:
            with lock:
                active -= 1
    ej = configure(monkeypatch, handler)
    with ThreadPoolExecutor(3) as pool:
        batches = list(pool.map(lambda n: ej.judge_cards([
            {**CARD, "card_id": f"c{i}", "claim": f"{n}-{i}"} for i in range(6)]), range(3)))
    assert 1 < peak <= ej.CONCURRENCY and active == 0
    assert all(r.relation == "supported" and len(r.attempts) == 2 for batch in batches for r in batch.values())
