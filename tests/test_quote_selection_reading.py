"""真实重复来源与截断句问题的行为回归；不调用网络或模型。"""
import json

import pytest

from dra import simple_research as sr
from dra.models import RetrievedDoc


def context(tmp_path, texts, *, run_id="selection"):
    store = sr.RunStore(tmp_path, run_id=run_id, query="q")
    for index, text in enumerate(texts):
        store.write_source(RetrievedDoc(title=f"版本 {index}", snippet="", raw_content=text,
                                      source_url=f"https://example.com/v{index}"))
    ctx = sr.ToolContext(store, sr.SimpleConfig(condense_over_chars=None))
    for ref in [s["ref"] for s in store.state["sources"]]:
        assert ctx.read_source({"source_ref": ref})[1]
    return ctx


def save(ctx, quote="相同原话", **kwargs):
    text, ok = ctx.save_evidence({"cards": [{"claim": "结论", "quote": quote, **kwargs}]})
    return json.loads(text), ok


def test_source_scope_resolves_duplicate_without_merging_versions(tmp_path):
    ctx = context(tmp_path, ["页面 A\n相同原话", "页面 B\n相同原话"])
    body, ok = save(ctx, source_ref="s2")
    assert ok and body["results"][0]["accepted"]
    card = ctx.store.cards()[0]
    assert card.source_ref == "s2" and card.source_url.endswith("v1")
    assert len(ctx.store.state["sources"]) == 2


@pytest.mark.parametrize("ref", ["s1", "missing", "s3"])
def test_explicit_source_never_falls_back_to_another_source(tmp_path, ref):
    ctx = context(tmp_path, ["其它内容", "只有第二页包含的原话"])
    # s3 已保存但当前 Agent 没读过，也不能成为自动回退入口。
    ctx.store.write_source(RetrievedDoc(title="未读", snippet="", raw_content="只有第二页包含的原话",
                                       source_url="https://example.com/unread"))
    body, ok = save(ctx, "只有第二页包含的原话", source_ref=ref)
    assert ok and body['pending_selection'] == 1 and not body["results"][0]["accepted"]
    assert body['results'][0]['binding_status'] == 'source_mismatch'
    assert not ctx.store.cards()


def test_ambiguity_is_pending_selection_and_same_page_candidate_can_be_saved(tmp_path):
    raw = "第一处：相同原话。\n第二处：相同原话。"
    ctx = context(tmp_path, [raw])
    body, ok = save(ctx)
    row = body["results"][0]
    assert ok and not row["accepted"]  # 有效操作，但尚未生成卡片。
    assert row["status"] == "needs_selection"
    assert body["pending_selection"] == 1 and not ctx.store.cards()
    candidate = row["candidates"][1]
    selected, ok = save(ctx, candidate_id=candidate["candidate_id"])
    assert ok and selected["results"][0]["accepted"]
    card = ctx.store.cards()[0]
    assert card.location == candidate["location"]
    assert sr._chars_of(card.location)[0] == raw.rfind("相同原话")


@pytest.mark.parametrize("change", ["quote", "source", "unknown"])
def test_candidate_cannot_override_quote_or_source_validation(tmp_path, change):
    ctx = context(tmp_path, ["甲：相同原话。另一段真实文字", "乙：相同原话。"])
    body, _ = save(ctx)
    candidate = body["results"][0]["candidates"][0]["candidate_id"]
    item = {"claim": "结论", "quote": "相同原话", "candidate_id": candidate}
    if change == "quote":
        item["quote"] = "另一段真实文字"
    elif change == "source":
        item["source_ref"] = "s2"
    elif change == "unknown":
        item["candidate_id"] = "invented"
    payload, ok = ctx.save_evidence({"cards": [item]})
    assert not ok and not json.loads(payload)["results"][0]["accepted"]
    assert not ctx.store.cards()


