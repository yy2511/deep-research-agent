"""V4 tool-loop 工具层：excerpt registry + 三个执行器。纯逻辑，零 API 成本。

fetch_page 把代码持有的原文注册成文档内序号；save_evidence 只选序号、由服务端回填 quote。
拒收路径必须逐条钉死（先读再引 / 序号归属 / 总量 cap / 空字段）。
"""
import json

import dra.nodes as nodes
import dra.toolloop as tl
from dra.models import RetrievedDoc
from dra.subagent import SubAgentConfig


def _doc(id_="d1", url="https://example.com/a", raw="固态电池将于2027年量产。产能规划为10GWh。",
         snippet="固态电池将于2027年量产"):
    return RetrievedDoc(id=id_, source_url=url, title="t", snippet=snippet,
                        raw_content=raw, published_at="2026-01-01T00:00:00+00:00")


def _registry(cap=10, docs=(), fetched=(), excerpts=None):
    r = tl.DocRegistry(max_cards_total=cap)
    for d in docs:
        r.docs[d.id] = d
    r.fetched.update(fetched)
    if excerpts:
        r.excerpts.update(excerpts)
    return r


CFG = SubAgentConfig()


def test_summarize_prompt_requires_context_complete_contiguous_excerpts():
    prompt = nodes._SUMMARIZE_SYSTEM
    assert "连续的 1-3 个完整句子" in prompt
    assert "指代对象的相邻前句" in prompt
    assert "年份、单位、样本量与统计口径" in prompt


# ── DocRegistry ──

def test_registry_add_docs_dedups_by_normalized_url():
    r = tl.DocRegistry(max_cards_total=10)
    a = _doc("d1", url="https://example.com/a?utm_source=x")
    b = _doc("d2", url="http://www.example.com/a")      # 归一化后同 URL
    c = _doc("d3", url="https://example.com/b")
    added1 = r.add_docs([a])
    added2 = r.add_docs([b, c])
    assert [d.id for d in added1] == ["d1"]
    assert [d.id for d in added2] == ["d3"]             # b 被判重
    assert set(r.docs) == {"d1", "d3"}


# ── exec_search ──

def test_search_empty_query_is_strike():
    text, ok = tl.exec_search({"query": "  "}, _registry(), CFG)
    assert not ok and text.startswith(tl._ERR_PREFIX)


def test_search_duplicate_query_is_strike(monkeypatch):
    monkeypatch.setattr(tl, "_retrieve", lambda q, c, verbose=False: [_doc()])
    r = _registry()
    _, ok1 = tl.exec_search({"query": "EV sales"}, r, CFG)
    text, ok2 = tl.exec_search({"query": "EV sales"}, r, CFG)
    assert ok1 and not ok2
    assert "已经搜过" in text


def test_search_returns_snippet_only_json(monkeypatch):
    long_snippet = "长" * 1000
    monkeypatch.setattr(tl, "_retrieve",
                        lambda q, c, verbose=False: [_doc(snippet=long_snippet)])
    r = _registry()
    text, ok = tl.exec_search({"query": "q"}, r, CFG)
    assert ok
    payload = json.loads(text)
    row = payload["results"][0]
    assert set(row) == {"doc_id", "title", "url", "snippet", "published_at"}
    assert len(row["snippet"]) <= tl._SNIPPET_CAP      # 全文绝不进 search 结果
    assert len(row["snippet"]) == 600
    assert "raw_content" not in text
    assert r.docs[row["doc_id"]].raw_content            # raw 存进 registry


def test_search_cleans_markdown_noise_before_snippet_cap(monkeypatch):
    noisy = (
        "![tracking](https://example.com/pixel.gif)\n"
        "# Useful title\n"
        "[Official report](https://example.com/report) confirms the result.\n"
        "https://example.com/very-long-tracking-url"
    )
    monkeypatch.setattr(
        tl, "_retrieve", lambda q, c, verbose=False: [_doc(snippet=noisy)],
    )

    text, ok = tl.exec_search({"query": "q"}, _registry(), CFG)

    assert ok
    snippet = json.loads(text)["results"][0]["snippet"]
    assert "tracking" not in snippet
    assert "https://" not in snippet
    assert "#" not in snippet
    assert "Useful title" in snippet
    assert "Official report confirms the result." in snippet


