"""Historical inputs stay frozen between explicit archive/compaction boundaries."""
from copy import deepcopy
import json

import pytest

from dra import simple_research as sr
from dra.models import RetrievedDoc


def setup(tmp_path, *, archive_mode="immediate"):
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id="stable", query="original goal"),
                        sr.SimpleConfig(archive_mode=archive_mode, condense_over_chars=None))
    raw = "Alpha fact.\n\nBeta condition.\n\nGamma exception."
    ctx.store.write_source(RetrievedDoc(title="Source", source_url="https://example.com/a",
                                      snippet="", raw_content=raw))
    receipt, ok = ctx.read_source({"source_ref": "s1"})
    assert ok
    return ctx, exchange("read_source", receipt), raw


def exchange(name, content, call_id="call"):
    return [{"role": "assistant", "content": "Read.", "reasoning_content": "recorded reasoning",
             "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": call_id, "content": content}]


def save(ctx, quote):
    payload, ok = ctx.save_evidence({"cards": [{"claim": quote, "quote": quote}]})
    assert ok
    return payload


def build(ctx, history, base=None):
    return sr._build_messages(base or [{"role": "system", "content": "fixed rules"},
                                      {"role": "user", "content": "original goal"}],
                              ctx, [], history, seconds_left=None)


@pytest.mark.parametrize("change", ["save", "child_save", "read"])
def test_frozen_stub_ignores_later_shared_state(tmp_path, change):
    ctx, read, _ = setup(tmp_path)
    save(ctx, "Alpha fact.")
    original = deepcopy(sr._archived_exchange(ctx, read))
    if change == "save":
        save(ctx, "Beta condition.")
    elif change == "child_save":
        child = ctx.fork_for_subagent(ctx.config, budget=None, delegation_id="sub1")
        child.read_source({"source_ref": "s1"})
        save(child, "Beta condition.")
    else:
        ctx.read_source({"source_ref": "s1", "range": "13-28"})
    assert sr._archived_exchange(ctx, read) == original


def test_each_receipt_uses_first_subsequent_archive(tmp_path):
    ctx, read, _ = setup(tmp_path)
    save(ctx, "Alpha fact.")
    first = sr._archived_exchange(ctx, read)
    payload, _ = ctx.read_source({"source_ref": "s1", "range": "13-28"})
    reread = exchange("read_source", payload, "reread")
    assert sr._archived_exchange(ctx, reread) == reread
    save(ctx, "Beta condition.")
    assert sr._archived_exchange(ctx, read) == first
    second = sr._archived_exchange(ctx, reread)
    assert json.loads(second[-1]["content"])["archived"]
    save(ctx, "Gamma exception.")
    assert sr._archived_exchange(ctx, reread) == second


def test_new_cards_state_and_scope_only_append(tmp_path):
    ctx, read, _ = setup(tmp_path, archive_mode="keep")
    history = [read]
    a = build(ctx, history)
    frozen = deepcopy(a)
    receipt = save(ctx, "Alpha fact.")
    history.append(exchange("save_evidence", receipt, "save"))
    ctx.plan_items = [{"id": "p1", "text": "Compare", "status": "doing"}]
    b = build(ctx, history)
    assert b[:len(a)] == a == frozen
    assert json.loads(next(m["content"] for m in b if m.get("tool_call_id") == "save"))["results"][0]["quote"] == "Alpha fact."
    assert build(ctx, history) == b  # retry/no change does not grow messages
    changed_base = [{"role": "system", "content": "fixed rules"}, {"role": "user", "content": "NEW goal"}]
    history.append([{"role": "user", "content": "User: change direction"}])
    c = build(ctx, history, changed_base)
    assert c[:len(b)] == b
    assert any("NEW goal" in m.get("content", "") for m in c[len(b):])
    assert c[-1]["content"] == "User: change direction"


def test_child_notifications_are_per_agent_once_and_full_reread_stays(tmp_path):
    ctx, read, _ = setup(tmp_path, archive_mode="keep")
    child = ctx.fork_for_subagent(ctx.config, budget=None, delegation_id="sub1")
    child.read_source({"source_ref": "s1"})
    sibling = ctx.fork_for_subagent(ctx.config, budget=None, delegation_id="sub2")
    sibling_before = build(sibling, [])
    a = build(ctx, [read])
    save(child, "Beta condition.")
    b = build(ctx, [read])
    assert b[:len(a)] == a
    assert "c1" in json.dumps(b[len(a):])
    assert build(ctx, [read]) == b
    sibling_after = build(sibling, [])
    assert sibling_after[:len(sibling_before)] == sibling_before
    assert "c1" in json.dumps(sibling_after[len(sibling_before):])
    assert build(sibling, []) == sibling_after  # parent delivery cannot consume sibling's cursor
    payload, ok = ctx.inspect_research({"view": "cards", "card_ids": ["c1"]})
    assert ok and json.loads(payload)["cards"][0]["quote"] == "Beta condition."
    history = [read, exchange("inspect_research", payload, "inspect")]
    c = build(ctx, history)
    assert c[:len(b)] == b
    assert build(ctx, history) == c


