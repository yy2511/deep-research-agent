"""研究上下文：多轮对话的内容保留、find 的后续匹配信息、截断提示。

只替换外部边界；核心状态机与消息装配全部真实。多交换保留不改引用授权：
quote 仍只来自已读 span（见 test_authorization_unchanged）。
"""

import json

from dra import simple_research as sr
from dra.research_input import ResearchInput
from test_simple_research import Script, _doc, _turn

def test_early_read_and_user_message_survive_past_former_limits(tmp_path):
    """超过旧六轮／24000 字符阈值仍保留早期已读材料和用户消息，工具交换完整。"""
    marker = "EARLY_FULL_RECEIPT_SENTINEL"
    raw = marker + " visible body " * 2200 + "UNREAD_SECRET_TAIL"
    message = "EARLY_USER_REQUEST: keep the original scope"
    inbox = ResearchInput("retention", lambda _: None)
    inbox.accept("u1", message)
    script = Script([
        _turn(("search_sources", {"query": "retention"})),
        _turn(("read_source", {"source_ref": "s1", "range": "0-26000"}),
              ("read_source", {"source_ref": "s1", "range": "0-700"})),
        *[_turn(("inspect_research", {"view": "sources"})) for _ in range(8)],
    ])
    sr.run_simple_research("retention", config=sr.SimpleConfig(archive_mode="keep"),
        run_dir=tmp_path / "run", user_input=inbox, model_call=script,
        search_fn=lambda _: [_doc(raw=raw)], ref_resolver=script)
    messages = script.seen[-1]
    body = json.dumps(messages, ensure_ascii=False)
    assert len(body) > 24000 and len(script.seen) > 6
    assert marker in body and message in body
    assert "UNREAD_SECRET_TAIL" not in body, "保留历史不扩大已读范围"
    pending = set()
    for item in messages:
        if item["role"] == "assistant":
            assert not pending, "assistant 交换丢失工具回执"
            pending = {call["id"] for call in item.get("tool_calls", [])}
        elif item["role"] == "tool":
            assert item["tool_call_id"] in pending, "出现孤儿工具回执"
            pending.remove(item["tool_call_id"])
    assert not pending


# ---------------------------------------------------------------------------
# find 后续匹配元信息与截断提示
# ---------------------------------------------------------------------------


def _tool_ctx(tmp_path, raw, search_fn=None):
    store = sr.RunStore(tmp_path / "s", run_id="f", query="q")
    ctx = sr.ToolContext(
        store, sr.SimpleConfig(), search_fn=search_fn or (lambda q: [_doc(raw=raw)])
    )
    return store, ctx


def test_find_reports_occurrences_and_next_match(tmp_path):
    raw = "前置。\n\nneedle 第一次。\n\n中间。\n\nneedle 第二次。\n\n收尾。"
    store, ctx = _tool_ctx(tmp_path, raw)
    ctx.search_sources({"query": "x"})
    ref = store.state["sources"][0]["ref"]
    text, ok = ctx.read_source({"source_ref": ref, "find": "needle"})
    assert ok, text
    payload = json.loads(text)
    first = raw.find("needle")
    second = raw.find("needle", first + 1)
    assert payload["view"] == "matches"
    assert payload["occurrences"] == 2
    assert payload["matches"][0]["position"] == f"chars={first}-{first + 6}"
    assert payload["matches"][0]["line"] == raw[:first].count("\n") + 1
    assert payload["matches"][0]["window_seen_before"] is False
    # 命中清单自带短上下文（可引用），不是整篇
    assert payload["excerpts"][0]["quote"] == "needle 第一次。"
    assert "needle 第二次" not in payload["excerpts"][0]["quote"]
    # find+cursor 从显式偏移搜后续出现：拿到第二次命中。
    text, ok = ctx.read_source({"source_ref": ref, "find": "needle", "cursor": second})
    assert ok, text
    payload = json.loads(text)
    assert payload["matches"][0]["position"] == f"chars={second}-{second + 6}"
    assert payload["next_cursor"] is None
    # 同一查询重复稳定：无 cursor 的 find 仍返回首个匹配与同一窗口。
    text, ok = ctx.read_source({"source_ref": ref, "find": "needle"})
    payload = json.loads(text)
    assert payload["matches"][0]["position"] == f"chars={first}-{first + 6}"
    assert payload["matches"][0]["window_seen_before"] is True


def test_find_invalid_cursor_rejected_before_fetch(tmp_path):
    calls = []

    def search(query):
        calls.append(query)
        return [_doc(raw="")]  # 只有摘要：一旦抓取正文会再次调用 search_fn(url)

    store, ctx = _tool_ctx(tmp_path, "", search_fn=search)
    ctx.search_sources({"query": "x"})
    ref = store.state["sources"][0]["ref"]
    assert len(calls) == 1
    for bad in (-1, "3", True, 1.5):
        text, ok = ctx.read_source({"source_ref": ref, "find": "needle", "cursor": bad})
        assert not ok and "cursor" in text, (bad, text)
        assert len(calls) == 1, "不合法 cursor 不得先抓取正文再拒收"


def test_window_tail_truncation_hint(tmp_path):
    # 3000 字符无标点：2000 窗口边界落在词中。
    store, ctx = _tool_ctx(tmp_path, "AB" * 1500)
    ctx.search_sources({"query": "x"})
    ref = store.state["sources"][0]["ref"]
    text, ok = ctx.read_source({"source_ref": ref, "range": "0-2000"})
    assert ok
    payload = json.loads(text)
    assert payload["has_more"] is True
    assert "note_truncated" in payload and "续读" in payload["note_truncated"]

    # 对照：窗口边界落在句末标点，不提示截断。
    raw2 = "x" * 1999 + "。\n" + "y" * 500
    store2, ctx2 = _tool_ctx(tmp_path, raw2)
    ctx2.search_sources({"query": "x"})
    ref2 = store2.state["sources"][0]["ref"]
    text, ok = ctx2.read_source({"source_ref": ref2, "range": "0-2000"})
    assert ok
    payload = json.loads(text)
    assert "note_truncated" not in payload


def test_authorization_unchanged_after_retention(tmp_path):
    """多交换保留不扩大引用授权：未读片段的编号仍然拒收。"""
    store, ctx = _tool_ctx(tmp_path, "AB" * 1500)
    ctx.search_sources({"query": "x"})
    ref = store.state["sources"][0]["ref"]
    ctx.read_source({"source_ref": ref, "range": "0-700"})
    for _ in range(8):  # 多次读取制造长历史
        ctx.read_source({"source_ref": ref, "range": "700-1400"})
    text, ok = ctx.save_evidence(
        {"cards": [{"claim": "未读编号", "source_ref": ref, "excerpt_no": 99}]}
    )
    assert not ok and "已读范围" in text
    assert store.cards() == []
