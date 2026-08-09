"""llm.call_tools：原生 function calling 薄封装。全部 mock client，零 API 成本。"""
from types import SimpleNamespace as NS

import httpx
import pytest
from openai import APIStatusError

import dra.llm as llm


def _tc(id_, name, arguments):
    """伪造 OpenAI SDK 的 tool_call 对象（属性访问兼容即可）。"""
    return NS(id=id_, function=NS(name=name, arguments=arguments))


def _resp(content=None, tool_calls=None, usage=(10, 5)):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls))],
              usage=NS(prompt_tokens=usage[0], completion_tokens=usage[1]))


def _fake_client(monkeypatch, replies):
    """让 _get_client 返回按序吐 replies 的假 client；记录每次 create 的 kwargs。
    replies 里的元素是 Exception 实例则 raise，否则 return。"""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        r = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    client = NS(chat=NS(completions=NS(create=create)))
    monkeypatch.setattr(llm, "_get_client", lambda provider="openai": client)
    return calls


def _api_error(status):
    req = httpx.Request("POST", "http://test")
    return APIStatusError("boom", response=httpx.Response(status, request=req), body=None)


def test_call_tools_parses_tool_calls(monkeypatch):
    _fake_client(monkeypatch, [_resp(tool_calls=[_tc("c1", "search", '{"query": "ev sales"}')])])
    turn = llm.call_tools([{"role": "user", "content": "x"}], tools=[])
    assert turn.tool_calls[0].name == "search"
    assert turn.tool_calls[0].arguments == {"query": "ev sales"}
    # assistant_message 能原样回填 history（OpenAI 契约：id/type/function 三件套）
    am = turn.assistant_message
    assert am["role"] == "assistant"
    assert am["tool_calls"][0]["id"] == "c1"
    assert am["tool_calls"][0]["function"]["name"] == "search"


def test_call_tools_bad_arguments_is_none(monkeypatch):
    _fake_client(monkeypatch, [_resp(tool_calls=[_tc("c1", "search", "not json")])])
    turn = llm.call_tools([{"role": "user", "content": "x"}], tools=[])
    assert turn.tool_calls[0].arguments is None       # 不抛——错误回给模型自纠
    assert turn.tool_calls[0].arguments_raw == "not json"


def test_call_tools_arguments_non_dict_is_none(monkeypatch):
    _fake_client(monkeypatch, [_resp(tool_calls=[_tc("c1", "search", '[1, 2]')])])
    turn = llm.call_tools([{"role": "user", "content": "x"}], tools=[])
    assert turn.tool_calls[0].arguments is None       # 合法 JSON 但不是对象，同样交回模型


def test_call_tools_text_only(monkeypatch):
    _fake_client(monkeypatch, [_resp(content="我想直接回答")])
    turn = llm.call_tools([{"role": "user", "content": "x"}], tools=[])
    assert turn.tool_calls == []
    assert turn.content == "我想直接回答"
    assert "tool_calls" not in turn.assistant_message  # 无调用时不带空 tool_calls 键


def test_call_tools_counts_tokens(monkeypatch):
    _fake_client(monkeypatch, [_resp(content="ok", usage=(100, 50))])
    llm.reset_token_usage()
    llm.call_tools([{"role": "user", "content": "x"}], tools=[])
    assert llm.get_token_usage() == {"input": 100, "output": 50}


def test_call_tools_4xx_no_retry(monkeypatch):
    calls = _fake_client(monkeypatch, [_api_error(400)])
    with pytest.raises(RuntimeError, match="4xx"):
        llm.call_tools([{"role": "user", "content": "x"}], tools=[])
    assert len(calls) == 1


def test_call_tools_5xx_retries_then_ok(monkeypatch):
    calls = _fake_client(monkeypatch, [_api_error(500), _resp(content="ok")])
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)   # 免等退避
    turn = llm.call_tools([{"role": "user", "content": "x"}], tools=[])
    assert turn.content == "ok"
    assert len(calls) == 2


def test_call_tools_non_json_response_fails_fast_with_base_url_hint(monkeypatch):
    calls = _fake_client(monkeypatch, ["<html>gateway home</html>"])
    with pytest.raises(llm.LLMProtocolError, match=r"OPENAI_BASE_URL.*?/v1"):
        llm.call_tools(
            [{"role": "user", "content": "x"}],
            tools=[],
            provider="openai",
            max_retries=2,
        )
    assert len(calls) == 1


def test_call_tools_reasoning_off_opencode(monkeypatch):
    calls = _fake_client(monkeypatch, [_resp(content="ok")])
    llm.call_tools([{"role": "user", "content": "x"}], tools=[],
                   provider="opencode", reasoning=False)
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}
