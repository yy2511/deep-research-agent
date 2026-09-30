"""存证入口、真实账本和 Jev 网络边界；默认离线。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import httpx2
import pytest

from dra import simple_research as sr
from dra.models import RetrievedDoc


def context(tmp_path, enabled=True):
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id="judge-test", query="q"),
                         sr.SimpleConfig(evidence_judge={"enabled": enabled}))
    source, _ = ctx.store.write_source(RetrievedDoc(id="d1", title="技术说明", snippet="",
        source_url="https://example.com/doc", raw_content="产品仅在室内使用。工作温度为 0 到 40 摄氏度。"))
    ctx.read_source({"source_ref": source.ref})
    return ctx, source.ref


def response(choice="C"):
    return {"model": "jev-1.13.0", "answers": {"relation": {"type": "choice", "choice": choice,
        "confidence": .98, "probabilities": {k: .98 if k == choice else .01 for k in "ABC"}}},
        "usage": {"input_tokens": 120, "output_tokens": 0}}


def install_transport(monkeypatch, handler):
    from dra import evidence_judge as ej
    original = httpx2.AsyncClient
    monkeypatch.setattr(ej, "_client", lambda: original(transport=httpx2.MockTransport(handler)))
    monkeypatch.setattr(ej, "_api_key", lambda: "unit-test-secret")
    return ej


def test_save_judges_only_bound_quotes_and_persists_same_receipt(tmp_path, monkeypatch):
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=response())
    install_transport(monkeypatch, handler)
    ctx, ref = context(tmp_path)
    text, ok = ctx.save_evidence({"cards": [
        {"claim": "产品能在室外使用", "quote": "产品仅在室内使用。", "source_ref": ref},
        {"claim": "产品完全防水", "quote": "编造的引文", "source_ref": ref}]})
    rows = json.loads(text)["results"]
    assert ok and rows[0]["accepted"] and not rows[1]["accepted"]
    assert len(seen) == 1
    assert seen[0]["state"]["claim"] == "产品能在室外使用"
    assert seen[0]["state"]["evidence"][0]["quote"] == "产品仅在室内使用。"
    assert "工作温度" in seen[0]["state"]["evidence"][0]["context"][0]["text"]
    judgment = rows[0]["judgment"]
    assert judgment["relation"] == "insufficient" and judgment["status"] == "complete"
    persisted = json.loads((tmp_path / "run.json").read_text())
    assert persisted["cards"][0]["judgment"] == judgment
    assert sr._card_payload(ctx.store.cards()[0])["judgment"] == judgment
    assert any(a["action"] == "evidence_judgment" for a in persisted["actions"])
    assert "unit-test-secret" not in json.dumps(persisted)
    assert sr.citation_gate("事实 [c1]", ctx.store.cards()) == (True, [])


def test_disabled_does_not_call_network(tmp_path, monkeypatch):
    from dra import evidence_judge as ej
    monkeypatch.setattr(ej, "judge_cards", lambda *a, **kw: pytest.fail("disabled judge called"))
    ctx, ref = context(tmp_path, False)
    text, _ = ctx.save_evidence({"cards": [{"claim": "室内使用", "quote": "产品仅在室内使用。", "source_ref": ref}]})
    assert json.loads(text)["results"][0].get("judgment") is None


def test_partial_timeout_preserves_completed_and_joins_cancelled_requests(monkeypatch):
    active = set()
    async def handler(request):
        claim = json.loads(request.content)["state"]["claim"]
        active.add(claim)
        try:
            if claim == "slow":
                await asyncio.Event().wait()
            return httpx2.Response(200, json=response("A"))
        finally:
            active.remove(claim)
    ej = install_transport(monkeypatch, handler)
    rows = ej.judge_cards([{"card_id": x, "claim": x, "quote": "q"} for x in ["fast", "slow"]],
                          deadline=time.monotonic() + .1)
    assert rows["fast"].relation == "supported"
    assert rows["slow"].status == "timeout" and rows["slow"].relation is None
    assert rows["slow"].attempted and rows["slow"].usage_unknown
    assert active == set()


@pytest.mark.parametrize("bad", [None, {"model": "jev-1.13.0"}, response("X"),
    {**response(), "answers": {"relation": {"type": "choice", "choice": "A", "confidence": .98,
                                         "probabilities": {"A": .01, "B": .01, "C": .98}}}}])
def test_invalid_response_is_unverified(monkeypatch, bad):
    async def handler(_):
        return httpx2.Response(200, json=bad)
    ej = install_transport(monkeypatch, handler)
    result = ej.judge_cards([{"card_id": "c1", "claim": "a", "quote": "b"}])["c1"]
    assert result.status == "error" and result.relation is None
    assert result.error_code == "invalid_response"


def test_global_parallelism_across_parent_and_children(monkeypatch):
    active = peak = 0
    lock = threading.Lock()
    async def handler(_):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            await asyncio.sleep(.02)
            return httpx2.Response(200, json=response("B"))
        finally:
            with lock:
                active -= 1
    ej = install_transport(monkeypatch, handler)
    cards = [{"card_id": f"c{i}", "claim": "a", "quote": "b"} for i in range(9)]
    with ThreadPoolExecutor(3) as pool:
        batches = list(pool.map(lambda _: ej.judge_cards(cards), range(3)))
    assert 1 < peak <= 5 and active == 0
    assert all(r.relation == "contradicted" for batch in batches for r in batch.values())


def test_missing_key_and_pre_cancel_do_not_attempt(monkeypatch):
    from dra import evidence_judge as ej
    monkeypatch.setattr(ej, "_api_key", lambda: "")
    cards = [{"card_id": "c1", "claim": "a", "quote": "b"}]
    missing = ej.judge_cards(cards)["c1"]
    assert missing.status == "unavailable" and not missing.attempted and missing.relation is None
    monkeypatch.setattr(ej, "_api_key", lambda: "unit-test-secret")
    cancelled = ej.judge_cards(cards, cancelled=lambda: True)["c1"]
    assert cancelled.status == "cancelled" and not cancelled.attempted


def test_parent_receives_late_child_judgment_once(tmp_path, monkeypatch):
    from dra import evidence_judge as ej
    ctx, ref = context(tmp_path, False)
    ctx.save_evidence({"cards": [{"claim": "室内使用", "quote": "产品仅在室内使用。", "source_ref": ref}]})
    base = [{"role": "user", "content": "研究"}]
    first = sr._build_messages(base, ctx, [], [], seconds_left=None)
    ctx.store.state["cards"][0]["judgment"] = ej.Judgment(status="complete", relation="insufficient").model_dump()
    second = sr._build_messages(base, ctx, [], [], seconds_left=None)
    third = sr._build_messages(base, ctx, [], [], seconds_left=None)
    assert len(second) > len(first) and second == third
    assert "insufficient" in json.dumps(second)


def test_cancelling_inflight_request_cleans_up_before_return(monkeypatch):
    cancelled = threading.Event()
    cleaned = []
    async def handler(_):
        cancelled.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(True)
    ej = install_transport(monkeypatch, handler)
    result = ej.judge_cards([{"card_id": "c1", "claim": "a", "quote": "b"}],
                            cancelled=cancelled.is_set)["c1"]
    assert result.status == "cancelled" and result.relation is None
    assert result.attempted and result.usage_unknown and cleaned == [True]


def test_cancel_while_queued_remains_cancelled_without_http(monkeypatch):
    ej = install_transport(monkeypatch, lambda _: pytest.fail("queued request was sent"))
    cancelled = threading.Event()
    class FullSlots:
        def acquire(self, blocking):
            cancelled.set()
            return False
    monkeypatch.setattr(ej, "_slots", FullSlots())
    result = ej.judge_cards([{"card_id":"c1", "claim":"a", "quote":"b"}],
                            cancelled=cancelled.is_set)["c1"]
    assert result.status == "cancelled" and not result.attempted and not result.usage_unknown


@pytest.mark.parametrize("status", [401, 403, 422])
def test_http_failures_are_not_negative_evidence_or_retried(monkeypatch, status):
    seen = []
    async def handler(_):
        seen.append(True)
        return httpx2.Response(status, text="unit-test-secret")
    ej = install_transport(monkeypatch, handler)
    result = ej.judge_cards([{"card_id": "c1", "claim": "a", "quote": "b"}])["c1"]
    assert result.status == "error" and result.error_code == f"http_{status}"
    assert result.relation is None and result.usage_unknown and len(seen) == 1
    assert "unit-test-secret" not in result.model_dump_json()