def test_search_infra_failure_no_strike(monkeypatch):
    def _boom(q, c, verbose=False):
        raise RuntimeError("all sources failed")
    monkeypatch.setattr(tl, "_retrieve", _boom)
    text, ok = tl.exec_search({"query": "q"}, _registry(), CFG)
    assert ok                                          # 基础设施故障不怪模型
    assert "检索失败" in text


# ── exec_fetch_page ──

def test_fetch_unknown_doc_id_is_strike():
    text, ok = tl.exec_fetch_page({"doc_id": "nope"}, _registry(), CFG, objective="o")
    assert not ok and text.startswith(tl._ERR_PREFIX)


def test_fetch_empty_doc_rejected_without_marking_fetched():
    d = _doc(raw="", snippet="")
    r = _registry(docs=[d])
    text, ok = tl.exec_fetch_page({"doc_id": "d1"}, r, CFG, objective="o")
    assert not ok and "没有可注册" in text
    assert "d1" not in r.fetched and r.excerpts["d1"] == {}


def test_fetch_small_doc_returns_text_and_marks_fetched():
    d = _doc()
    r = _registry(docs=[d])
    text, ok = tl.exec_fetch_page({"doc_id": "d1"}, r, CFG, objective="o")
    assert ok and "[excerpt_no=1]" in text and "2027" in text
    assert "d1" in r.fetched
    assert r.excerpts["d1"][1] == d.raw_content


def test_fetch_long_doc_triggers_condense(monkeypatch):
    d = _doc(raw="长" * 6001)                            # 超 _MAX_DOC_CHARS=6000
    called = []
    monkeypatch.setattr(tl, "summarize_doc",
                        lambda q, doc, **kw: called.append(q) or
                        ("【理解摘要｜不可作为 quote】压缩后的内容\n\n"
                         "【可引用原文摘录｜每条独立引用，禁止跨条拼接】\n"
                         f"- {'长' * 40}"))
    r = _registry(docs=[d])
    text, ok = tl.exec_fetch_page({"doc_id": "d1"}, r, CFG, objective="研究目标X")
    assert ok and "压缩后的内容" in text
    assert called == ["研究目标X"]                        # condense 的 query = 研究目标
    assert d.condensed and "压缩后的内容" in d.condensed  # 缓存——二次 fetch 不再调
    assert r.excerpts["d1"][1] == "长" * 40


def test_fetch_doc_at_condense_threshold_uses_source(monkeypatch):
    d = _doc(raw="长" * 6000)
    monkeypatch.setattr(
        tl,
        "summarize_doc",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("6000 字符不应触发摘要")
        ),
    )
    r = _registry(docs=[d])
    text, ok = tl.exec_fetch_page({"doc_id": "d1"}, r, CFG, objective="o")
    assert ok and "[excerpt_no=1]" in text
    assert d.condensed is None


def test_fetch_rejects_hallucinated_condensed_excerpt_and_falls_back_to_source():
    """摘要模型的坏摘录不能注册；代码回退到 raw 切片，理解摘要也不能冒充 quote。"""
    d = _doc(raw="真实原文只有这一句。")
    d.condensed = ("【理解摘要｜不可作为 quote】模型概括\n\n"
                   "【可引用原文摘录｜每条独立引用，禁止跨条拼接】\n"
                   "- 原文里不存在的模型幻觉")
    r = _registry(docs=[d])
    text, ok = tl.exec_fetch_page({"doc_id": "d1"}, r, CFG, objective="o")
    assert ok
    assert r.excerpts["d1"] == {1: "真实原文只有这一句。", 2: d.snippet}
    assert "模型概括" not in text and "模型幻觉" not in text


def test_fetch_resolves_markdown_excerpt_to_server_owned_visible_text():
    """LLM 摘录省掉 markdown 语法时，registry 保存代码从来源切回的规范可见原文。"""
    d = _doc(raw="[Reciprocal rank fusion](https://example.com) reduces latency.")
    d.condensed = ("【可引用原文摘录｜每条独立引用，禁止跨条拼接】\n"
                   "- Reciprocal rank fusion reduces latency.")
    r = _registry(docs=[d])
    text, ok = tl.exec_fetch_page({"doc_id": "d1"}, r, CFG, objective="o")
    assert ok and "[excerpt_no=1]" in text
    assert r.excerpts["d1"][1] == "Reciprocal rank fusion reduces latency."


