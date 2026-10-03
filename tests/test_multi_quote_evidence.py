"""多处原文联合存证；运行默认不访问外部模型。"""
import json
import re

import httpx2

from dra import simple_research as sr
from dra.models import RetrievedDoc
from test_evidence_judge import install_transport, response


def setup_context(tmp_path, *, enabled=False):
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id="multi-quote", query="q"),
                         sr.SimpleConfig(evidence_judge={"enabled": enabled}))
    refs = []
    raws = ["# 2016 全明星\n\n## 参赛阵容\n\nUzi 属于 Team Ice。\n\n" + "其他赛制。\n" * 180 +
            "\n## 最终成绩\n\nTeam Ice 获得第一名。\n\n比赛在巴塞罗那举行。",
            "# 2016 全明星补充\n\n这是队伍赛事，另有个人项目。\n"]
    for i, raw in enumerate(raws):
        src, _ = ctx.store.write_source(RetrievedDoc(title=f"全明星 {i}", snippet="",
            source_url=f"https://example.com/{i}", raw_content=raw))
        ctx.read_source({"source_ref": src.ref})
        refs.append(src.ref)
    return ctx, refs, raws


def test_distant_and_cross_source_spans_are_one_card_one_judgment(tmp_path, monkeypatch):
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=response("A"))
    install_transport(monkeypatch, handler)
    ctx, refs, raws = setup_context(tmp_path, enabled=True)
    quotes = [{"source_ref": refs[0], "quote": "Uzi 属于 Team Ice。"},
              {"source_ref": refs[0], "quote": "Team Ice 获得第一名。"},
              {"source_ref": refs[1], "quote": "这是队伍赛事，另有个人项目。"}]
    text, ok = ctx.save_evidence({"cards": [{"claim": "Uzi 所属 Team Ice 在队伍赛事获得第一名", "quotes": quotes}]})
    assert ok, text
    row = json.loads(text)["results"][0]
    assert row["accepted"] and len(ctx.store.cards()) == len(seen) == 1
    assert len(row["quotes"]) == len(seen[0]["state"]["evidence"]) == 3
    assert ctx.store.cards()[0].judgment.prompt_version == "evidence-support-v3"
    saved = ctx.store.cards()[0].model_dump()
    assert sr._card_payload(ctx.store.cards()[0])["quotes"] == row["quotes"]
    assert [(q["quote"], q["location"]) for q in saved["quotes"]] == [(q["quote"], q["location"]) for q in row["quotes"]]
    for original, span in zip(quotes, saved["quotes"]):
        assert span["submitted_quote"] == original["quote"]
        assert span["quote"] == original["quote"]
        raw = raws[refs.index(span["source_ref"])]
        a, b = map(int, re.fullmatch(r"chars=(\d+)-(\d+)", span["location"]).groups())
        assert raw[a:b] == span["quote"]
    assert refs[1] in ctx.archived