def test_candidate_is_not_transferable_to_another_agent_or_run(tmp_path):
    ctx = context(tmp_path / "parent", ["相同原话", "相同原话"])
    body, _ = save(ctx)
    candidate = body["results"][0]["candidates"][0]["candidate_id"]
    child = ctx.fork_for_subagent(ctx.config, budget=None, delegation_id="sub1")
    for ref in ["s1", "s2"]:
        child.read_source({"source_ref": ref})
    other = context(tmp_path / "other", ["相同原话", "相同原话"], run_id="other")
    for target in [child, other]:
        result, ok = save(target, candidate_id=candidate)
        assert not ok and "candidate_id" in result["results"][0]["reason"]
        assert not target.store.cards()


def test_candidate_rechecks_current_read_exposure(tmp_path):
    ctx = context(tmp_path, ["相同原话", "相同原话"])
    body, _ = save(ctx)
    candidate = body["results"][0]["candidates"][0]
    ctx.read_exposures[candidate["source_ref"]] = []
    result, ok = save(ctx, candidate_id=candidate["candidate_id"])
    assert not ok and not result["results"][0]["accepted"]


def test_candidate_rechecks_snapshot_and_accepts_only_whitespace_variation(tmp_path):
    ctx = context(tmp_path, ["同一句\n原话", "同一句\n原话"])
    body, _ = save(ctx, "同一句 原话")
    candidate = body["results"][0]["candidates"][0]["candidate_id"]
    assert save(ctx, "同一句\r\n 原话", candidate_id=candidate)[1]
    assert ctx.store.cards()[0].quote == "同一句\n原话"
    ctx.readings["s1"].raw = "另一句\n原话"
    assert not save(ctx, "同一句 原话", candidate_id=candidate)[1]


def test_pending_candidate_survives_loop_history_without_failure_stop(tmp_path):
    from test_simple_research import _turn

    turn = 0

    def model(messages, **kwargs):
        nonlocal turn
        turn += 1
        if turn == 1:
            return _turn(("search_sources", {"query": "版本比较"}))
        if turn == 2:
            return _turn(("read_source", {"source_ref": "s1"}), ("read_source", {"source_ref": "s2"}))
        if turn < 6:
            return _turn(("save_evidence", {"cards": [{"claim": "结论", "quote": "相同原话"}]}))
        if turn == 6:
            receipt = json.loads(next(m["content"] for m in reversed(messages)
                                     if m.get("role") == "tool" and "needs_selection" in m.get("content", "")))
            candidate = receipt["results"][0]["candidates"][1]["candidate_id"]
            return _turn(("save_evidence", {"cards": [{"claim": "结论", "quote": "相同原话", "candidate_id": candidate}]}))
        return _turn(("finish_research", {"draft": "第二版本有相同原话。[c1]",
                                         "coverage": [{"requirement_id": "goal", "status": "answered"}]}))

    result = sr.run_simple_research("版本比较", sr.requirements_from_query("版本比较"),
        model_call=model, run_dir=tmp_path / "loop", config=sr.SimpleConfig(condense_over_chars=None),
        search_fn=lambda _q: [RetrievedDoc(title=f"版本 {i}", snippet="线索", raw_content="相同原话",
                                           source_url=f"https://example.com/v{i}") for i in range(2)])
    assert turn == 7 and len(result.cards) == 1
    assert result.cards[0].source_url.endswith("v1")


def test_pending_selection_preserves_read_context_and_display_counts(tmp_path):
    ctx = context(tmp_path, ["唯一句子；相同原话", "相同原话"])
    payload, ok = ctx.save_evidence({"cards": [
        {"claim": "已保存", "quote": "唯一句子"},
        {"claim": "待选出处", "quote": "相同原话"},
    ]})
    assert ok and "s1" not in ctx.archived
    display = sr._display_payload("save_evidence", {}, payload, 0)
    assert display["accepted"] == 1 and display["rejected"] == 0
    assert display["pending_selection"] == 1