def test_fetch_allows_more_than_two_unsaved_documents():
    """未保存的精读材料可以超过两篇，不用流程控制约束替模型做阅读决策。"""
    docs = [
        _doc("d1", url="https://example.com/1"),
        _doc("d2", url="https://example.com/2"),
        _doc("d3", url="https://example.com/3"),
    ]
    r = _registry(docs=docs)

    assert tl.exec_fetch_page({"doc_id": "d1"}, r, CFG, objective="o")[1]
    assert tl.exec_fetch_page({"doc_id": "d2"}, r, CFG, objective="o")[1]
    assert tl.exec_fetch_page({"doc_id": "d3"}, r, CFG, objective="o")[1]
    assert r.open_doc_ids == ["d1", "d2", "d3"]

    _, saved = tl.exec_save_evidence(
        {"cards": [{"claim": "c1", "doc_id": "d1", "excerpt_no": 1}]},
        r,
    )
    assert saved and r.open_doc_ids == ["d2", "d3"]


# ── exec_save_evidence ──

def test_save_before_fetch_rejected():
    d = _doc()
    r = _registry(docs=[d])                             # 注意：没 fetched
    text, ok = tl.exec_save_evidence(
        {"cards": [{"claim": "c", "excerpt_no": 1, "doc_id": "d1"}]}, r)
    assert not ok                                       # 全拒 = strike
    assert "先" in text and r.evidence == []


def test_save_unknown_excerpt_no_rejected():
    d = _doc()
    r = _registry(docs=[d], fetched=["d1"],
                  excerpts={"d1": {1: "固态电池将于2027年量产。"}})
    text, ok = tl.exec_save_evidence(
        {"cards": [{"claim": "c", "excerpt_no": 999, "doc_id": "d1"}]}, r)
    assert not ok and "不属于" in text and "可用序号：1" in text and r.evidence == []


def test_save_excerpt_no_accepted_and_server_fills_quote_and_title():
    d = _doc()
    server_quote = "固态电池将于2027年量产。产能规划为10GWh。"
    r = _registry(docs=[d], fetched=["d1"],
                  excerpts={"d1": {1: server_quote}})
    text, ok = tl.exec_save_evidence(
        {"cards": [{"claim": "固态电池 2027 量产", "excerpt_no": 1,
                    "quote": "模型即使多传一个伪造 quote 也不会被采用",
                    "doc_id": "d1"}]}, r)
    assert ok
    card = r.evidence[0]
    # 来源标题由服务端从 RetrievedDoc 复制，不让模型抄写；内部 doc_id 不进入证据卡。
    assert card.source_title == d.title
    assert card.source_url == d.source_url
    assert card.published_at == d.published_at
    assert card.support_quote == server_quote
    payload = json.loads(text)
    assert payload["results"][0] == {"index": 1, "accepted": True, "card_no": 1}


def test_save_trusts_registered_excerpt_without_second_raw_check():
    """逐字门只在注册阶段；save 阶段按文档内序号取值，不再重复做 quote-in-raw。"""
    d = _doc(raw="这里故意没有 registry 中的测试片段")
    registered = "假设已在注册阶段验收的规范原文"
    r = _registry(docs=[d], fetched=["d1"],
                  excerpts={"d1": {1: registered}})
    _, ok = tl.exec_save_evidence(
        {"cards": [{"claim": "c", "doc_id": "d1", "excerpt_no": 1}]}, r)
    assert ok and r.evidence[0].support_quote == registered


def test_save_quota_exhausted_rejected():
    d = _doc()
    r = _registry(cap=1, docs=[d], fetched=["d1"], excerpts={"d1": {
        1: "固态电池将于2027年量产。", 2: "产能规划为10GWh。"}})
    tl.exec_save_evidence(
        {"cards": [{"claim": "c1", "excerpt_no": 1, "doc_id": "d1"}]}, r)
    text, ok = tl.exec_save_evidence(
        {"cards": [{"claim": "c2", "excerpt_no": 2, "doc_id": "d1"}]}, r)
    assert not ok and "额度" in text and len(r.evidence) == 1


