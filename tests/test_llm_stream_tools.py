"""call_tools 流式路径的边界护栏（阶段 1）。

协议样本覆盖一次请求里普通 content 与 tool_calls 同时返回、arguments
跨 chunk 分片拼接，以及 include_usage 字段。

这里只替换外部模型客户端，验证的是真实装配代码：
- 文本增量进 on_chunk，工具分片按 index 完整拼装后才解析；
- 终止原因异常 / 缺 index·id·name / 空流 / 回调出错 → incomplete，**工具一个都不执行**；
- 取消与 deadline 在消费过程中生效，断开时关闭流并如实标 stopped；
- reasoning 扩展按字段原样保留，不合并、不静默丢弃。

纯逻辑、不联网、秒级。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from dra import llm  # noqa: E402


# ---------------------------------------------------------------------------
# 假流式边界：只产出 OpenAI 流式 chunk 形态的对象
# ---------------------------------------------------------------------------


def _chunk(*, content=None, tool_calls=None, finish_reason=None, usage=None,
           reasoning_content=None, reasoning_details=None, reasoning=None):
    delta = NS(content=content, tool_calls=tool_calls, role=None,
               reasoning_content=reasoning_content, reasoning=reasoning,
               reasoning_details=reasoning_details)
    return NS(choices=[NS(delta=delta, index=0, finish_reason=finish_reason)], usage=usage)


def _tc(index, *, id=None, name=None, arguments=None, type=None):
    return NS(index=index, id=id, type=type, function=NS(name=name, arguments=arguments))


def _usage(prompt=100, completion=20):
    return NS(prompt_tokens=prompt, completion_tokens=completion)


class FakeStream:
    """可迭代 + 可 close 的假流，记录 close 是否被调用。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.closed = False
        self.consumed = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    def close(self):
        self.closed = True


class FakeClient:
    def __init__(self, stream):
        self.stream = stream
        self.calls: list[dict] = []

    @property
    def chat(self):
        client = self

        class _Completions:
            @staticmethod
            def create(**kwargs):
                client.calls.append(kwargs)
                return client.stream

        return NS(completions=_Completions())


@pytest.fixture
def fake(monkeypatch):
    """把外部边界换成假流式客户端；返回 (install, calls)。"""
    holders: list[FakeClient] = []

    def install(chunks):
        client = FakeClient(FakeStream(chunks))
        holders.append(client)
        monkeypatch.setattr(llm, "_get_client", lambda provider="openai", _c=client: _c)
        return client

    return install


def _stream_call(client, **kwargs):
    return llm.call_tools(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "search_sources", "parameters": {}}}],
        on_chunk=kwargs.pop("on_chunk", lambda _p: None),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. 正常流：文本增量 + 工具分片
# ---------------------------------------------------------------------------


def test_text_deltas_and_fragmented_tool_arguments_are_assembled(fake):
    client = fake([
        _chunk(content="我先查阅", reasoning_content=None),
        _chunk(content="官方文档。"),
        _chunk(tool_calls=[_tc(0, id="call-1", type="function", name="search_sources",
                               arguments='{"que')]),
        _chunk(tool_calls=[_tc(0, arguments='ry": "Redis 持久化"}')]),
        _chunk(finish_reason="tool_calls", usage=_usage()),
    ])
    pieces: list[str] = []
    turn = _stream_call(client, provider="opencode", on_chunk=pieces.append)

    assert pieces == ["我先查阅", "官方文档。"]
    assert turn.content == "我先查阅官方文档。"
    assert turn.incomplete is False and turn.note == ""
    assert turn.streamed is True
    assert [c.id for c in turn.tool_calls] == ["call-1"]
    assert turn.tool_calls[0].name == "search_sources"
    assert turn.tool_calls[0].arguments == {"query": "Redis 持久化"}
    assert turn.tool_calls[0].arguments_raw == '{"query": "Redis 持久化"}'
    # 原生 assistant_message 原样保留 provider 字段，供下一轮回填
    assert turn.assistant_message["tool_calls"] == [{
        "id": "call-1", "type": "function",
        "function": {"name": "search_sources", "arguments": '{"query": "Redis 持久化"}'}}]
    assert turn.input_tokens == 100 and turn.output_tokens == 20


