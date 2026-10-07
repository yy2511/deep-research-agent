"""关闭模型输入/输出上限后的默认行为。

模型输出与单次读取默认不设上限，显式给值仍生效；已退役的机械上下文裁剪配置不再接受。
"""

import json
from unittest.mock import MagicMock

import pytest

from dra import llm as llm_module
from dra import simple_research as sr
from test_simple_research import _doc, _run, _turn


def _reset_cached_clients():
    llm_module._clients.clear()


def test_default_config_disables_model_io_caps():
    """模型输出与读取窗口默认不设上限。"""
    config = sr.SimpleConfig()
    assert config.max_output_tokens is None
    assert config.read_window_chars is None
    assert config.storage_max_chars is None
    assert config.effective_read_window() is None


def test_config_rejects_invalid_limits_and_retired_options():
    """现役限额校验不变；旧裁剪与旧归档名由现有 extra=forbid 拒绝。"""
    for kwargs in (
        {"read_window_chars": 0},
        {"working_context_max_exchanges": 6},
        {"working_context_max_chars": 24000},
        {"archive_saved_sources": True},
        {"max_output_tokens": 511},
    ):
        try:
            sr.SimpleConfig(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"{kwargs} 应被拒绝")


def test_read_source_without_window_covers_whole_saved_body(tmp_path):
    """read_window_chars=None：一次 read_source 就能覆盖整份已保存正文。"""
    raw = "这是用于撑长正文的段落。" * 20_000  # 超过原来的 20 万字符保存上限
    config = sr.SimpleConfig(max_llm_calls=6, max_tool_calls=6, wall_timeout_s=60.0, wrapup_reserve_s=0,
                             condense_over_chars=None)
    _result, script = _run(
        [
            _turn(("search_sources", {"query": "q"})),
            _turn(("read_source", {"source_ref": "s1"})),
        ],
        raw=raw,
        config=config,
        run_dir=tmp_path / "run",
    )
    receipts = []
    # seen[-1] 是最后一次模型调用前装配的完整消息（含刚才那次 read_source 回执）。
    for message in script.seen[-1]:
        if message.get("role") != "tool":
            continue
        try:
            receipts.append(json.loads(message["content"]))
        except (TypeError, ValueError):
            continue
    window = next(row for row in receipts if "shown_range" in row)
    start, stop = (int(v) for v in window["shown_range"][6:].split("-"))
    assert start == 0
    assert window["total_chars"] == len(raw)
    assert window["truncated_source"] is False
    assert stop == window["total_chars"], "不设窗口时必须一次读到保存范围末尾"
    assert window["has_more"] is False and window["next_cursor"] is None


@pytest.mark.parametrize("locator", ["find", "range", "cursor"])
def test_unlimited_storage_keeps_far_tail_readable_and_citable(tmp_path, locator):
    prefix = "filler\n" * 130_001
    quote = "The archive retains logs for 43 days."
    raw = prefix + quote + "\n" + "closing\n" * 15_000
    config = sr.SimpleConfig(read_window_chars=2000)
    store = sr.open_run(tmp_path / "run", run_id="long-source", query="retention")
    context = sr.ToolContext(store, config, search_fn=lambda _: [_doc(raw=raw)])
    text, ok = context.search_sources({"query": "retention"})
    row = json.loads(text)["results"][0]
    assert ok and row["total_chars"] == len(raw)
    text, ok = context.read_source({"source_ref": row["source_ref"], "range": "0-40"})
    ref = json.loads(text)["source_ref"]
    assert ok and store.read_source_text(ref) == raw
    snapshot = store.source(ref)
    assert snapshot.raw_chars == len(raw) and snapshot.truncated is False
    card = {"claim": "Logs are kept for 43 days.", "quote": quote}
    assert not context.save_evidence({"cards": [card]})[1], "完整保存不等于已经读过"
    args = ({"find": quote} if locator == "find" else
            {"range": f"{len(prefix)}-{len(prefix) + 500}"} if locator == "range" else
            {"cursor": len(prefix)})
    text, ok = context.read_source({"source_ref": ref, **args})
    payload = json.loads(text)
    assert ok and quote in "".join(e["quote"] for e in payload["excerpts"])
    assert sum(len(e["quote"]) for e in payload["excerpts"]) <= 2000
    assert context.save_evidence({"cards": [card]})[1]
    saved = store.cards()[0]
    assert saved.location == f"chars={len(prefix)}-{len(prefix) + len(quote)}"
    assert store.read_source_text(saved.source_ref)[len(prefix):len(prefix) + len(quote)] == saved.quote


def test_chat_omits_max_tokens_when_none():
    """max_tokens=None 时连字段都不发；显式给值时照常发送。"""
    fake_client = MagicMock()
    response = MagicMock()
    response.choices[0].message.content = "ok"
    response.usage.prompt_tokens = 1
    response.usage.completion_tokens = 1
    fake_client.chat.completions.create.return_value = response
    llm_module._clients["openai"] = fake_client
    try:
        assert llm_module.chat([{"role": "user", "content": "hi"}], max_tokens=None) == "ok"
        assert "max_tokens" not in fake_client.chat.completions.create.call_args.kwargs
        llm_module.chat([{"role": "user", "content": "hi"}], max_tokens=4096)
        assert fake_client.chat.completions.create.call_args.kwargs["max_tokens"] == 4096
    finally:
        _reset_cached_clients()


def test_call_tools_omits_max_tokens_when_none():
    """工具回合与 chat 同语义：None 不发字段，显式值照常发送。"""
    fake_client = MagicMock()
    response = MagicMock()
    response.choices[0].message.content = "hi"
    response.choices[0].message.tool_calls = []
    response.usage.prompt_tokens = 1
    response.usage.completion_tokens = 1
    fake_client.chat.completions.create.return_value = response
    llm_module._clients["openai"] = fake_client
    tool_schema = {"type": "function", "function": {"name": "x", "parameters": {}}}
    try:
        turn = llm_module.call_tools(
            [{"role": "user", "content": "hi"}], tools=[tool_schema], max_tokens=None
        )
        assert turn.content == "hi"
        assert "max_tokens" not in fake_client.chat.completions.create.call_args.kwargs
        llm_module.call_tools(
            [{"role": "user", "content": "hi"}], tools=[tool_schema], max_tokens=2048
        )
        assert fake_client.chat.completions.create.call_args.kwargs["max_tokens"] == 2048
    finally:
        _reset_cached_clients()


def test_runtime_config_sets_explicit_output_cap():
    """Web 与脚本的统一入口显式给输出上限，避免上游在不带 max_tokens 时按 8192 截断长交稿。"""
    from dra import runtime_config
    assert runtime_config.to_simple_config(runtime_config._code_defaults()).max_output_tokens == 32768