def test_save_batch_mixed_results():
    d = _doc()
    r = _registry(docs=[d], fetched=["d1"],
                  excerpts={"d1": {1: "固态电池将于2027年量产。"}})
    text, ok = tl.exec_save_evidence({"cards": [
        {"claim": "好卡", "excerpt_no": 1, "doc_id": "d1"},
        {"claim": "坏卡", "excerpt_no": 404, "doc_id": "d1"},
    ]}, r)
    assert ok                                           # 有一张过 = 不算 strike
    payload = json.loads(text)
    assert payload["results"][0]["accepted"] is True
    assert payload["results"][1]["accepted"] is False
    assert payload["saved_total"] == 1 and payload["remaining_quota"] == 9


def test_save_ignores_unknown_card_keys():
    """卡 dict 里未知字段（如已移除的 confidence）被容忍并忽略，不影响保存。"""
    d = _doc()
    r = _registry(docs=[d], fetched=["d1"],
                  excerpts={"d1": {1: "固态电池将于2027年量产。"}})
    _, ok = tl.exec_save_evidence(
        {"cards": [{"claim": "c", "excerpt_no": 1, "doc_id": "d1",
                    "confidence": "很高"}]}, r)
    assert ok and r.evidence[0].claim == "c"


def test_save_cards_not_list_is_strike():
    text, ok = tl.exec_save_evidence({"cards": "not a list"}, _registry())
    assert not ok and text.startswith(tl._ERR_PREFIX)


# ── excerpt 归属与工具契约 ──

def test_save_excerpt_no_is_scoped_by_doc_id():
    d1, d2 = _doc("d1"), _doc("d2", url="https://example.com/b")
    r = _registry(docs=[d1, d2], fetched=["d1", "d2"], excerpts={
        "d1": {1: "文档一的原文"},
        "d2": {1: "文档二的原文"},
    })
    _, ok = tl.exec_save_evidence(
        {"cards": [{"claim": "c", "doc_id": "d1", "excerpt_no": 1}]}, r)
    assert ok and r.evidence[0].support_quote == "文档一的原文"


def test_save_legacy_excerpt_id_is_rejected():
    d = _doc()
    r = _registry(docs=[d], fetched=["d1"], excerpts={"d1": {1: "原文"}})
    text, ok = tl.exec_save_evidence(
        {"cards": [{"claim": "c", "doc_id": "d1", "excerpt_id": "d1:q1"}]}, r)
    assert not ok and "excerpt_no" in text and r.evidence == []


def test_save_tool_schema_requires_excerpt_no_not_quote_or_legacy_id():
    schema = next(s for s in tl.TOOL_SCHEMAS
                  if s["function"]["name"] == "save_evidence")["function"]
    item = schema["parameters"]["properties"]["cards"]["items"]
    assert item["required"] == ["claim", "doc_id", "excerpt_no"]
    assert item["properties"]["excerpt_no"]["type"] == "integer"
    assert "excerpt_id" not in item["properties"]
    assert "quote" not in item["properties"]
    assert "自动回填" in schema["description"]
    assert "自包含" in item["properties"]["claim"]["description"]


def test_save_tool_schema_has_no_confidence():
    """confidence 已移除：schema 不再暴露该参数（无校准打分只会扭曲下游排序）。"""
    schema = next(s for s in tl.TOOL_SCHEMAS
                  if s["function"]["name"] == "save_evidence")["function"]
    item = schema["parameters"]["properties"]["cards"]["items"]
    assert "confidence" not in item["properties"]


# ── _build_loop_messages：建议起点仅首轮 ──

def test_start_hint_injected_only_first_round():
    """【建议起点 query】只在 n_calls==0 注入；搜过后它出现在已检索 query 里，不再重发。"""
    base = [{"role": "user", "content": "【研究目标】目标"}]
    r = _registry()
    first = tl._build_loop_messages(
        base, r, n_calls=0, strikes=0, config=CFG,
        recent_exchange=[], recent_open_doc_ids=set(), start_hint="起点Q1",
    )
    assert any(
        "【建议起点 query】起点Q1" in m.get("content", "")
        for m in first if m.get("role") == "user"
    )
    second = tl._build_loop_messages(
        base, r, n_calls=1, strikes=0, config=CFG,
        recent_exchange=[], recent_open_doc_ids=set(), start_hint="起点Q1",
    )
    assert not any(
        "【建议起点 query】" in m.get("content", "")
        for m in second if m.get("role") == "user"
    )