def test_grouped_reads_preserve_full_cards_and_report_missing(tmp_path):
    ctx, _, _ = setup(tmp_path)
    save(ctx, "Alpha fact.")
    save(ctx, "Beta condition.")
    payload, ok = ctx.inspect_research({"view": "cards", "groups": [
        {"label": "Exceptions", "card_ids": ["c2", "c404"]},
        {"label": "Baseline", "card_ids": ["c1"]}]})
    body = json.loads(payload)
    assert not ok and body["missing_card_ids"] == ["c404"]
    assert [g["label"] for g in body["groups"]] == ["Exceptions", "Baseline"]
    for group, card in zip(body["groups"], reversed(ctx.store.cards())):
        row = group["cards"][0]
        for key in ("card_id", "quote", "claim", "source_ref", "source_url", "location"):
            assert row[key] == getattr(card, key)


@pytest.mark.parametrize("args", [
    {"card_id": "c1", "card_ids": ["c1"]}, {"card_ids": []}, {"card_ids": "c1"},
    {"groups": [{"label": "Missing ids"}]}, {"card_ids": ["c1"] * 51},
])
def test_invalid_batch_does_not_silently_return_directory(tmp_path, args):
    ctx, _, _ = setup(tmp_path)
    save(ctx, "Alpha fact.")
    payload, ok = ctx.inspect_research({"view": "cards", **args})
    assert not ok
    assert "错误" in payload or "error" in payload


def test_agent_notifications_are_delivered_once_even_over_twenty(tmp_path):
    ctx, read, _ = setup(tmp_path)
    ctx.delegation_manager = type("Manager", (), {"inspect": lambda _: []})()
    ctx.delegation_messages = [{"message_id": f"m{i}", "kind": "progress", "message": f"finding-{i}"}
                               for i in range(25)]
    a = build(ctx, [read])
    assert "finding-0" in json.dumps(a)
    assert build(ctx, [read]) == a
    ctx.delegation_messages.append({"message_id": "m26", "kind": "result", "message": "late result"})
    b = build(ctx, [read])
    assert b[:len(a)] == a
    assert "late result" in b[-1]["content"] and "finding-" not in b[-1]["content"]


def test_mcp_nested_archive_stays_frozen_and_other_fields_survive(tmp_path):
    ctx, read, _ = setup(tmp_path)
    ctx.mcp.tools["mcp_fetch"] = object()
    mcp = exchange("mcp_fetch", json.dumps({"server": "sample", "sources": [json.loads(read[-1]["content"])]}))
    save(ctx, "Alpha fact.")
    a = sr._archived_exchange(ctx, mcp)
    save(ctx, "Beta condition.")
    assert sr._archived_exchange(ctx, mcp) == a
    assert json.loads(a[-1]["content"])["server"] == "sample"


def test_long_batch_quotes_and_group_duplicates_are_not_truncated(tmp_path):
    ctx, _, _ = setup(tmp_path)
    raw = "Long evidence with conditions. " * 300 + "TAIL condition matters."
    ctx.store.write_source(RetrievedDoc(title="Long", source_url="https://example.com/b", snippet="", raw_content=raw))
    ctx.read_source({"source_ref": "s2"})
    save(ctx, raw)
    payload, ok = ctx.inspect_research({"view": "cards", "groups": [
        {"label": "Compare", "card_ids": ["c1", "c1"]}, {"label": "Limitations", "card_ids": ["c1"]}]})
    assert ok
    groups = json.loads(payload)["groups"]
    assert [len(g["cards"]) for g in groups] == [1, 1]
    assert all(g["cards"][0]["quote"] == raw for g in groups)


def test_reserved_batch_reads_do_not_consume_save_budget_or_allow_duplicates(tmp_path):
    from test_finish_and_trace import seeded, QUOTE, FINISH
    from test_budget_wrapup import Script, _turn
    config, store, budget, ctx = seeded(tmp_path)
    ctx.tool_calls = 2
    script = Script([
        _turn(("inspect_research", {"view": "cards", "groups": [{"label": "Limits", "card_ids": ["c1"]}]})),
        _turn(("inspect_research", {"view": "cards", "card_ids": ["c1"]})),
        _turn(("finish_research", FINISH)),
    ])
    result = sr.run_lead_loop(store, config, sr.requirements_from_query(store.query),
                              budget=budget, context=ctx, model_call=script)
    reads = [r for r in ctx.receipts if r["tool"] == "inspect_research"]
    assert [r["ok"] for r in reads] == [True, False]
    assert json.loads(reads[0]["payload"])["groups"][0]["cards"][0]["quote"] == QUOTE
    assert ctx.tool_calls == 2 and result["stop_reason"] == "finished"
