"""阅读工具的 find / range 参数按模型的常见写法来理解。

模型常用 `A|B|C` 表示“任选其一”（写代码时的习惯），旧实现把它当成一整串去找，必然找不到。
这里锁住修复后的行为，并覆盖同类“悄悄出错”的情况：

1. `find` 支持 `a|b|c` 多候选（逐字匹配，合并成一张清单）；
2. 全部候选按原样找不到时按**不区分大小写**重试，并如实说明；
3. 仍然不支持正则——不命中时给出可操作的说明，而不是一句"没找到"；
4. `range` 支持按行读（`L120-180`），因为行号是我们自己给出去的坐标；
5. 非法行号 / 非法 range 明确拒收；
6. `inspect_research` 的未知 view 不再静默降级成 summary；
7. 展示层也带行号（否则 UI 与账本看不到）。
"""

from __future__ import annotations

import json

from dra import simple_research as sr

from test_simple_research import _doc


def _ctx(tmp_path, raw):
    store = sr.RunStore(tmp_path / "s", run_id="f", query="q")
    ctx = sr.ToolContext(store, sr.SimpleConfig(), search_fn=lambda q: [_doc(raw=raw)])
    ctx.search_sources({"query": "x"})
    return store, ctx, store.state["sources"][0]["ref"]


TEXT = (
    "第一行：JSONB 与 jsonb 是同一个类型。\n"
    "第二行：ON DUPLICATE KEY UPDATE 是 MySQL 语法。\n"
    "第三行：ON CONFLICT DO NOTHING 是 PostgreSQL 语法。\n"
    "第四行：收尾。\n"
)


def test_find_accepts_a_or_style_candidates(tmp_path):
    """`A|B|C` 按多候选处理：命中合并成一张清单，按位置排序。"""
    _store, ctx, ref = _ctx(tmp_path, TEXT)
    text, ok = ctx.read_source({"source_ref": ref, "find": "ON DUPLICATE|ON CONFLICT"})
    assert ok, text
    payload = json.loads(text)
    assert payload["view"] == "matches"
    assert payload["searched"] == ["ON DUPLICATE", "ON CONFLICT"]
    assert payload["occurrences"] == 2
    lines = [row["line"] for row in payload["matches"]]
    assert lines == sorted(lines), "多候选的命中要按位置排序"
    assert lines == [2, 3]


def test_find_falls_back_to_case_insensitive_and_says_so(tmp_path):
    """大小写不同时按不区分大小写再试一次，并在回执里说明。"""
    _store, ctx, ref = _ctx(tmp_path, TEXT)
    text, ok = ctx.read_source({"source_ref": ref, "find": "on duplicate"})
    assert ok, text
    payload = json.loads(text)
    assert payload["occurrences"] == 1
    assert "note_case_insensitive" in payload
    assert "ON DUPLICATE" in payload["excerpts"][0]["quote"]


def test_regex_style_pattern_is_reported_as_unsupported_not_silent(tmp_path):
    """`a|b` 之外的正则语法仍然不支持——但要说清楚，而不是让模型继续猜。"""
    _store, ctx, ref = _ctx(tmp_path, TEXT)
    text, ok = ctx.read_source({"source_ref": ref, "find": "JSON_(TABLE|OBJECT)"})
    assert not ok
    assert "逐字" in text and "a|b|c" in text and "不支持正则" in text


def test_range_can_be_given_as_line_numbers(tmp_path):
    """`L2-3` 按行读：行号是我们自己给出去的坐标，就该能按它读回来。"""
    _store, ctx, ref = _ctx(tmp_path, TEXT)
    text, ok = ctx.read_source({"source_ref": ref, "range": "L2-3"})
    assert ok, text
    payload = json.loads(text)
    assert "ON DUPLICATE" in json.dumps(payload, ensure_ascii=False)
    assert "第一行" not in json.dumps(payload, ensure_ascii=False), "不该把第一行也算进来"
    assert payload["excerpts"], payload
    for row in payload["excerpts"]:
        assert row["lines"].startswith(("2", "3")), row["lines"]


def test_line_range_out_of_bounds_is_rejected_clearly(tmp_path):
    """越界行号明确拒收，并告诉它这份快照有多少行（而不是静默读到别处）。"""
    _store, ctx, ref = _ctx(tmp_path, TEXT)
    text, ok = ctx.read_source({"source_ref": ref, "range": "L99-120"})
    assert not ok
    assert "超出范围" in text and "行" in text
    text2, ok2 = ctx.read_source({"source_ref": ref, "range": "L0-2"})
    assert not ok2 and "1 起" in text2


def test_bad_range_shape_still_rejected_before_fetch(tmp_path):
    _store, ctx, ref = _ctx(tmp_path, TEXT)
    text, ok = ctx.read_source({"source_ref": ref, "range": "Lx-3"})
    assert not ok and "L120-180" in text


def test_inspect_research_rejects_unknown_view(tmp_path):
    """未知 view 不再静默返回 summary——那会让"我要了卡片清单"变成查不出的错觉。"""
    _store, ctx, _ref = _ctx(tmp_path, TEXT)
    text, ok = ctx.inspect_research({"view": "all"})
    assert not ok
    assert "summary" in text and "sources" in text and "cards" in text
    # 合法 view 照常
    for view in ("summary", "sources", "cards"):
        _t, ok2 = ctx.inspect_research({"view": view})
        assert ok2, view


def test_display_projection_carries_line_numbers():
    """展示层也要有行号：否则模型看得到、UI 与账本看不到（可观测性缺口）。"""
    payload = {
        "source_ref": "s1",
        "view": "matches",
        "find": "needle",
        "searched": ["needle"],
        "occurrences": 1,
        "matches_shown": 1,
        "excerpts": [
            {"excerpt_no": 3, "location": "chars=0-20", "lines": "2-2", "quote": "needle 在此"}
        ],
        "matches": [{"position": "chars=0-6", "line": 2, "excerpt_no": 3}],
    }
    out = sr._display_payload("read_source", {"source_ref": "s1", "find": "needle"}, json.dumps(payload), 1)
    assert out["view"] == "matches"
    assert out["excerpts"][0]["lines"] == "2-2"
    assert out["matches"][0]["line"] == 2
