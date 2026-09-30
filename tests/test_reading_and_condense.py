"""阅读与上下文压缩：模型能看到什么、快照与接口一致、额度归属、压缩的覆盖范围与定位。"""
import html
import json
import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from dra import simple_research as sr, web
from dra.deeplink import text_fragment_url
from dra.runstore import RunStore as WebStore
from test_simple_research import _doc, _run, _turn


def _tool_payloads(messages):
    return [json.loads(m["content"]) for m in messages if m.get("role") == "tool"]


@pytest.mark.parametrize("locator", [{"range": "0-650"}, {"cursor": 0}, {"find": "N" * 150}])
def test_archived_explicit_read_reaches_model_until_saved(locator):
    raw = "A" * 400 + "N" * 150 + "B" * 100
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
        _turn(("save_evidence", {"cards": [{"claim": "first", "source_ref": "s2", "excerpt_no": 1}]})),
        _turn(("read_source", {"source_ref": "s2", **locator})),
        _turn(("inspect_research", {"view": "summary"})),
        _turn(("save_evidence", {"cards": [{"claim": "second", "source_ref": "s2", "excerpt_no": "{$excerpt:0}"}]})),
    ]
    _, script = _run(turns, raw=raw)
    for index in (4, 5):
        reads = [p for p in _tool_payloads(script.seen[index]) if p.get("source_ref") == "s2"]
        assert reads[0].get("archived") is True, "旧全文不能复活"
        assert "N" * 150 in "".join(e["quote"] for e in reads[-1].get("excerpts", []))
    reads = [p for p in _tool_payloads(script.seen[6]) if p.get("source_ref") == "s2"]
    assert all(p.get("archived") for p in reads), "再次存证后新回执也应收起"
    assert all("excerpts" in json.loads(r["payload"]) for r in script.context.receipts
               if r["tool"] == "read_source"), "账本原回执不改"


@pytest.mark.parametrize("bad", [{"claim": ""}, {"excerpt_no": 0}, {"excerpt_no": True}])
@pytest.mark.parametrize("ref", ["s1", "s2"])
def test_early_rejection_of_same_source_prevents_archive(bad, ref):
    _, script = _run([
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
        _turn(("save_evidence", {"cards": [
            {"claim": "ok", "source_ref": "s2", "excerpt_no": 1},
            {"claim": "bad", "source_ref": ref, "excerpt_no": 1, **bad},
        ]})),
    ])
    assert not script.context.archived


@pytest.mark.parametrize("raw", ["first\r\nsecond\r\n", "first\rsecond\nthird\r\n"])
def test_verbatim_newlines_survive_internal_and_http_reads(tmp_path, monkeypatch, raw):
    ws = WebStore(tmp_path)
    run_id = ws.create_run("q", None, {})
    store = sr.RunStore(tmp_path / run_id, run_id=run_id, query="q")
    snap, _ = store.write_source(_doc(raw=raw))
    assert (store.sources_dir / f"{snap.ref}.txt").read_bytes() == raw.encode()
    assert store.read_source_text(snap.ref) == raw
    assert ws.read_source_text(run_id, snap.ref) == raw
    monkeypatch.setattr(web, "STORE", ws)
    start, end = raw.index("second"), raw.index("second") + len("second")
    with TestClient(web.app) as client:
        url = f"/api/runs/{run_id}/sources/{snap.ref}"
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == raw.encode()
        response = client.get(url, params={"hl": f"{start}-{end}"})
        assert response.status_code == 200
        assert html.unescape(re.search(r'<mark id="cited">(.*?)</mark>', response.text, re.S)[1]) == "second"


@pytest.mark.parametrize("parent_used", [0, 19])
def test_delegated_condense_uses_its_own_budget(tmp_path, monkeypatch, parent_used):
    config = sr.SimpleConfig(condense_over_chars=100)
    store = sr.RunStore(tmp_path / "run", run_id="r", query="q")
    budget = sr.Budget(config, None)
    for _ in range(parent_used):
        budget.take("lead", "lead", store)
    ctx = sr.ToolContext(store, config, budget=budget,
        search_fn=lambda q: [_doc(raw="A" * 200)],
        condense_fn=lambda *a, **k: {"summary": "s", "excerpts": ["A" * 100]})
    observations = []

    def child_loop(store, config, requirements, *, budget, context, **kwargs):
        budget.take("subagent", "lead", store)
        context.search_sources({"query": "q"})
        payload, ok = context.read_source({"source_ref": "s1"})
        observations.append((ok, json.loads(payload)["view"], budget.calls_used))
        return {"stop_reason": "finished", "draft": "", "gaps": []}

    monkeypatch.setattr(sr, "run_lead_loop", child_loop)
    manager=sr._create_delegation_manager(store,ctx,config,budget,None)
    ctx.delegation_manager=manager
    manager.submit([("child", "")], "p")
    manager.close(cancel=False)
    assert observations == [(True, "condensed", 2)]
    assert budget.calls_used == parent_used