def test_partial_binding_failure_never_saves_or_judges_subset(tmp_path, monkeypatch):
    from dra import evidence_judge as ej
    monkeypatch.setattr(ej, "judge_cards", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not judge")))
    ctx, refs, _ = setup_context(tmp_path, enabled=True)
    text, ok = ctx.save_evidence({"cards": [{"claim": "联合结论", "quotes": [
        {"source_ref": refs[0], "quote": "Uzi 属于 Team Ice。"},
        {"source_ref": refs[1], "quote": "不存在的引文"}]}]})
    row = json.loads(text)["results"][0]
    assert not ok and not row["accepted"] and not ctx.store.cards()
    assert row["quote_results"][1]["quote_index"] == 2
    assert row["quote_results"][1]["binding_status"] == "not_found"
    assert not ctx.archived


def test_context_is_snapshot_text_deduplicated_bounded_and_returned(tmp_path):
    ctx, refs, raws = setup_context(tmp_path)
    text, ok = ctx.save_evidence({"cards": [{"claim": "第一名且在巴塞罗那", "quotes": [
        {"source_ref": refs[0], "quote": "Team Ice 获得第一名。"},
        {"source_ref": refs[0], "quote": "比赛在巴塞罗那举行。"}]}]})
    assert ok, text
    spans = json.loads(text)["results"][0]["quotes"]
    ranges = []
    total = 0
    for span in spans:
        for block in [{"text": span["quote"], "location": span["location"]}, *span["context"]]:
            a, b = map(int, re.fullmatch(r"chars=(\d+)-(\d+)", block["location"]).groups())
            assert raws[0][a:b] == block["text"]
            assert not any(a < d and c < b for c, d in ranges)
            ranges.append((a, b))
        total += sum(len(c["text"]) for c in span["context"])
    assert 0 < total <= 6000
    assert any("最终成绩" in c["text"] for span in spans for c in span["context"])


def test_legacy_submission_gains_context_but_keeps_full_submitted_quote(tmp_path):
    ctx, refs, _ = setup_context(tmp_path)
    text, ok = ctx.save_evidence({"cards": [{"claim": "有 Uzi", "source_ref": refs[0], "quote": "Uzi  属于 Team Ice。"}]})
    assert ok, text
    row = json.loads(text)["results"][0]
    assert row["quotes"][0]["quote"] == "Uzi 属于 Team Ice。"
    # 回执不再重复模型提交的原文；保存的卡片仍保留它供界面展示。
    assert ctx.store.cards()[0].quotes[0].submitted_quote == "Uzi  属于 Team Ice。"
    assert row["quotes"][0]["context"]


def test_context_exposure_only_after_receipt_and_never_shared_with_other_agent(tmp_path):
    ctx, refs, raws = setup_context(tmp_path)
    quote = "Team Ice 获得第一名。"
    a = raws[0].index(quote)
    ctx.read_exposures = {refs[0]: [(a, a + len(quote))]}
    items = [{"claim": "第一名", "quotes": [{"quote": quote, "source_ref": refs[0]}]},
             {"claim": "巴塞罗那", "quotes": [{"quote": "比赛在巴塞罗那举行。", "source_ref": refs[0]}]}]
    text, ok = ctx.save_evidence({"cards": items})
    rows = json.loads(text)["results"]
    assert ok and rows[0]["accepted"] and not rows[1]["accepted"]
    assert ctx.save_evidence({"cards": [items[1]]})[1]  # 已经真实返回的补充上下文可引用。
    other = sr.ToolContext(ctx.store, ctx.config)
    other.readings = ctx.readings
    assert not other.save_evidence({"cards": [items[1]]})[1]


def test_import_multisource_context_verifies_every_span_and_remaps(tmp_path):
    from test_research_history import history_fixture, child_context
    ws, parent, ps, raw = history_fixture(tmp_path)
    src, _ = ps.write_source(RetrievedDoc(title="异地规则", snippet="", source_url="https://example.com/b",
                                         raw_content="异地备份保留 60 天。\n定期恢复。"))
    ctx = sr.ToolContext(ps, sr.SimpleConfig())
    for ref in ("s1", src.ref):
        ctx.read_source({"source_ref": ref})
    text, ok = ctx.save_evidence({"cards": [{"claim": "日志43天，异地备份60天", "quotes": [
        {"source_ref": "s1", "quote": "日志保留 43 天。"},
        {"source_ref": src.ref, "quote": "异地备份保留 60 天。"}]}]})
    assert ok, text
    saved_id = json.loads(text)["results"][0]["card_id"]
    child, _ = child_context(ws, parent)
    # 占据 s1，让跨轮映射确实产生新编号。
    child.store.write_source(RetrievedDoc(title="本轮", snippet="", source_url="https://example.com/new", raw_content="不同内容"))
    imported = child.history.import_card(1, saved_id)
    assert imported["quotes"][0]["source_ref"] != "s1"
    for span in imported["quotes"]:
        local = child.store.read_source_text(span["source_ref"])
        for block in [{"text": span["quote"], "location": span["location"]}, *span["context"]]:
            a, b = map(int, re.fullmatch(r"chars=(\d+)-(\d+)", block["location"]).groups())
            assert local[a:b] == block["text"]
    # 主引文不变，但第二来源的上下文被篡改，整张历史卡必须拒绝。
    ledger = json.loads((ws._dir(parent) / "run.json").read_text())
    old = next(c for c in ledger["cards"] if c["card_id"] == saved_id)
    old["quotes"][1]["context"][0]["text"] = "伪造上下文"
    (ws._dir(parent) / "run.json").write_text(json.dumps(ledger))
    import pytest
    from dra.research_history import HistoryError
    child, _ = child_context(ws, parent, request="context-tamper")
    with pytest.raises(HistoryError, match="上下文"):
        child.history.import_card(1, saved_id)
