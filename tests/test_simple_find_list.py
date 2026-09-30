"""find 给**命中清单**（地址 + 行号 + 短上下文），而不是一次一个命中。

形状是照 coding agent 的 grep 学的：先给"地址清单"，模型看完再决定读哪儿。
覆盖点：
1. 清单本身（多条命中、每条带位置/行号/可引用上下文）；
2. 有界与翻页（上限 10 条 + next_cursor，两页不重叠）；
3. 清单里的片段可直接存证；
4. 行号与正文的真实行号一致（行号是可读坐标，字符区间仍是权威坐标）；
5. 没命中时的错误文案保持不变；
6. 普通 range 回执也带行号。
"""

from __future__ import annotations

import json

from dra import simple_research as sr

from test_simple_research import Script, _doc, _turn

BODY = (
    "第一行\n"
    "第二行 needle 出现一次\n"
    "第三行\n"
    "第四行 needle 又出现\n"
    "第五行\n"
)


def _run(turns, *, raw=BODY, config=None):
    script = Script(turns)
    result = sr.run_simple_research(
        "q",
        sr.requirements_from_query("q"),
        config=config or sr.SimpleConfig(),
        model_call=script,
        search_fn=lambda _q: [_doc(raw=raw)],
        ref_resolver=script,
    )
    return result, script


def _read_receipts(script):
    return [
        json.loads(r["payload"])
        for r in script.context.receipts
        if r["tool"] == "read_source" and r["ok"]
    ]


def test_find_returns_address_list_with_lines_and_citable_context():
    """一次 find 给出全部命中的地址清单；每条上下文都能直接引用。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1", "find": "needle"})),
    ]
    _result, script = _run(turns)
    payload = _read_receipts(script)[-1]
    assert payload["view"] == "matches"
    assert payload["occurrences"] == 2
    assert payload["matches_shown"] == 2
    assert payload["next_cursor"] is None
    # 每条给出：字符位置、行号、可引用编号，且上下文里确实含命中词
    for index, row in enumerate(payload["matches"]):
        assert row["position"].startswith("chars=")
        assert row["line"] == len(BODY[: int(row["position"].split("-")[0][6:])].split("\n"))
        excerpt = payload["excerpts"][index]
        assert excerpt["excerpt_no"] == row["excerpt_no"]
        assert "needle" in excerpt["quote"]
        assert excerpt["quote"] in BODY
    # 行号落在正文的真实行上
    assert [row["line"] for row in payload["matches"]] == [2, 4]


def test_find_list_is_capped_and_paginated_without_overlap():
    """密集命中：只列前 10 条 + 游标翻页，两页不重叠、不丢内容。"""
    raw = "".join(f"第{i}行 needle\n" for i in range(1, 26))
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1", "find": "needle"})),
    ]
    _result, script = _run(turns, raw=raw)
    first = _read_receipts(script)[-1]
    assert first["occurrences"] == 25
    assert len(first["matches"]) == sr._FIND_MAX_MATCHES
    assert first["next_cursor"] is not None
    assert "note_more_matches" in first

    # 按游标翻页：拿到的都是新命中，且与第一页不重叠
    ctx = script.context
    second = json.loads(
        ctx.read_source({"source_ref": "s1", "find": "needle", "cursor": first["next_cursor"]})[0]
    )
    assert second["matches"][0]["position"] != first["matches"][0]["position"]
    first_positions = {row["position"] for row in first["matches"]}
    assert not (first_positions & {row["position"] for row in second["matches"]})
    assert all(row["line"] > first["matches"][-1]["line"] for row in second["matches"])


def test_match_context_in_the_list_can_be_saved_as_evidence():
    """清单里的上下文直接可存证——这就是"地址清单"能替代"先猜一处"的关键。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1", "find": "needle"})),
        _turn(
            (
                "save_evidence",
                {
                    "cards": [
                        {"claim": "第二行提到 needle", "source_ref": "s2", "excerpt_no": "{$excerpt:0}"}
                    ]
                },
            )
        ),
    ]
    result, _script = _run(turns)
    assert len(result.cards) == 1
    assert "needle" in result.cards[0].quote
    assert result.cards[0].quote in BODY


def test_find_without_match_keeps_the_actionable_error():
    """没命中时的文案不变（模型据此换词，而不是以为来源不可用）。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1", "find": "根本不存在的词"})),
    ]
    _result, script = _run(turns)
    receipts = [r for r in script.context.receipts if r["tool"] == "read_source"]
    assert receipts and not receipts[-1]["ok"]
    assert "没有在原文中找到该子串" in receipts[-1]["payload"]


def test_range_receipts_also_carry_line_numbers():
    """普通区间读回执也带行号：行号是全局可读坐标，字符区间是引用权威坐标。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1", "range": "0-40"})),
    ]
    _result, script = _run(turns)
    payload = _read_receipts(script)[-1]
    assert payload["excerpts"], payload
    for excerpt in payload["excerpts"]:
        start, stop = map(int, excerpt["location"][6:].split("-"))
        assert excerpt["lines"] == "%d-%d" % (
            len(BODY[:start].split("\n")),
            len(BODY[: max(start, stop - 1)].split("\n")),
        )
