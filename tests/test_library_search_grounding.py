"""资料库检索：不漏掉命中、证据卡编号不错配。只验证确定的规则，不评价语义准确率。"""
import json

import pytest

from dra import library as lib, simple_research as sr
from dra.models import RetrievedDoc
from test_simple_research import Script, _turn


def context(tmp_path, **options):
    store = sr.RunStore(tmp_path, run_id="grounding", query="当前问题的完整限定")
    config = sr.SimpleConfig(source_mode="library", library_selection={"version_ids": ["v1"]}, **options)
    return sr.ToolContext(store, config)


def document(ids, version="v1"):
    return RetrievedDoc(title="资料", snippet="线索", document={
        "document_id": "d1", "version_id": version, "file_name": "资料.md",
        "matches": [{"id": i, "version_id": version, "start": n * 10, "end": n * 10 + 5,
                     "text": i, "score": 0.1} for n, i in enumerate(ids)]})


def search_spy(monkeypatch, fn):
    class Library:
        search = staticmethod(fn)
    monkeypatch.setattr(lib, "get_library", lambda: Library())


def test_first_search_keeps_both_rankings_and_one_copy_of_each_chunk(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    calls = []
    def search(query, selection, **kwargs):
        calls.append(query)
        assert selection == ctx.config.library_selection
        return [document(["shared"] + [f"{query}-{i}" for i in range(7)])]
    search_spy(monkeypatch, search)
    receipt, ok = ctx.search_sources({"query": "改写"})
    assert ok
    assert calls == ["改写", ctx.effective_query]
    row = json.loads(receipt)["results"][0]
    assert "matches" not in row["document"]  # 同一回执不重复正文块。
    assert len(row["matches"]) == 15
    assert row["document"]["retrieval_queries"] == {"q1": calls[0], "q2": calls[1]}
    assert row["matches"][0]["matched_queries"] == ["q1", "q2"]
    assert {m["id"] for m in row["matches"]} == {"shared"} | {
        f"{q}-{i}" for q in calls for i in range(7)}
    # 保存元数据仍有命中块，默认 read_source 的定位不受影响。
    summary = next(s for s in ctx.store.state["sources"] if s["ref"] == row["source_ref"])
    assert summary["document"]["matches"] == row["matches"]
    ctx.search_sources({"query": "下一角度"})
    assert calls == ["改写", ctx.effective_query, "下一角度"]


def test_anchor_failure_preserves_primary_hits_and_can_retry(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    calls = []
    def search(query, selection, **kwargs):
        calls.append(query)
        if query == ctx.effective_query:
            raise lib.LibraryError("temporary failure")
        return [document(["primary"])]
    search_spy(monkeypatch, search)
    receipt, ok = ctx.search_sources({"query": "改写"})
    payload = json.loads(receipt)
    assert ok and payload["results"][0]["matches"][0]["text"] == "primary"
    assert payload["warnings"]
    ctx.search_sources({"query": "重试角度"})
    assert calls.count(ctx.effective_query) == 2


def test_steering_uses_new_goal_and_versions_never_expand(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    calls = []
    def search(query, selection, **kwargs):
        calls.append(query)
        assert selection["version_ids"] == ["v1"]
        return [document([query])]
    search_spy(monkeypatch, search)
    ctx.search_sources({"query": ctx.effective_query})  # 原问题本身不重复搜索。
    assert calls == [ctx.effective_query]
    ctx.effective_query = "用户改向后的目标"
    ctx.search_sources({"query": "新角度"})
    assert calls[-2:] == ["新角度", "用户改向后的目标"]


def test_child_anchor_is_its_mission_not_parent_query(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    child = ctx.fork_for_subagent(ctx.config, budget=sr.Budget(ctx.config, None))
    calls = []
    search_spy(monkeypatch, lambda query, selection, **kw: calls.append(query) or [document([query])])
    script = Script([_turn(("search_sources", {"query": "子任务关键词"})),
                     _turn(("finish_research", {"draft": "资料不足", "coverage": [], "gaps": ["未存证"]}))])
    sr.run_lead_loop(ctx.store, ctx.config, [sr.Requirement(id="sub", text="子任务目标", kind="goal")],
                     budget=child.budget, context=child, role="subagent", mission_text="子任务目标",
                     schemas=sr.TOOL_SCHEMAS, max_turns=2, model_call=script)
    assert calls == ["子任务关键词", "子任务目标"]
    assert ctx.effective_query == "当前问题的完整限定"


def test_long_anchor_is_bounded_and_reported(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    ctx.effective_query = "很长的问题" * 500
    calls = []
    search_spy(monkeypatch, lambda query, selection, **kw: calls.append(query) or [document([query])])
    payload, ok = ctx.search_sources({"query": "简短关键词"})
    assert ok and len(calls) == 2 and len(calls[1]) == 2000
    assert "2000" in " ".join(json.loads(payload)["warnings"])


def test_anchor_uses_first_paragraph_without_changing_full_mission(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    full = "用户实际问题是什么？\n\n随后是输出格式或其他要求，需要单独处理。"
    ctx.effective_query = full
    calls = []
    search_spy(monkeypatch, lambda query, selection, **kw: calls.append(query) or [document([query])])
    receipt, ok = ctx.search_sources({"query": "关键词"})
    assert ok and calls == ["关键词", "用户实际问题是什么？"]
    assert ctx.effective_query == full
    assert "首段" in " ".join(json.loads(receipt)["warnings"])


def test_partial_save_receipt_maps_real_ids_to_full_quotes_and_retry(tmp_path):
    ctx = context(tmp_path)
    ctx.config = ctx.config.model_copy(update={"source_mode": "web"})
    raw = "第一项原文。" + "背景" * 140 + "第一项决定性条件只在尾部。\n第二项的独立原文。"
    ctx.store.write_source(RetrievedDoc(title="正文", snippet="", raw_content=raw, source_url="https://example.org/source"))
    assert ctx.read_source({"source_ref": "s1"})[1]
    quote = raw.split("\n")[0]
    receipt, ok = ctx.save_evidence({"cards": [
        {"claim": "应拒收的结论", "quote": "不存在的原话"},
        {"claim": "第一项结论", "quote": quote},
        {"claim": "第二项结论", "quote": "第二项的独立原文。"}]})
    data = json.loads(receipt)
    assert ok and data["results"][0]["claim"] == "应拒收的结论"
    for row, card in zip(data["results"][1:], ctx.store.cards()):
        assert row["card_id"] == card.card_id
        span = row["quotes"][0]
        assert row["claim"] == card.claim and span["quote"] == card.quote
        assert span["source_ref"] == card.source_ref and span["location"] == card.location
    assert data["results"][1]["index"] == 2 and data["results"][1]["card_id"] == "c1"
    assert "序号" in data["note"]
    # 模拟历史交换含早前错误配号；当前权威卡片映射在历史之后、含尾部条件。
    history = [[{"role": "assistant", "content": "旧的编号猜测 c2"}]]
    messages = sr._build_messages([], ctx, [], history, seconds_left=None)
    assert messages[-1]["role"] == "user"
    assert quote in messages[-1]["content"] and "[c1] 第一项结论" in messages[-1]["content"]
    retried, _ = ctx.save_evidence({"cards": [{"claim": "补充结论", "quote": "第二项的独立原文。"}]})
    assert json.loads(retried)["results"][0]["card_id"] == "c3"
    assert ctx.store.cards()[0].quote == quote


def test_web_search_does_not_query_library_or_add_goal(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    ctx.config = ctx.config.model_copy(update={"source_mode": "web"})
    search_spy(monkeypatch, lambda *a, **k: pytest.fail("web called library"))
    queries = []
    ctx.search_fn = lambda q: queries.append(q) or [RetrievedDoc(title="网页", snippet="线索", source_url="https://example.org")]
    assert ctx.search_sources({"query": "网页关键词"})[1]
    assert queries == ["网页关键词"]