@pytest.mark.parametrize("length,fail", [(120010, False), (40001, False), (120010, True)])
def test_condense_receipt_discloses_actual_input(tmp_path, length, fail):
    raw = "x" * (length - 10) + "TAILMARKER"
    captured = []
    def model(messages, **kwargs):
        captured.append(messages[-1]["content"].split("【网页正文】\n")[1])
        if fail:
            raise TimeoutError("offline")
        return {"summary": "s", "excerpts": ["x" * 100]}
    config = sr.SimpleConfig()
    store = sr.RunStore(tmp_path / "r", run_id="r", query="q")
    ctx = sr.ToolContext(store, config, condense_fn=model, search_fn=lambda q: [_doc(raw=raw)])
    ctx.search_sources({"query": "q"})
    payload, ok = ctx.read_source({"source_ref": "s1"})
    data = json.loads(payload)
    assert ok
    assert data["condense_attempted"] is True
    assert data["condense_input_range"] == f"chars=0-{len(captured[0])}"
    assert data["condense_input_truncated"] == (length > 120000)
    if length > 120000:
        assert "TAILMARKER" not in captured[0]
        assert "120000" in data["note"]
    if fail:
        assert data["view"] == "slice"


def test_no_budget_does_not_claim_condense_coverage(tmp_path):
    config = sr.SimpleConfig(max_llm_calls=1)
    store = sr.RunStore(tmp_path / "r", run_id="r", query="q")
    ctx = sr.ToolContext(store, config, budget=sr.Budget(config, None),
        condense_fn=lambda *a, **k: pytest.fail("不该调用模型"),
        search_fn=lambda q: [_doc(raw="x" * 40001)])
    ctx.search_sources({"query": "q"})
    payload, ok = ctx.read_source({"source_ref": "s1"})
    data = json.loads(payload)
    assert ok and data["view"] == "slice"
    assert data["condense_attempted"] is False
    assert data["condense_input_range"] is None


@pytest.mark.parametrize("needle", ["N" * 150, "N" * 200, "abc\ndef"])
def test_find_shows_complete_match_and_stays_bounded(needle):
    raw = "x" * 120 + needle + "y" * 300
    reading = sr.Reading(None, raw)
    row = reading.find_matches(needle, context_chars=200)["rows"][0]
    assert needle in row["quote"]
    assert len(row["quote"]) <= 200
    assert reading.quote(row["excerpt_no"])[0] == row["quote"]


@pytest.mark.parametrize("size", [48, 49, 69, 95, 96])
def test_short_fragment_is_single_nonoverlapping_string(size):
    text = "".join(chr(0x4E00 + i) for i in range(size))
    directive = text_fragment_url("https://example.org", text).split("text=")[1]
    assert "," not in directive
    assert unquote(directive) == text


def test_long_fragment_edges_do_not_split_words():
    text = " ".join(f"encyclopedia{i}" for i in range(60))
    start, end = map(unquote, text_fragment_url("https://example.org", text).split("text=")[1].split(","))
    assert text.startswith(start) and text.endswith(end)
    assert text[len(start)].isspace()
    end_at = len(text) - len(end)
    assert text[end_at - 1].isspace()
    assert len(start) <= end_at


def test_fragment_encodes_hyphens_and_delimiters():
    text = "-alpha,beta&gamma%delta-"
    directive = text_fragment_url("https://example.org", text).split("text=")[1]
    assert "-" not in directive
    assert unquote(directive) == text


def test_multiline_fragment_keeps_distinct_blocks():
    text = "first complete line\n\nlast complete line"
    directive = text_fragment_url("https://example.org", text).split("text=")[1]
    start, end = map(unquote, directive.split(","))
    assert start == "first complete line"
    assert end == "last complete line"
