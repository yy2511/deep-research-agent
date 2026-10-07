"""归档：来源产出证据卡后，原文退出工作上下文（不是删除）。

覆盖的是真实行为，不是字段镜像：
1. 已存证的来源，其历史 read_source 回执在下一轮装配里变成一行存根；
2. 账本与磁盘不受影响（账本仍记完整回执长度，快照文件仍在）；
3. 不再主动预览未存证片段，已有阅读权限仍可用于存卡；
4. 归档后可主动重读全文或局部，新阅读回执保持至下次存证；
5. 同一来源本次还有拒收 → 不归档（模型还得看着原文改）；
6. keep 模式保留完整回执。
"""

from __future__ import annotations

import json
import pytest

from dra import simple_research as sr

from test_simple_research import _run, _turn

TAIL_MARKER = "TAIL_MARKER_UNIQUE_9F3"
# 长到能切出 ≥3 个片段（单片 ≤700 字符），且尾部标记落在最后一片。
LONG_RAW = (
    "# 归档测试来源\n\n"
    + "这是用于撑长正文的补充段落。\n\n" * 130
    + f"## 尾部\n\n{TAIL_MARKER}\n"
)
READ_CALL = ("read_source", {"source_ref": "s1"})
SAVE_FIRST = (
    "save_evidence",
    {"cards": [{"claim": "第一张", "source_ref": "s2", "excerpt_no": 1}]},
)


def _seq(*extra):
    """检索 → 精读 → 存证 →（可选的额外回合）→ 交给脚本默认收尾。"""
    return [
        _turn(("search_sources", {"query": "q"})),
        _turn(READ_CALL),
        _turn(SAVE_FIRST),
        *extra,
    ]


def _last_input(script) -> str:
    """最后一轮模型输入（此时归档已经发生）。"""
    return json.dumps(script.seen[-1], ensure_ascii=False)


