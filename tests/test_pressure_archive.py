"""archive_mode 三态配置契约与"满了再压"行为。

配置部分覆盖：
1. 默认立即压，产品默认行为不变；
2. pressure 必须显式给容量，各参数有界、低水位不得超过触发线。

行为部分覆盖 PLAN §7 的离线验收（先红后绿）：压力未到不改写前缀、越线一批收起且不重复、
同源新旧回执区别保护、拒收/待选未了结不收、无候选如实记录、MCP 嵌套只换选中来源块、
主子隔离、显式重读保留至下次成功存证、用量锚定估算依据。
"""
import json

import pytest

from dra import simple_research as sr
from dra.models import RetrievedDoc


def test_default_mode_is_immediate():
    config = sr.SimpleConfig()
    assert config.archive_mode == "immediate"


def test_pressure_requires_explicit_capacity():
    with pytest.raises(ValueError):
        sr.SimpleConfig(archive_mode="pressure")
    config = sr.SimpleConfig(archive_mode="pressure", context_window_tokens=64000)
    assert config.archive_mode == "pressure"
    assert config.context_window_tokens == 64000


def test_pressure_params_bounded():
    for bad in (
        {"pressure_trigger_ratio": 0.0},
        {"pressure_trigger_ratio": 1.5},
        {"pressure_low_ratio": 0.0},
        {"pressure_low_ratio": 1.5},
        {"pressure_low_ratio": 0.9, "pressure_trigger_ratio": 0.8},  # 低水位不得超过触发线
        {"pressure_protect_recent_reads": -1},
        {"pressure_reserve_tokens": -1},
        {"estimate_chars_per_token": 0.0},
    ):
        with pytest.raises(ValueError):
            sr.SimpleConfig(archive_mode="pressure", context_window_tokens=64000, **bad)
    with pytest.raises(ValueError):
        sr.SimpleConfig(archive_mode="pressure", context_window_tokens=0)
    # 非 pressure 模式下容量可空；但给了就必须合法
    assert sr.SimpleConfig().context_window_tokens is None
    with pytest.raises(ValueError):
        sr.SimpleConfig(context_window_tokens=0)


# ---------------------------------------------------------------------------
# "满了再压"行为：达到触发线后批量收起，同时保护最近阅读。

RAW_A = "Alpha fact one.\n\n" + "Beta condition two. " * 60 + "\n\nGamma exception three."
RAW_B = "Delta finding four.\n\n" + "Epsilon limit five. " * 60 + "\n\nZeta note six."
RAW_C = "Ceta long material for growth. " * 100


