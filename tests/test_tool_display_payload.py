"""工具卡展示字段：走**真实工具执行与 emit 边界**的回归。

这里替换的只有外部检索边界，工具执行（search/read/save）、账本、事件 emit 都是真实代码。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT / "src"), str(_ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dra import events, llm, simple_research as sr  # noqa: E402
from dra.models import RetrievedDoc  # noqa: E402

from test_budget_wrapup import RAW  # noqa: E402

QUERY = "sqlite3_rsync 同机两个文件也能同步吗？"

def _doc():
    return RetrievedDoc(
        id="d1", source_url="https://sqlite.org/rsync.html", title="sqlite3_rsync",
        # 真实回执比展示上限长得多：搜索片段与阅读原文都远超 200 字符
        snippet="sqlite3_rsync 通过 SSH 同步两个数据库文件。" * 40,
        raw_content=RAW, published_at="2025-11-13",
    )


def _turn(*calls):
    from dra.llm import ToolCall, ToolTurn

    tcs = [ToolCall(id=f"c{i}", name=name, arguments_raw=json.dumps(args, ensure_ascii=False),
                    arguments=args) for i, (name, args) in enumerate(calls)]
    message = {"role": "assistant", "content": ""}
    if tcs:
        message["tool_calls"] = [
            {"id": t.id, "type": "function",
             "function": {"name": t.name, "arguments": t.arguments_raw}}
            for t in tcs
        ]
    return ToolTurn(content="", tool_calls=tcs, assistant_message=message)


class Script:
    """按序吐回合；用完返回空文本（收尾形态）。"""

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen: list[list[dict]] = []

    def __call__(self, messages, **kwargs):
        self.seen.append(messages)
        index = len(self.seen) - 1
        return self.turns[index] if index < len(self.turns) else _turn()

    def resolve(self, args):
        return args


SEQUENCE = [
    _turn(("search_sources", {"query": "sqlite3_rsync ssh local"})),
    _turn(("read_source", {"source_ref": "s1"})),
    _turn(("save_evidence", {"cards": [
        {"claim": "同机两文件可同步", "source_ref": "s1", "excerpt_no": 1}]})),
    _turn(("finish_research", {"draft": "不要求一端为远程 [c1]。"})),
]


@pytest.fixture
def emitted(monkeypatch, tmp_path):
    """跑一次真实 Lead 循环，收集真实 emit 出来的工具事件。"""
    captured: list[dict] = []
    script = Script(SEQUENCE)
    config = sr.SimpleConfig(max_llm_calls=8, max_tool_calls=8)
    store = sr.RunStore(tmp_path / "run", run_id="display-run", query=QUERY)

    def search(query, cfg, **kw):
        return [_doc()]

    monkeypatch.setattr(sr, "_retrieve", search)
    subscription = events.subscribe(captured.append)
    try:
        result = sr.run_lead_loop(
            store, config, sr.requirements_from_query(QUERY),
            budget=sr.Budget(config, None), context=sr.ToolContext(store, config),
            model_call=script,
        )
    finally:
        events.unsubscribe(subscription)
    assert result["stop_reason"] == "finished", result["stop_reason"]
    return captured


def _done(events_: list[dict], tool: str) -> dict:
    return next(e for e in events_
                if e["type"] == "subagent_tool_call" and e["phase"] == "done"
                and e["tool"] == tool)


def test_search_emits_structured_display_from_full_receipt(emitted):
    """真实工具执行路径上，展示字段带真实标题/URL/片段，且每项都有界。"""
    display = _done(emitted, "search_sources")["display"]
    assert display["total"] == 1 and display["shown"] == 1
    row = display["results"][0]
    assert row["source_ref"] == "s1"
    assert row["title"] == "sqlite3_rsync"
    assert row["url"] == "https://sqlite.org/rsync.html"
    assert 0 < len(row["snippet"]) <= 200        # 真实片段被截到上限
    # 事件体积仍受控：展示字段不会把整篇正文塞进事件
    assert len(json.dumps(display, ensure_ascii=False)) < 1200


def test_read_emits_range_total_and_bounded_excerpt(emitted):
    display = _done(emitted, "read_source")["display"]
    # 摘要来源 s1 读取时落盘为全文快照 s2（真实路径行为，不是测试造出的编号）
    assert display["source_ref"] in {"s1", "s2"}
    assert display["url"] == "https://sqlite.org/rsync.html"
    assert display["shown_range"].startswith("chars=")
    # 落盘快照是清洗后的正文，长度与原始回执略有差异：只要求是真实数字
    assert display["total_chars"] > 0
    assert isinstance(display["has_more"], bool)
    assert display["excerpts"] and display["excerpts"][0]["excerpt_no"] == 1
    assert 0 < len(display["excerpts"][0]["quote"]) <= 200
    assert display["excerpt_total"] >= 1


def test_save_emits_card_ids_and_rejection_reasons(emitted):
    display = _done(emitted, "save_evidence")["display"]
    assert display["accepted"] == 1 and display["rejected"] == 0
    assert display["card_ids"] == ["c1"]
    assert display["submitted"] == 1
    assert display["saved_total"] == 1


def test_running_event_has_empty_display(emitted):
    """执行开始阶段没有回执：展示字段为空，不能拿参数猜结果。"""
    running = next(e for e in emitted
                   if e["type"] == "subagent_tool_call" and e["phase"] == "running")
    assert running["display"] == {}
    assert running["status"] == "running"