def test_model_schema_exposes_optional_source_and_candidate(tmp_path):
    schema = next(t["function"]["parameters"] for t in sr.TOOL_SCHEMAS
                  if t["function"]["name"] == "save_evidence")
    card = schema["properties"]["cards"]["items"]
    quote = card["properties"]["quotes"]["items"]
    assert {"source_ref", "candidate_id"} <= quote["properties"].keys()
    assert quote['anyOf'] == [{'required': ['quote']}, {'required': ['candidate_id']}]
    assert set(card["required"]) == {"claim", "quotes"}


@pytest.mark.parametrize("read_args", [{"range": "0-6000"}, {"cursor": 100}, {"range": "L2-6"}])
def test_read_returns_one_continuous_window_and_keeps_old_ids(tmp_path, read_args):
    raw = ("一段正文" * 100 + "\n") * 20
    ctx = context(tmp_path, [raw])
    reading = ctx.readings["s1"]
    old_first = reading.quote(1)
    payload, ok = ctx.read_source({"source_ref": "s1", **read_args})
    body = json.loads(payload)
    assert ok and len(body["excerpts"]) == 1
    ex = body["excerpts"][0]
    lo, hi = sr._chars_of(ex["location"])
    assert ex["quote"] == raw[lo:hi]
    assert body["shown_range"] == ex["location"]
    assert reading.quote(1) == old_first
    repeated = json.loads(ctx.read_source({"source_ref": "s1", **read_args})[0])
    assert repeated["excerpts"][0]["excerpt_no"] == ex["excerpt_no"]


def test_short_not_null_reread_does_not_split_at_old_boundary(tmp_path):
    paragraph = "If the key part is defined as `NOT\nNULL`, an error is reported."
    prefix = "x" * (700 - len(paragraph.split("NULL")[0]))
    raw = prefix + paragraph + "\n" + "y" * 800
    ctx = context(tmp_path, [raw])
    result, _ = ctx.read_source({"source_ref": "s1", "range": f"{len(prefix)}-{len(prefix)+len(paragraph)}"})
    ex = json.loads(result)["excerpts"]
    assert len(ex) == 1 and ex[0]["quote"] == paragraph
    assert save(ctx, paragraph, source_ref="s1")[1]


def test_find_includes_wrapped_condition_and_does_not_expose_unshown_tail(tmp_path):
    paragraph = ("Queries using MEMBER OF() on JSON columns\n"
                 "can use multi-valued indexes.\n"
                 "If the key part is defined as `NOT\nNULL`, an error is reported.")
    raw = "标题\n\n" + paragraph + "\n\n" + "填充\n" * 2000 + "未展示的秘密"
    ctx = context(tmp_path, [raw])
    ctx.read_exposures.clear()
    payload, ok = ctx.read_source({"source_ref": "s1", "find": "MEMBER OF()"})
    body = json.loads(payload)
    assert ok and paragraph in body["excerpts"][0]["quote"]
    assert body["matches"][0]["context_next_cursor"] == sr._chars_of(body["excerpts"][0]["location"])[1]
    assert save(ctx, paragraph, source_ref="s1")[1]
    assert not save(ctx, "未展示的秘密", source_ref="s1")[1]


def test_find_long_context_is_bounded_and_can_continue(tmp_path):
    raw = "a" * 3000 + "needle" + "b" * 3000
    ctx = context(tmp_path, [raw])
    payload, ok = ctx.read_source({"source_ref": "s1", "find": "needle"})
    body = json.loads(payload)
    assert ok
    ex = body["excerpts"][0]
    assert "needle" in ex["quote"] and len(ex["quote"]) <= sr._FIND_CONTEXT_CHARS
    match = body["matches"][0]
    assert match["context_truncated"] is True
    tail = json.loads(ctx.read_source({"source_ref": "s1", "cursor": match["context_next_cursor"]})[0])
    assert tail["excerpts"][0]["quote"] == raw[match["context_next_cursor"]:]