def _exchange(name, content, call_id="call"):
    return [{"role": "assistant", "content": "Read.", "reasoning_content": "recorded reasoning",
             "tool_calls": [{"id": call_id, "type": "function",
                             "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": call_id, "content": content}]


def _save(ctx, source_ref, excerpt_no, claim):
    payload, ok = ctx.save_evidence(
        {"cards": [{"claim": claim, "source_ref": source_ref, "excerpt_no": excerpt_no}]})
    assert ok
    return payload


def _build(ctx, history):
    messages = sr._build_messages(
        [{"role": "system", "content": "fixed rules"}, {"role": "user", "content": "goal"}],
        ctx, [], history, seconds_left=None)
    if ctx.config.compaction_enabled:
        from dra.context_compaction import compact_context
        # This suite isolates archive-only behavior with no summary call budget.
        budget = sr.Budget(ctx.config.model_copy(update={"max_llm_calls": 1}), None)
        messages = compact_context(ctx, messages, [], budget)
    return messages


def _pressure_ctx(tmp_path, **overrides):
    params = dict(
        archive_mode="pressure", context_window_tokens=10 ** 6, pressure_reserve_tokens=0,
        pressure_protect_recent_reads=0, estimate_chars_per_token=1.0,
        condense_over_chars=None)
    params.update(overrides)
    return sr.ToolContext(sr.RunStore(tmp_path, run_id="pressure", query="goal"),
                          sr.SimpleConfig(**params))


def _write(ctx, raw, title):
    ctx.store.write_source(RetrievedDoc(title=title, source_url=f"https://example.com/{title}",
                                        snippet="", raw_content=raw))


def _read(ctx, ref):
    receipt, ok = ctx.read_source({"source_ref": ref})
    assert ok
    return receipt


def _fit_window(ctx, history):
    """把窗口调到"当前请求恰好越过触发线"（estimate_chars_per_token=1 → 1 字 ≈ 1 token）。"""
    chars = sr._request_chars(_build(ctx, history))
    w = max(1, int(chars / ctx.config.pressure_trigger_ratio) - 2)
    ctx.config = ctx.config.model_copy(update={"context_window_tokens": w})


def _actions(ctx, name):
    return [a["payload"] for a in ctx.store.state["actions"] if a["action"] == name]


def _tool_ids(messages):
    return [(m.get("tool_call_id"), tuple(c["id"] for c in m.get("tool_calls") or []))
            for m in messages if m.get("tool_call_id") or m.get("tool_calls")]


def _receipt_of(messages, call_id):
    return next(json.loads(m["content"]) for m in messages if m.get("tool_call_id") == call_id)


def _visible_chars(payload):
    """回执正文区仍在的字数；存根没有 excerpts 正文（计数按结构字段，不做字符串搜索）。"""
    rows = payload.get("sources") if isinstance(payload.get("sources"), list) else [payload]
    return sum(len(e.get("quote") or "") for row in rows for e in (row.get("excerpts") or []))


def _receipts_archived(messages):
    out = []
    for m in messages:
        if m.get("role") != "tool":
            continue
        try:
            payload = json.loads(m.get("content") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("archived"):
            out.append(payload)
    return out


@pytest.mark.parametrize("mode", ["pressure", "compaction"])
def test_pressure_keeps_full_text_below_trigger(tmp_path, mode):
    """压力未到：新增工具交换、存证都不改写已有前缀，全文一直可见。"""
    ctx = _pressure_ctx(tmp_path, **({"archive_mode": "immediate", "compaction_enabled": True} if mode == "compaction" else {}))
    _write(ctx, RAW_A, "a")
    history = [
        _exchange("read_source", _read(ctx, "s1"), "read1"),
        _exchange("save_evidence", _save(ctx, "s1", 1, "第一张"), "save1"),
    ]
    a = _build(ctx, history)
    history.append(_exchange("inspect_research", "{}", "more"))
    b = _build(ctx, history)
    assert b[:len(a)] == a
    assert _visible_chars(_receipt_of(b, "read1")) > 1000  # 全文一直可见
    assert not _actions(ctx, "context_archive") and not _actions(ctx, "context_pressure_batch")


def test_compaction_archive_pairs_reused_tool_ids_by_turn(tmp_path):
    ctx = _pressure_ctx(tmp_path, compaction_enabled=True)
    _write(ctx, RAW_A, 'a')
    read = _read(ctx, 's1')
    saved = _save(ctx, 's1', 1, 'fact')
    history = [_exchange('read_source', read, 'same-id'),
               _exchange('save_evidence', saved, 'same-id')]
    _fit_window(ctx, history)
    after = _build(ctx, history)
    replies = [json.loads(m['content']) for m in after if m.get('role') == 'tool']
    assert replies[0]['source_ref'] == 's1' and replies[0]['archived']
    assert replies[1] == json.loads(saved)


@pytest.mark.parametrize("mode", ["pressure", "compaction"])
def test_pressure_batch_collapses_once_records_events_and_freezes(tmp_path, mode):
    """越线一批收起旧阅读回执：存证引文与协议配对不动，事件齐全，不重复处理。"""
    ctx = _pressure_ctx(tmp_path, **({"archive_mode": "immediate", "compaction_enabled": True} if mode == "compaction" else {}))
    _write(ctx, RAW_A, "a")
    _write(ctx, RAW_B, "b")
    history = [
        _exchange("read_source", _read(ctx, "s1"), "read1"),
        _exchange("read_source", _read(ctx, "s2"), "read2"),
        _exchange("save_evidence", _save(ctx, "s1", 1, "第一张"), "save1"),
        _exchange("save_evidence", _save(ctx, "s2", 1, "第二张"), "save2"),
    ]
    before = _build(ctx, history)   # 大窗口：不触发
    _fit_window(ctx, history)       # 定标（这次构建仍按旧窗口渲染）
    after = _build(ctx, history)    # 越线：这一批收起，返回已收起的渲染
    after = _build(ctx, history)    # 状态行跟随"已归档"更新；此后渲染稳定
    assert _visible_chars(_receipt_of(after, "read1")) < 200  # 全文退出
    assert _visible_chars(_receipt_of(after, "read2")) < 200
    assert {s["source_ref"] for s in _receipts_archived(after)} == {"s1", "s2"}
    # 整理边界可收起较旧的成功存证副本；新回执仍完整，卡库没有改写。
    save_receipt = next(m["content"] for m in after if m.get("tool_call_id") == "save1")
    assert "第一张" in save_receipt
    if mode == "compaction":
        assert json.loads(save_receipt)["results"][0]["read_back"]["card_id"] == "c1"
    else:
        assert "chars=" in save_receipt
    assert "quotes" in _receipt_of(after, "save2")["results"][0]
    # 工具调用/回执配对完整（消息条数与 id 一一对应）
    assert _tool_ids(before) == _tool_ids(after)
    # 事件：一批摘要 + 每来源一笔，触发原因、估算依据与真实字符变化齐全
    batches = _actions(ctx, "context_pressure_batch")
    assert len(batches) == 1
    assert batches[0]["basis"] == "chars_estimated"
    assert batches[0]["collapsed_refs"] == ["s1", "s2"]
    assert batches[0]["trigger_tokens"] and batches[0]["target_tokens"]
    assert batches[0]["real_chars_removed"] > 0
    archived = _actions(ctx, "context_archive")
    assert {a["source_ref"] for a in archived} == {"s1", "s2"}
    assert all(a["trigger"] == "context_pressure" for a in archived)
    # 一批之后不重复处理；同一输入重试渲染稳定（存根冻结）
    again = _build(ctx, history)
    assert again == after
    assert len(_actions(ctx, "context_pressure_batch")) == 1


def test_pressure_second_batch_on_growth(tmp_path):
    """达到目标线后不重复；继续增长能触发下一批，且不重选已收完的来源。"""
    ctx = _pressure_ctx(tmp_path)
    _write(ctx, RAW_A, "a")
    history = [
        _exchange("read_source", _read(ctx, "s1"), "read1"),
        _exchange("save_evidence", _save(ctx, "s1", 1, "第一张"), "save1"),
    ]
    _fit_window(ctx, history)
    _build(ctx, history)
    assert [b["collapsed_refs"] for b in _actions(ctx, "context_pressure_batch")] == [["s1"]]
    # 继续研究、继续增长：新来源读+存，触发下一批
    _write(ctx, RAW_C, "c")
    history += [
        _exchange("read_source", _read(ctx, "s2"), "read2"),
        _exchange("save_evidence", _save(ctx, "s2", 1, "第二张"), "save2"),
    ]
    _build(ctx, history)
    batches = _actions(ctx, "context_pressure_batch")
    assert [b["batch_no"] for b in batches] == [1, 2]
    assert batches[1]["collapsed_refs"] == ["s2"]


@pytest.mark.parametrize("mode", ["pressure", "compaction"])
def test_pressure_recent_receipts_and_rereads_protected(tmp_path, mode):
    """同源新旧回执区别保护（最近 1 条不收）；重读正文保留至下一次成功存证。"""
    ctx = _pressure_ctx(tmp_path, pressure_protect_recent_reads=1,
                        **({"archive_mode": "immediate", "compaction_enabled": True} if mode == "compaction" else {}))
    _write(ctx, RAW_A, "a")
    first = _read(ctx, "s1")
    second = ctx.read_source({"source_ref": "s1", "range": "0-800"})[0]
    history = [
        _exchange("read_source", first, "read1"),
        _exchange("read_source", second, "read2"),
        _exchange("save_evidence", _save(ctx, "s1", 1, "第一张"), "save1"),
    ]
    _fit_window(ctx, history)
    after = _build(ctx, history)
    assert _receipt_of(after, "read1").get("archived") is True   # 旧回执收起
    r2 = _receipt_of(after, "read2")
    assert r2.get("archived") is None and _visible_chars(r2) > 300  # 最近 1 条受保护
    # 给后续交互留够容量，本测试只验证归档保护；容量不足的收尾另有测试。
    if mode == "compaction":
        ctx.config.context_window_tokens = 1000000
    # 不带定位参数也能重读全文，读回原文在下次成功存证前保持可见。
    third = ctx.read_source({"source_ref": "s1"})[0]
    assert json.loads(third)["shown_range"] == f"chars=0-{len(RAW_A)}"
    history.append(_exchange("read_source", third, "read3"))
    b = _build(ctx, history)
    r3 = _receipt_of(b, "read3")
    assert r3.get("archived") is None and _visible_chars(r3) > 300
    # 存证只恢复候选资格，最近阅读仍受保护；直到出现更近期的阅读。
    history.append(_exchange("save_evidence", _save(ctx, "s1", 2, "第二张"), "save2"))
    d = _build(ctx, history)
    assert not _receipt_of(d, "read3").get("archived")
    history.append(_exchange("read_source", ctx.read_source({"source_ref": "s1", "range": "0-100"})[0], "read4"))
    _fit_window(ctx, history)
    e = _build(ctx, history)
    assert _receipt_of(e, "read3").get("archived") is True
    assert not _receipt_of(e, "read4").get("archived")


@pytest.mark.parametrize("mode", ["pressure", "compaction"])
def test_pressure_unsettled_sources_protect_and_unresolved_recorded(tmp_path, mode):
    """同源拒收未了结不收；无候选时如实记录压力与原因，不偷删。"""
    ctx = _pressure_ctx(tmp_path, **({"archive_mode": "immediate", "compaction_enabled": True} if mode == "compaction" else {}))
    _write(ctx, RAW_A, "a")
    read = _exchange("read_source", _read(ctx, "s1"), "read1")
    mixed, ok = ctx.save_evidence({"cards": [
        {"claim": "好的那张", "source_ref": "s1", "excerpt_no": 1},
        {"claim": "越界的那张", "source_ref": "s1", "excerpt_no": 99}]})
    assert ok
    history = [read, _exchange("save_evidence", mixed, "save1")]
    _fit_window(ctx, history)
    after = _build(ctx, history)
    assert _receipt_of(after, "read1").get("archived") is None, "拒收未了结：不收"
    unresolved = _actions(ctx, "context_pressure_unresolved")
    assert unresolved and unresolved[0]["skipped_unsettled"] == ["s1"]
    assert not _actions(ctx, "context_archive")
    # 干净存证了结 → 下一批收起
    history.append(_exchange("save_evidence", _save(ctx, "s1", 2, "补对的那张"), "save2"))
    _build(ctx, history)
    assert [a["source_ref"] for a in _actions(ctx, "context_archive")] == ["s1"]


def test_pressure_main_and_child_are_isolated(tmp_path):
    """子 Agent 存卡不是主线的收起资格；子上下文自己的收起不改写主线历史。"""
    ctx = _pressure_ctx(tmp_path)
    _write(ctx, RAW_A, "a")
    read = _exchange("read_source", _read(ctx, "s1"), "read1")
    child = ctx.fork_for_subagent(ctx.config, budget=None, delegation_id="sub1")
    child_receipt = child.read_source({"source_ref": "s1"})[0]
    _save(child, "s1", 1, "子 Agent 存的卡")
    history = [read, _exchange("save_evidence", "子卡已存", "childnote")]
    _fit_window(ctx, history)
    after = _build(ctx, history)
    assert _receipt_of(after, "read1").get("archived") is None
    assert not _actions(ctx, "context_archive")
    # 子上下文自己的压力与收起
    child_history = [
        _exchange("read_source", child_receipt, "cread"),
        _exchange("save_evidence", _save(child, "s1", 2, "子第二张"), "csave"),
    ]
    stable = _build(ctx, history)   # 子存卡改共享账本的计数：先取主线稳定基线
    _fit_window(child, child_history)
    child_after = _build(child, child_history)
    assert _receipt_of(child_after, "cread").get("archived") is True
    # 子上下文的收起不改写主线历史（主线渲染一字不变）
    assert _build(ctx, history) == stable


@pytest.mark.parametrize("mode", ["pressure", "compaction"])
def test_pressure_mcp_rows_replace_only_selected_sources(tmp_path, mode):
    """MCP 多来源嵌套回执只替换被收起的来源块，其他字段保持。"""
    ctx = _pressure_ctx(tmp_path, **({"archive_mode": "immediate", "compaction_enabled": True} if mode == "compaction" else {}))
    _write(ctx, RAW_A, "a")
    _write(ctx, RAW_B, "b")
    row1, row2 = _read(ctx, "s1"), _read(ctx, "s2")
    ctx.mcp.tools["mcp_fetch"] = object()
    mcp_receipt = json.dumps({"server": "sample", "sources": [json.loads(row1), json.loads(row2)]},
                             ensure_ascii=False)
    history = [
        _exchange("mcp_fetch", mcp_receipt, "mcp"),
        _exchange("save_evidence", _save(ctx, "s1", 1, "第一张"), "save1"),
    ]
    _fit_window(ctx, history)
    after = _build(ctx, history)
    payload = _receipt_of(after, "mcp")
    assert payload["server"] == "sample"
    s1_row, s2_row = payload["sources"]
    assert s1_row.get("archived") is True and _visible_chars(s1_row) == 0
    assert s2_row.get("archived") is None and s2_row["source_ref"] == "s2"
    assert _visible_chars(s2_row) > 1000  # 没被选中的来源块不动


def test_pressure_usage_anchor_estimate_basis(tmp_path):
    """有真实用量锚时按锚换算估算，事件里记依据；估算与真实分开记账。"""
    ctx = _pressure_ctx(tmp_path)
    _write(ctx, RAW_A, "a")
    history = [
        _exchange("read_source", _read(ctx, "s1"), "read1"),
        _exchange("save_evidence", _save(ctx, "s1", 1, "第一张"), "save1"),
    ]
    chars = sr._request_chars(_build(ctx, history))
    ctx.note_usage_anchor(100, 200)   # 真实测量：200 字 ≈ 100 token（0.5 token/字）
    w = max(1, int(chars * 0.5 / ctx.config.pressure_trigger_ratio) - 2)
    ctx.config = ctx.config.model_copy(update={"context_window_tokens": w})
    _build(ctx, history)
    batches = _actions(ctx, "context_pressure_batch")
    assert batches and batches[0]["basis"] == "usage_anchored"
    assert abs(batches[0]["estimated_tokens_before"] - chars * 0.5) <= 2
    assert batches[0]["real_chars_removed"] > 0


def test_keep_mode_never_batches_even_over_trigger(tmp_path):
    """keep 模式永不收起，即便超过触发线（对照组语义）。"""
    config = sr.SimpleConfig(
        archive_mode="keep", context_window_tokens=10, pressure_reserve_tokens=0,
        estimate_chars_per_token=1.0, condense_over_chars=None)
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id="keep", query="goal"), config)
    _write(ctx, RAW_A, "a")
    history = [
        _exchange("read_source", _read(ctx, "s1"), "read1"),
        _exchange("save_evidence", _save(ctx, "s1", 1, "第一张"), "save1"),
    ]
    after = _build(ctx, history)
    assert _visible_chars(_receipt_of(after, "read1")) > 1000
    assert not _actions(ctx, "context_pressure_batch") and not _actions(ctx, "context_archive")


@pytest.mark.parametrize("mode", ["pressure", "compaction"])
def test_pressure_resave_below_threshold_does_not_authorize_new_receipts(tmp_path, mode):
    ctx = _pressure_ctx(tmp_path, **({"archive_mode": "immediate", "compaction_enabled": True} if mode == "compaction" else {}))
    _write(ctx, RAW_A, "a")
    h = [_exchange("read_source", _read(ctx, "s1"), "r1"),
         _exchange("save_evidence", _save(ctx, "s1", 1, "first"), "save1")]
    _fit_window(ctx, h)
    _build(ctx, h)
    ctx.config = ctx.config.model_copy(update={"context_window_tokens": 1000000,
                                              "pressure_protect_recent_reads": 2})
    h.append(_exchange("read_source", ctx.read_source({"source_ref": "s1", "range": "0-800"})[0], "r2"))
    before = _build(ctx, h)
    h.append(_exchange("save_evidence", _save(ctx, "s1", 1, "second"), "save2"))
    after = _build(ctx, h)
    assert after[:len(before)] == before
    assert not _receipt_of(after, "r2").get("archived")
    assert len(_actions(ctx, "context_pressure_batch")) == 1


def test_pressure_protection_moves_to_newest_read(tmp_path):
    ctx = _pressure_ctx(tmp_path, pressure_protect_recent_reads=1)
    _write(ctx, RAW_A, "a")
    _write(ctx, RAW_B, "b")
    h = [_exchange("read_source", _read(ctx, "s1"), "r1"),
         _exchange("read_source", _read(ctx, "s1"), "r2"),
         _exchange("save_evidence", _save(ctx, "s1", 1, "first"), "save1")]
    _fit_window(ctx, h)
    first = _build(ctx, h)
    assert not _receipt_of(first, "r2").get("archived")
    h.append(_exchange("read_source", _read(ctx, "s2"), "r3"))
    after = _build(ctx, h)
    assert _receipt_of(after, "r2").get("archived")
    assert not _receipt_of(after, "r3").get("archived")


@pytest.mark.parametrize("by_candidate", [True, False])
def test_pressure_resolved_selection_no_longer_blocks(tmp_path, by_candidate):
    ctx = _pressure_ctx(tmp_path)
    _write(ctx, RAW_A, "a")
    _write(ctx, RAW_A, "b")
    h = [_exchange("read_source", _read(ctx, "s1"), "r1"),
         _exchange("read_source", _read(ctx, "s2"), "r2")]
    res, _ = ctx.save_evidence({"cards": [{"claim": "fact", "quote": "Alpha fact one."}]})
    candidate = json.loads(res)["results"][0]["candidates"][0]
    h.append(_exchange("save_evidence", res, "ambiguous"))
    selector = {"candidate_id": candidate["candidate_id"]} if by_candidate else {"source_ref": "s1"}
    res, ok = ctx.save_evidence({"cards": [{"claim": "fact", "quote": "Alpha fact one.", **selector}]})
    assert ok
    h.append(_exchange("save_evidence", res, "resolved"))
    _fit_window(ctx, h)
    after = _build(ctx, h)
    assert _receipt_of(after, "r1").get("archived")
    assert candidate["candidate_id"] in ctx.quote_candidates  # 历史校验仍可用


def test_pressure_estimator_counts_full_request_consistently(tmp_path):
    ctx = _pressure_ctx(tmp_path)
    messages = [{"role": "assistant", "content": "", "reasoning_content": "thinking" * 100,
                 "tool_calls": [{"id": "x", "function": {"name": "read_source", "arguments": "a" * 1000}}]}]
    schemas = [{"type": "function", "function": {"name": "read_source", "description": "schema" * 100}}]
    chars = sr._request_chars(messages, schemas)
    assert chars > 2200
    ctx.note_usage_anchor(7149, chars)
    assert ctx.estimate_request_tokens(sr._request_chars(messages, schemas))[0] == 7149
    assert ctx.estimate_request_tokens(sr._request_chars(messages + [{"role": "user", "content": "more"}], schemas))[0] > 7149


def test_pressure_rejects_nonpositive_usable_capacity():
    for reserve in (32000, 32001):
        with pytest.raises(ValueError):
            sr.SimpleConfig(archive_mode="pressure", context_window_tokens=32000,
                            pressure_reserve_tokens=reserve)


def test_pressure_does_not_count_inspect_as_collapsible_source(tmp_path):
    ctx = _pressure_ctx(tmp_path)
    _write(ctx, RAW_A, "a")
    read = _read(ctx, "s1")
    h = [_exchange("inspect_research", read, "inspect"),
         _exchange("save_evidence", _save(ctx, "s1", 1, "first"), "save")]
    _fit_window(ctx, h)
    after = _build(ctx, h)
    assert not _actions(ctx, "context_pressure_batch")
    assert not _receipt_of(after, "inspect").get("archived")


def test_pressure_other_pending_quote_remains_protected(tmp_path):
    ctx = _pressure_ctx(tmp_path)
    _write(ctx, RAW_A, "a")
    _write(ctx, RAW_A, "b")
    h = [_exchange("read_source", _read(ctx, "s1"), "r1"),
         _exchange("read_source", _read(ctx, "s2"), "r2")]
    for i, quote in enumerate(("Alpha fact one.", "Gamma exception three.")):
        res, _ = ctx.save_evidence({"cards": [{"claim": "fact", "quote": quote}]})
        h.append(_exchange("save_evidence", res, f"pending{i}"))
    res, ok = ctx.save_evidence({"cards": [{"claim": "first", "quote": "Alpha fact one.", "source_ref": "s1"}]})
    assert ok
    h.append(_exchange("save_evidence", res, "resolved1"))
    _fit_window(ctx, h)
    assert not _receipt_of(_build(ctx, h), "r1").get("archived")
    assert len(ctx.pending_quote_refs) == 1
    res, ok = ctx.save_evidence({"cards": [{"claim": "second", "quote": " Gamma  exception three. ", "source_ref": "s1"}]})
    assert ok
    h.append(_exchange("save_evidence", res, "resolved2"))
    assert _receipt_of(_build(ctx, h), "r1").get("archived")
    assert not ctx.pending_quote_refs


def test_pressure_batch_estimate_matches_render_and_preserves_evidence(tmp_path):
    from copy import deepcopy
    ctx = _pressure_ctx(tmp_path)
    _write(ctx, RAW_A * 10, "a")
    read = _read(ctx, "s1")
    save = _save(ctx, "s1", 1, "first")
    cards = deepcopy(ctx.store.cards())
    exposures = deepcopy(ctx.read_exposures)
    raw = ctx.readings["s1"].raw
    evidence, ok = ctx.inspect_research({"view": "cards", "card_ids": [cards[0].card_id]})
    assert ok
    h = [_exchange("read_source", read, "r1"), _exchange("save_evidence", save, "save"),
         _exchange("inspect_research", evidence, "cards")]
    schemas = [{"type": "function", "function": {"name": "read_source", "description": "read"}}]
    extra = sum(len(json.dumps(x, ensure_ascii=False)) for x in schemas)
    before = sr._request_chars(_build(ctx, h), schemas)
    ctx.note_usage_anchor(before, before)
    ctx.config = ctx.config.model_copy(update={"context_window_tokens": 1000})
    result = sr._build_messages([{"role": "system", "content": "fixed rules"}, {"role": "user", "content": "goal"}],
                                ctx, [], h, seconds_left=None, extra_chars=extra)
    assert ctx.readings["s1"].raw == raw and ctx.store.cards() == cards
    assert ctx.read_exposures == exposures
    assert next(m['content'] for m in result if m.get('tool_call_id') == 'save') == save
    assert next(m['content'] for m in result if m.get('tool_call_id') == 'cards') == evidence
    # 所有压力事件都明确记录能否达到目标；容量不足不伪装成成功。
    events = _actions(ctx, 'context_pressure_batch') + _actions(ctx, 'context_pressure_unresolved')
    assert events
    last = events[-1]
    assert last['estimated_tokens_after'] == round(ctx.estimate_request_tokens(sr._request_chars(result, schemas))[0], 1)
    assert last['over_usable_capacity'] is True