def test_multiple_tool_calls_keep_provider_order_by_index(fake):
    client = fake([
        _chunk(tool_calls=[_tc(1, id="call-b", name="read_source", arguments='{"ref": "s1"}')]),
        _chunk(tool_calls=[_tc(0, id="call-a", name="search_sources", arguments='{"query": "x"}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    turn = _stream_call(client)
    assert [c.id for c in turn.tool_calls] == ["call-a", "call-b"]


def test_interleaved_fragments_of_two_calls_are_not_mixed(fake):
    client = fake([
        _chunk(tool_calls=[_tc(0, id="c0", name="search_sources", arguments='{"q')]),
        _chunk(tool_calls=[_tc(1, id="c1", name="read_source", arguments='{"r')]),
        _chunk(tool_calls=[_tc(0, arguments='uery": "a"}')]),
        _chunk(tool_calls=[_tc(1, arguments='ef": "s1"}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    turn = _stream_call(client)
    assert [c.arguments for c in turn.tool_calls] == [{"query": "a"}, {"ref": "s1"}]


def test_plain_text_turn_stop_without_tools(fake):
    client = fake([_chunk(content="好的。"), _chunk(finish_reason="stop")])
    turn = _stream_call(client)
    assert turn.content == "好的。"
    assert turn.tool_calls == [] and turn.incomplete is False


# ---------------------------------------------------------------------------
# 2. 协议不完整：一律不执行工具
# ---------------------------------------------------------------------------


def test_length_finish_reason_is_incomplete_and_no_tool_calls(fake):
    """finish_reason=length：内容被截断，半截 arguments 绝不能执行。"""
    client = fake([
        _chunk(tool_calls=[_tc(0, id="c0", name="save_evidence", arguments='{"claim": "半截')]),
        _chunk(finish_reason="length"),
    ])
    turn = _stream_call(client)
    assert turn.incomplete is True
    assert "length" in turn.note
    assert turn.tool_calls == []
    # 片段不静默丢弃：写进 note 供诊断，但不可执行
    assert "未执行片段" in turn.note
    assert "半截" in turn.note


def test_content_filter_finish_reason_is_incomplete(fake):
    client = fake([_chunk(finish_reason="content_filter")])
    turn = _stream_call(client)
    assert turn.incomplete is True and "content_filter" in turn.note


def test_missing_finish_reason_is_incomplete(fake):
    client = fake([_chunk(content="说到一半"), _chunk(tool_calls=[
        _tc(0, id="c0", name="search_sources", arguments='{"query": "x"}')])])
    turn = _stream_call(client)
    assert turn.incomplete is True
    assert "终止标记" in turn.note
    assert turn.tool_calls == []


def test_empty_stream_is_incomplete(fake):
    client = fake([])
    turn = _stream_call(client)
    assert turn.incomplete is True and "空流" in turn.note
    assert turn.content == "" and turn.tool_calls == []


def test_missing_index_does_not_invent_identity(fake):
    client = fake([
        _chunk(tool_calls=[_tc(None, id="c0", name="search_sources", arguments='{"query": "x"}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    turn = _stream_call(client)
    assert turn.incomplete is True
    assert turn.tool_calls == []
    assert "index" in turn.note
    # 不得伪造 call_missing_* 之类的身份
    assert "call_missing" not in json.dumps(turn.assistant_message)


def test_missing_id_is_incomplete_and_not_fabricated(fake):
    client = fake([
        _chunk(tool_calls=[_tc(0, name="search_sources", arguments='{"query": "x"}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    turn = _stream_call(client)
    assert turn.incomplete is True and "缺少 id" in turn.note
    assert turn.tool_calls == []


def test_missing_name_is_incomplete(fake):
    client = fake([
        _chunk(tool_calls=[_tc(0, id="c0", arguments='{"query": "x"}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    turn = _stream_call(client)
    assert turn.incomplete is True and "function.name" in turn.note
    assert turn.tool_calls == []


def test_bad_json_arguments_stay_unparsed_but_call_survives(fake):
    """arguments 拼完但不是合法 JSON：保留 raw 交给上层自纠，不当协议崩溃。"""
    client = fake([
        _chunk(tool_calls=[_tc(0, id="c0", name="search_sources", arguments='{"query": ')]),
        _chunk(finish_reason="tool_calls"),
    ])
    turn = _stream_call(client)
    assert turn.incomplete is False
    assert turn.tool_calls[0].arguments is None
    assert turn.tool_calls[0].arguments_raw == '{"query": '


# ---------------------------------------------------------------------------
# 3. 取消 / deadline：消费过程中生效并关闭流
# ---------------------------------------------------------------------------


def test_cancel_mid_stream_stops_closes_and_marks_stopped(fake, monkeypatch):
    client = fake([
        _chunk(content="开始说明"),
        _chunk(tool_calls=[_tc(0, id="c0", name="search_sources", arguments='{"query": "x"}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    state = {"cancelled": False}
    monkeypatch.setattr(llm, "cancellation_requested", lambda: state["cancelled"])

    pieces: list[str] = []

    def on_chunk(piece):
        pieces.append(piece)
        state["cancelled"] = True  # 第一个增量之后用户取消

    turn = _stream_call(client, on_chunk=on_chunk)
    assert turn.stopped is True and turn.stop_reason == "cancelled"
    assert turn.tool_calls == []
    assert client.stream.closed is True
    # 取消检查发生在 chunk 之间：已取到的那一片按增量交付，但不再继续消费后续内容
    assert client.stream.consumed == 2
    assert pieces == ["开始说明"]
    assert turn.content == "开始说明"


def test_deadline_mid_stream_stops_and_closes(fake, monkeypatch):
    client = fake([_chunk(content="一"), _chunk(content="二"), _chunk(finish_reason="stop")])
    monkeypatch.setattr(llm, "_deadline_expired", lambda: True)
    turn = _stream_call(client)
    assert turn.stopped is True and turn.stop_reason == "deadline"
    assert turn.tool_calls == []
    assert client.stream.closed is True
    # 第一个 chunk 已从流里取出，但 deadline 检查在消费之前，内容一律不采用
    assert client.stream.consumed == 1
    assert turn.content == ""


def test_callback_error_is_reported_not_swallowed(fake):
    client = fake([_chunk(content="x"), _chunk(finish_reason="stop")])

    def boom(_piece):
        raise RuntimeError("前端接不上")

    turn = _stream_call(client, on_chunk=boom)
    assert turn.incomplete is True
    assert "增量回调失败" in turn.note and "前端接不上" in turn.note


# ---------------------------------------------------------------------------
# 4. reasoning 扩展：按字段保留
# ---------------------------------------------------------------------------


def test_reasoning_fields_are_preserved_separately(fake):
    client = fake([
        _chunk(reasoning_content="想"),
        _chunk(reasoning_content="清楚了", reasoning_details=[{"type": "reasoning.text", "text": "a"}]),
        _chunk(content="好。"),
        _chunk(finish_reason="stop"),
    ])
    turn = _stream_call(client)
    msg = turn.assistant_message
    assert msg["reasoning_content"] == "想清楚了"
    assert msg["reasoning_details"] == [{"type": "reasoning.text", "text": "a"}]
    # reasoning 绝不混进用户可见文本
    assert turn.content == "好。"


def test_reasoning_detail_arrays_from_multiple_chunks_stay_flat(fake):
    """跨 chunk 的 reasoning_details 必须拼成**一层**对象序列（OpenRouter 协议）。"""
    first = {"index": 0, "type": "reasoning.text", "text": "第一块"}
    second = {"index": 1, "type": "reasoning.encrypted", "data": "opaque"}
    client = fake([
        _chunk(reasoning_details=[first]),
        _chunk(reasoning_details=[second]),
        _chunk(finish_reason="stop"),
    ])
    turn = _stream_call(client)
    assert turn.assistant_message["reasoning_details"] == [first, second]
    assert turn.incomplete is False


def test_reasoning_detail_same_index_fragments_merge_in_place(fake):
    """同一 index 的分片按字段累加，不新开一块、不改 type、不乱序。"""
    client = fake([
        _chunk(reasoning_details=[{"index": 0, "type": "reasoning.text", "text": "上半",
                                   "format": "anthropic-claude-v1"}]),
        _chunk(reasoning_details=[{"index": 0, "type": "reasoning.text", "text": "下半",
                                   "signature": "sig"}]),
        _chunk(reasoning_details=[{"index": 1, "type": "reasoning.summary", "summary": "结论"}]),
        _chunk(finish_reason="stop"),
    ])
    turn = _stream_call(client)
    details = turn.assistant_message["reasoning_details"]
    assert [b["index"] for b in details] == [0, 1]
    assert details[0]["text"] == "上半下半"
    assert details[0]["format"] == "anthropic-claude-v1"
    assert details[0]["signature"] == "sig"
    assert turn.incomplete is False


def test_reasoning_detail_same_index_different_type_is_not_merged(fake):
    """index 相同但 type 不同：不猜语义，各自成块。"""
    client = fake([
        _chunk(reasoning_details=[{"index": 0, "type": "reasoning.text", "text": "a"}]),
        _chunk(reasoning_details=[{"index": 0, "type": "reasoning.summary", "summary": "b"}]),
        _chunk(finish_reason="stop"),
    ])
    turn = _stream_call(client)
    assert turn.assistant_message["reasoning_details"] == [
        {"index": 0, "type": "reasoning.text", "text": "a"},
        {"index": 0, "type": "reasoning.summary", "summary": "b"},
    ]


def test_note_never_contains_reasoning_text(fake):
    """公开诊断只报结构（字段名/块数/类型），绝不带隐藏思考正文。"""
    secret = "内部推理：用户看不到这句话"
    client = fake([
        _chunk(reasoning_content=secret, reasoning=secret,
               reasoning_details=[{"index": 0, "type": "reasoning.text", "text": secret}]),
        _chunk(reasoning_details=["坏块"], tool_calls=[
            _tc(0, id="c0", name="search_sources", arguments='{"query": "x"}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    turn = _stream_call(client)
    assert turn.incomplete is True
    assert secret not in turn.note, turn.note
    assert "reasoning_details" in turn.note          # 字段名可以出现
    assert "块不是对象" in turn.note                  # 结构错误原因可以出现
    # 内容仍然按字段原样回填，供下一轮协议使用
    assert turn.assistant_message["reasoning_content"] == secret
    assert turn.assistant_message["reasoning_details"][0]["text"] == secret

    # 合法块（含同 index 不同类型）可原样保留：不构成协议错误、也不该被拒绝执行；
    # 同时确认它们不会把正文带进任何公开诊断。
    client2 = fake([
        _chunk(reasoning_details=[{"index": 0, "type": "reasoning.text", "text": secret},
                                  {"index": 0, "type": "reasoning.summary", "summary": "结论"}],
               tool_calls=[_tc(0, id="c0", name="search_sources", arguments='{"query": "x"}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    turn2 = _stream_call(client2)
    assert turn2.incomplete is False and turn2.note == ""
    assert turn2.tool_calls[0].name == "search_sources"
    assert turn2.assistant_message["reasoning_details"] == [
        {"index": 0, "type": "reasoning.text", "text": secret},
        {"index": 0, "type": "reasoning.summary", "summary": "结论"}]


def test_unrecognizable_reasoning_block_is_not_silently_rewritten(fake):
    """非对象块无法安全回填：如实标不完整并写明，不静默丢弃后继续调用。"""
    client = fake([
        _chunk(reasoning_details=["裸字符串块"]),
        _chunk(tool_calls=[_tc(0, id="c0", name="search_sources", arguments='{"query": "x"}')]),
        _chunk(finish_reason="tool_calls"),
    ])
    turn = _stream_call(client)
    assert turn.incomplete is True
    assert "块不是对象" in turn.note
    assert turn.tool_calls == []
    assert turn.assistant_message["reasoning_details"] == []


def test_detail_without_index_is_kept_in_order(fake):
    """index 可选：缺失时按到达顺序追加，不猜编号。"""
    client = fake([
        _chunk(reasoning_details=[{"type": "reasoning.text", "text": "x"}]),
        _chunk(reasoning_details=[{"type": "reasoning.text", "text": "y"}]),
        _chunk(finish_reason="stop"),
    ])
    turn = _stream_call(client)
    details = turn.assistant_message["reasoning_details"]
    assert details == [{"type": "reasoning.text", "text": "x"},
                       {"type": "reasoning.text", "text": "y"}]
    assert all("index" not in b for b in details)


def test_non_string_reasoning_is_kept_as_structure(fake):
    """非字符串扩展字段保留原结构；单块不做多余包裹，多块保序成列表。"""
    client = fake([
        _chunk(reasoning_content={"steps": ["a"]}),
        _chunk(finish_reason="stop"),
    ])
    turn = _stream_call(client)
    assert turn.assistant_message["reasoning_content"] == {"steps": ["a"]}

    client2 = fake([
        _chunk(reasoning_content={"steps": ["a"]}),
        _chunk(reasoning_content={"steps": ["b"]}),
        _chunk(finish_reason="stop"),
    ])
    turn2 = _stream_call(client2)
    assert turn2.assistant_message["reasoning_content"] == [{"steps": ["a"]}, {"steps": ["b"]}]


# ---------------------------------------------------------------------------
# 5. 请求参数：流式用量只在已核实的通道上开
# ---------------------------------------------------------------------------


def test_opencode_stream_requests_usage_by_default(fake):
    client = fake([_chunk(content="x"), _chunk(finish_reason="stop", usage=_usage())])
    turn = _stream_call(client, provider="opencode")
    assert client.calls[0]["stream"] is True
    assert client.calls[0]["stream_options"] == {"include_usage": True}
    assert turn.input_tokens == 100


def test_other_provider_does_not_send_stream_options_by_default(fake):
    client = fake([_chunk(content="x"), _chunk(finish_reason="stop")])
    turn = _stream_call(client, provider="openai")
    assert client.calls[0]["stream"] is True
    assert "stream_options" not in client.calls[0]
    # 不回传 usage → 如实置 None，由调用方按 usage_unknown 记账
    assert turn.input_tokens is None and turn.output_tokens is None


def test_explicit_stream_usage_override(fake):
    client = fake([_chunk(content="x"), _chunk(finish_reason="stop")])
    _stream_call(client, provider="openai", stream_usage=True)
    assert client.calls[0]["stream_options"] == {"include_usage": True}
    client2 = fake([_chunk(content="x"), _chunk(finish_reason="stop")])
    _stream_call(client2, provider="opencode", stream_usage=False)
    assert "stream_options" not in client2.calls[0]


# ---------------------------------------------------------------------------
# 6. 非流式兼容：不传 on_chunk 的调用方零变化
# ---------------------------------------------------------------------------


def test_without_on_chunk_no_stream_flag(monkeypatch):
    class PlainClient:
        def __init__(self):
            self.calls = []
            outer = self

            class _Completions:
                @staticmethod
                def create(**kwargs):
                    outer.calls.append(kwargs)
                    msg = NS(content="文本", tool_calls=None, reasoning_content=None)
                    return NS(choices=[NS(message=msg)], usage=_usage())

            self.chat = NS(completions=_Completions())

    client = PlainClient()
    monkeypatch.setattr(llm, "_get_client", lambda provider="openai": client)
    turn = llm.call_tools([{"role": "user", "content": "hi"}], tools=[])
    assert "stream" not in client.calls[0]
    assert turn.content == "文本" and turn.incomplete is False and turn.streamed is False