def _stubs(script, turn: int = -1):
    """从某一轮输入里解析出所有归档存根（解析回执，不做字符串匹配）。

    存根 JSON 嵌在消息里会被再转义一层，直接搜 `"archived": true` 是搜不到的——
    这里按"能不能解析成带 archived 字段的对象"来判断，避免被转义骗过。
    """
    out = []
    for message in script.seen[turn]:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(message.get("content") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("archived"):
            out.append(payload)
    return out


def _tool_contents(script, turn: int = -1):
    return [
        str(m.get("content") or "")
        for m in script.seen[turn]
        if m.get("role") == "tool"
    ]


def test_saved_source_leaves_working_context():
    """归档前原文就在上下文里；归档后同一篇只剩存根。"""
    result, script = _run(
        _seq(_turn(("inspect_research", {"view": "summary"}))), raw=LONG_RAW
    )

    assert script.context.archived == {"s2"}, script.context.archived
    # 精读之后那一轮确实看得到原文（证明"归档前它在上下文里"）
    earlier = json.dumps(script.seen[2], ensure_ascii=False)
    assert TAIL_MARKER in earlier
    # 归档生效后的最后一轮：整篇原文不再出现在任何工具回执里，存根在
    last = _last_input(script)
    assert _stubs(script), "归档后应看到存根回执"
    assert all(LONG_RAW not in content for content in _tool_contents(script)), (
        "已归档来源的整篇原文不应继续留在工作上下文里"
    )
    # 后续轮次新增了存证、状态和查询，不能拿两个不同时间的整份输入比较大小。
    raw_receipt = next(m["content"] for m in script.seen[2] if m.get("role") == "tool"
                       and TAIL_MARKER in m.get("content", ""))
    assert len(json.dumps(_stubs(script)[0], ensure_ascii=False)) < len(raw_receipt)
    assert "该来源已归档" in last
    assert "【可引用来源】" in last and "s2「sqlite3_rsync 官方文档」" in last
    assert "**已归档**" in last and "【归档来源回读】" in last
    stub = _stubs(script)[0]
    assert stub["source_ref"] == "s2" and stub["url"] == "https://sqlite.org/rsync.html"
    assert 'read_source(source_ref="s2")' in stub["note"]
    assert "find=" in stub["note"] and "range=" in stub["note"]
    assert "unused_readable_count" not in stub and "unused_readable_previews" not in stub
    assert "【最近已读、可直接存证的片段】" not in last
    assert result.delivery_status == "delivered"


def test_archive_does_not_touch_ledger_or_snapshot(tmp_path):
    """归档只改"喂给模型的那一份"：账本仍记完整回执，磁盘快照仍在。"""
    result, script = _run(
        _seq(_turn(("inspect_research", {"view": "summary"}))),
        raw=LONG_RAW,
        run_dir=tmp_path / "run",
    )
    ledger = json.loads((tmp_path / "run" / "run.json").read_text(encoding="utf-8"))
    # 归档本身要留一笔：否则事后无法回答"这次到底归档了没有"
    archived = [a["payload"] for a in ledger["actions"] if a.get("action") == "context_archive"]
    assert [a["source_ref"] for a in archived] == ["s2"], archived
    assert archived[0]["source_chars"] == len(LONG_RAW)
    reads = [
        a["payload"]
        for a in ledger["actions"]
        if a.get("action") == "tool_call" and a["payload"].get("tool") == "read_source"
    ]
    assert len(reads) == 1, reads
    # 账本记的是执行时的完整回执长度，不是存根长度
    assert reads[0]["receipt_chars"] >= len(LONG_RAW), reads[0]["receipt_chars"]
    # 快照原文仍在磁盘上，且与卡片引用一致
    body = (tmp_path / "run" / "sources" / "s2.txt").read_text(encoding="utf-8")
    assert TAIL_MARKER in body
    assert result.cards[0].quote in body


def test_read_permissions_survive_archiving_without_unused_previews():
    """不重复推送未存证片段，也不会剥夺已有阅读权限。"""
    second_save = (
        "save_evidence",
        {"cards": [{"claim": "第二张", "source_ref": "s2", "excerpt_no": 2}]},
    )
    result, script = _run(
        _seq(_turn(second_save), _turn(("inspect_research", {"view": "summary"}))),
        raw=LONG_RAW,
    )
    assert _stubs(script, turn=3)
    assert all("unused_readable_previews" not in stub for stub in _stubs(script, turn=3))
    # 此前已经看过的原文仍可继续存卡。
    assert [c.claim for c in result.cards] == ["第一张", "第二张"]


@pytest.mark.parametrize("args", [
    {"source_ref": "s1"},
    {"source_ref": "s2"},
    {"url": "https://sqlite.org/rsync.html"},
])
def test_no_locator_reread_returns_original_until_next_save(args):
    """摘要编号、正文编号和 URL 都可主动重读；旧回执不恢复，新回执可再次归档。"""
    result, script = _run(
        _seq(_turn(("read_source", args)),
             _turn(("inspect_research", {"view": "summary"})),
             _turn(("save_evidence", {"cards": [{"claim": "第二张", "quote": TAIL_MARKER}]}))),
        raw=LONG_RAW,
    )
    original_stub = _stubs(script, turn=3)[0]
    for turn in (4, 5):
        assert _stubs(script, turn=turn) == [original_stub]
        reads = [json.loads(content) for content in _tool_contents(script, turn=turn)]
        full = next(p for p in reads if p.get("excerpts"))
        assert full["shown_range"] == f"chars=0-{len(LONG_RAW)}"
        assert not full["has_more"] and full["next_cursor"] is None
        assert any(TAIL_MARKER in e["quote"] for e in full["excerpts"])
    reads = [r for r in script.context.receipts if r["tool"] == "read_source"]
    assert len(reads) == 2 and all(r["ok"] for r in reads)
    assert _stubs(script)[0] == original_stub
    assert len(_stubs(script)) == 2
    assert all(LONG_RAW not in content for content in _tool_contents(script))
    assert result.cards[-1].quote == TAIL_MARKER
    assert not any("错误" in w for w in result.warnings)


def test_explicit_range_reread_still_works_after_archiving():
    """显式区间照常读回局部原文。"""
    reread = ("read_source", {"source_ref": "s2", "range": "0-200"})
    _result, script = _run(
        _seq(_turn(reread), _turn(("inspect_research", {"view": "summary"}))),
        raw=LONG_RAW,
    )
    assert "chars=0-" in _last_input(script), "显式区间必须真的读回内容"


def test_partial_rejection_prevents_archiving():
    """同一来源本次还有拒收 → 不归档（模型得看着原文把那些卡补对）。"""
    mixed = (
        "save_evidence",
        {
            "cards": [
                {"claim": "好的那张", "source_ref": "s2", "excerpt_no": 1},
                {"claim": "越界的那张", "source_ref": "s2", "excerpt_no": 99},
            ]
        },
    )
    # 注意：这一次存证**只发 mixed**（不先存一张好的），否则第一张成功就归档了。
    result, script = _run(
        [
            _turn(("search_sources", {"query": "q"})),
            _turn(READ_CALL),
            _turn(mixed),
            _turn(READ_CALL),
        ],
        raw=LONG_RAW,
    )
    assert script.context.archived == set(), "有拒收就不该归档"
    assert len(result.cards) == 1
    assert TAIL_MARKER in _last_input(script), "未归档时重读应照常拿到原文"


def test_keep_mode_preserves_saved_source_receipts():
    """keep 模式存证后仍保留原文回执。"""
    config = sr.SimpleConfig(archive_mode="keep")
    _result, script = _run(
        _seq(_turn(("inspect_research", {"view": "summary"}))), raw=LONG_RAW, config=config
    )
    assert script.context.archived == set()
    assert TAIL_MARKER in _last_input(script)
