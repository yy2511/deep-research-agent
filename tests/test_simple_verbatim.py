"""快照零修改（D2）：上游给什么就存什么，一个字符都不改。

这一条是"引用逐字可核"的地基——快照被改写的话，我们核的就不是原文了。

断言强度：**落盘字节 = 上游字节**（直接对拍，不需要人工判断差异出现在哪几处）。
历史证据：有损清洗曾把 `*,` 删成 `, ,`（`asyncio.create_task(coro, , name=None)`）、
把缩进抹平（现存快照缩进保留率 0/251）、把标题标记 `#` 删掉——这些都由本文件锁住。
"""

from __future__ import annotations

import hashlib
import json

from dra import simple_research as sr
from dra.text import clean_markdown

from test_simple_research import _doc, _run, _turn

# 一份"什么坏毛病都有"的上游正文：markdown 语法、缩进、关键字星号、表格、转义标识符、
# 上游过度转义（`\_`）、URL。
UPSTREAM = """# 标题 #带井号

段落里带 `*args`、`**kwargs` 与类型联合 `int | None`。

    async def wait_for(fut, timeout, *, loop=None, **kwargs):
        return fut.result()

| 参数 | 类型 |
| --- | --- |
| fut | Awaitable |

官方文档写作 pg\\_createsubscriber，链接 [文档](https://example.org/a?b=1) 与图片 ![](https://example.org/i.png)
"""


def _snapshot(tmp_path, name="s2.txt") -> str:
    return (tmp_path / "run" / "sources" / name).read_text(encoding="utf-8")


def _read_and_save(raw, tmp_path, *, find=None):
    read_args = {"source_ref": "s1"}
    if find:
        read_args["find"] = find
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", read_args)),
        _turn(("save_evidence", {"cards": [{"claim": "c", "source_ref": "s2", "excerpt_no": 1}]})),
    ]
    return _run(turns, raw=raw, run_dir=tmp_path / "run")


def test_snapshot_equals_upstream_bytes(tmp_path):
    """最强断言：落盘内容与上游正文**逐字节相同**。"""
    _result, script = _read_and_save(UPSTREAM, tmp_path)
    stored = _snapshot(tmp_path)
    assert stored == UPSTREAM, "快照必须与上游正文逐字节相同"
    # 快照自身的 sha256 也是按这份原文算的
    snapshot = script.context.store.source("s2")
    assert snapshot.content_sha256 == hashlib.sha256(UPSTREAM.encode("utf-8")).hexdigest()
    assert snapshot.raw_chars == len(UPSTREAM)


def test_lossy_cleaning_artifacts_are_gone(tmp_path):
    """历史上被清洗破坏的东西，现在原样保留。"""
    _read_and_save(UPSTREAM, tmp_path)
    stored = _snapshot(tmp_path)
    assert "*, loop=None, **kwargs" in stored, "关键字参数星号不能被删（曾变成 ', ,'）"
    assert "\n    async def wait_for" in stored, "缩进必须保留（曾被抹平）"
    assert stored.startswith("# 标题 #带井号"), "行首 # 标题标记必须保留"
    assert "| --- | --- |" in stored, "表格分隔行必须保留"
    assert "int | None" in stored, "类型联合的竖线必须保留"
    assert "pg\\_createsubscriber" in stored, "上游的过度转义原样保留（不再还原）"
    assert "[文档](https://example.org/a?b=1)" in stored, "链接语法原样保留"
    assert "![](https://example.org/i.png)" in stored, "图片语法原样保留"


def test_card_quote_is_snapshot_text_not_cleaned_text(tmp_path):
    """卡片回填的原文来自快照本身——不是清洗后的副本。"""
    result, _script = _read_and_save(UPSTREAM, tmp_path)
    stored = _snapshot(tmp_path)
    quote = result.cards[0].quote
    assert quote in stored, "引用必须能在快照里逐字找到"
    assert quote != clean_markdown(quote), "这段引用恰恰是清洗会改坏的那类文本"
    assert "**kwargs" in quote


def test_upstream_escape_makes_literal_find_fail(tmp_path):
    """已知风险（登记在案）：上游过度转义后，按"没转义"的字面去 find 会找不到。

    这是"零修改"换来的代价。届时的正确修法是把容错放进 find 的匹配层，而不是
    去改快照。这里把两边的行为都锁住，避免下次重新诊断。
    """
    # 带转义的原形能找到（命中清单里带可引用片段）
    _result, script = _read_and_save(UPSTREAM, tmp_path / "a", find="pg\\_createsubscriber")
    hit = [
        json.loads(r["payload"])
        for r in script.context.receipts
        if r["tool"] == "read_source" and r["ok"]
    ][-1]
    assert hit["view"] == "matches" and hit["occurrences"] >= 1
    assert "pg\\_createsubscriber" in hit["excerpts"][0]["quote"]

    # 不带转义的写法找不到：这是被接受的、已知的行为
    _result2, script2 = _read_and_save(UPSTREAM, tmp_path / "b", find="pg_createsubscriber")
    rejects = [r for r in script2.context.receipts if r["tool"] == "read_source" and not r["ok"]]
    assert rejects and "没有在原文中找到" in rejects[-1]["payload"]


def test_search_snippet_line_is_still_cleaned(tmp_path):
    """边界：只当线索的 snippet 仍然清洗（它不是真相源、从不参与引用）。"""
    snippet = "![图](https://x/i.png)\n\n[标题](https://x/a)\n\n正文一句。"
    turns = [_turn(("search_sources", {"query": "q"}))]
    _result, script = _run(
        turns,
        raw=UPSTREAM,
        search=lambda _q: [_doc(raw=UPSTREAM, snippet=snippet)],
        run_dir=tmp_path / "run",
    )
    payload = json.loads(script.context.receipts[0]["payload"])
    shown = payload["results"][0]["snippet"]
    assert "![" not in shown and "](https://x/a)" not in shown, "线索可以清洗"
    assert "正文一句。" in shown
