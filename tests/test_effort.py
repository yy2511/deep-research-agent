"""reasoning_effort（思维强度）透传支持。

背景（2026-07-06）：后续换非 zetatechs 中转——zeta 把 effort 编码在模型名后缀
（gpt-5.5-high / gpt-5-mini-minimal），别家 OpenAI 兼容中转走原生 reasoning_effort
参数。本特性契约：
- llm.chat / llm.call_tools 新增 effort: str | None（默认 None = 现状零变化）；
  非 None 时 extra_body 发 {"reasoning_effort": effort}。
- effort 与关推理参数 **互斥**（effort=开思考并定档）：llm 层 effort 优先、
  绝不同时发两个（thinking:disabled 或 reasoning_effort:none）；config 层
  fail-loud（effort 非 None 时该档 reasoning 必须 True）。
- effort 值不做白名单：各家中转档位表不同（low/medium/high/xhigh/none…），
  端点自己校验，白名单只会跟着中转漂移。
- reasoning=False 按 provider 翻译关闭参数：opencode → thinking:disabled；
  codex521/codex_local → reasoning_effort:none；其它 provider 不乱传。
- call_json 经 **chat_kwargs 天然透传，零改动。
"""

import json
from types import SimpleNamespace as NS

import pytest

import dra.llm as llm


def _resp(content="ok"):
    return NS(choices=[NS(message=NS(content=content, tool_calls=None))],
              usage=NS(prompt_tokens=10, completion_tokens=5))


def _fake_client(monkeypatch, content="ok"):
    """假 client：记录每次 create 的 kwargs（模式同 test_call_tools._fake_client）。"""
    calls: list[dict] = []

    def create(**kwargs):
        calls.append(kwargs)
        return _resp(content)

    client = NS(chat=NS(completions=NS(create=create)))
    monkeypatch.setattr(llm, "_get_client", lambda provider="openai": client)
    return calls


# ---------------------------------------------------------------------------
# llm.chat 层
# ---------------------------------------------------------------------------


def test_chat_effort_sends_reasoning_effort(monkeypatch):
    calls = _fake_client(monkeypatch)
    llm.chat([{"role": "user", "content": "x"}], provider="opencode", effort="high")
    assert calls[0]["extra_body"] == {"reasoning_effort": "high"}


def test_chat_effort_wins_over_thinking_disabled(monkeypatch):
    """effort 与关思考语义冲突：llm 层 effort 优先，绝不同时发两个参数。"""
    calls = _fake_client(monkeypatch)
    llm.chat([{"role": "user", "content": "x"}],
             provider="opencode", reasoning=False, effort="high")
    assert calls[0]["extra_body"] == {"reasoning_effort": "high"}
    assert "thinking" not in calls[0]["extra_body"]


def test_chat_no_effort_keeps_thinking_disabled(monkeypatch):
    """回归：effort 缺省时 opencode 关思考注入行为与现状字节级一致。"""
    calls = _fake_client(monkeypatch)
    llm.chat([{"role": "user", "content": "x"}], provider="opencode", reasoning=False)
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_chat_no_effort_no_extra_body_on_other_provider(monkeypatch):
    """回归：非 opencode/codex 且无 effort → 不发 extra_body（别乱发参数）。"""
    calls = _fake_client(monkeypatch)
    llm.chat([{"role": "user", "content": "x"}], provider="openai", reasoning=False)
    assert "extra_body" not in calls[0]


def test_chat_codex521_reasoning_off_sends_effort_none(monkeypatch):
    """codex521 关推理：reasoning=False 且 effort=None → reasoning_effort:none。"""
    calls = _fake_client(monkeypatch)
    llm.chat([{"role": "user", "content": "x"}], provider="codex521", reasoning=False)
    assert calls[0]["extra_body"] == {"reasoning_effort": "none"}


def test_chat_codex_local_reasoning_off_sends_effort_none(monkeypatch):
    """codex_local 与 codex521 同属 Codex 中转集合，关推理语义一致。"""
    calls = _fake_client(monkeypatch)
    llm.chat([{"role": "user", "content": "x"}], provider="codex_local", reasoning=False)
    assert calls[0]["extra_body"] == {"reasoning_effort": "none"}


def test_chat_codex_effort_wins_over_reasoning_off(monkeypatch):
    """effort 显式给定时优先，不同时发 reasoning_effort:none。"""
    calls = _fake_client(monkeypatch)
    llm.chat([{"role": "user", "content": "x"}],
             provider="codex521", reasoning=False, effort="high")
    assert calls[0]["extra_body"] == {"reasoning_effort": "high"}
    assert calls[0]["extra_body"] != {"reasoning_effort": "none"}


def test_chat_effort_is_provider_agnostic(monkeypatch):
    """effort 不闸 provider：换任何 OpenAI 兼容中转都能带上。"""
    calls = _fake_client(monkeypatch)
    llm.chat([{"role": "user", "content": "x"}], provider="openai", effort="xhigh")
    assert calls[0]["extra_body"] == {"reasoning_effort": "xhigh"}


def test_call_json_forwards_effort(monkeypatch):
    captured: dict = {}

    def fake_chat(messages, **kw):
        captured.update(kw)
        return '{"ok": 1}'

    monkeypatch.setattr(llm, "chat", fake_chat)
    llm.call_json([{"role": "user", "content": "给我 json"}],
                  expect_keys=("ok",), effort="low")
    assert captured["effort"] == "low"


# ---------------------------------------------------------------------------
# llm.call_tools 层（loop worker 的出口）
# ---------------------------------------------------------------------------


def test_call_tools_effort_sends_reasoning_effort(monkeypatch):
    calls = _fake_client(monkeypatch)
    llm.call_tools([{"role": "user", "content": "x"}], tools=[],
                   provider="opencode", reasoning=False, effort="medium")
    assert calls[0]["extra_body"] == {"reasoning_effort": "medium"}
    assert "thinking" not in calls[0]["extra_body"]


def test_call_tools_no_effort_keeps_thinking_disabled(monkeypatch):
    calls = _fake_client(monkeypatch)
    llm.call_tools([{"role": "user", "content": "x"}], tools=[],
                   provider="opencode", reasoning=False)
    assert calls[0]["extra_body"] == {"thinking": {"type": "disabled"}}


def test_call_tools_codex521_reasoning_off_sends_effort_none(monkeypatch):
    """call_tools 与 chat 同构：codex521 关推理 → reasoning_effort:none。"""
    calls = _fake_client(monkeypatch)
    llm.call_tools([{"role": "user", "content": "x"}], tools=[],
                   provider="codex521", reasoning=False)
    assert calls[0]["extra_body"] == {"reasoning_effort": "none"}


def test_call_tools_codex_effort_wins_over_reasoning_off(monkeypatch):
    """call_tools：effort 优先，不同时发 reasoning_effort:none。"""
    calls = _fake_client(monkeypatch)
    llm.call_tools([{"role": "user", "content": "x"}], tools=[],
                   provider="codex521", reasoning=False, effort="medium")
    assert calls[0]["extra_body"] == {"reasoning_effort": "medium"}


# ---------------------------------------------------------------------------
# config 层 fail-loud（co-knob 校验：effort 非 None 时该档 reasoning 必须开）
# ---------------------------------------------------------------------------


def test_subagent_config_effort_requires_reasoning():
    from dra.subagent import SubAgentConfig

    with pytest.raises(ValueError, match="effort"):
        SubAgentConfig(effort="high")            # 默认 reasoning=False → 冲突
    cfg = SubAgentConfig(effort="high", reasoning=True)
    assert cfg.effort == "high"


def test_subagent_config_summarize_effort_requires_summarize_reasoning():
    """summarize 档 co-knob：summarize_effort 与 summarize_reasoning=False 冲突 fail-loud。"""
    from dra.subagent import SubAgentConfig

    with pytest.raises(ValueError, match="summarize_effort"):
        SubAgentConfig(summarize_effort="high")  # 默认 summarize_reasoning=False
    cfg = SubAgentConfig(summarize_effort="high", summarize_reasoning=True)
    assert cfg.summarize_effort == "high"
    # 子代理开推理不影响 summarize 默认关
    cfg2 = SubAgentConfig(reasoning=True, effort="high")
    assert cfg2.summarize_reasoning is False and cfg2.summarize_effort is None


def test_orchestrator_config_writer_effort_requires_writer_reasoning():
    from dra.orchestrator import OrchestratorConfig

    with pytest.raises(ValueError, match="effort"):
        OrchestratorConfig(writer_effort="high")  # 默认 writer_reasoning=False → 冲突
    cfg = OrchestratorConfig(writer_effort="high", writer_reasoning=True)
    assert cfg.writer_effort == "high"
    # planner 档节点默认开思考，planner_effort 无冲突组合，直接可设
    assert OrchestratorConfig(planner_effort="xhigh").planner_effort == "xhigh"


# ---------------------------------------------------------------------------
# 节点穿参（代表性：planner 档 build_research_plan；其余调用点由 grep 审计保证同构）
# ---------------------------------------------------------------------------


def test_build_research_plan_forwards_effort(monkeypatch):
    from dra.nodes import build_research_plan

    captured: dict = {}

    def fake_chat(messages, **kw):
        captured.update(kw)
        return json.dumps({
            "plan_nodes": [{
                "id": "root",
                "objective": "o",
                "kind": "research",
                "dependency_ids": [],
                "acceptance_criteria": "取得有来源证据",
            }],
            "initial_tasks": [{
                "node_id": "root",
                "objective": "o",
                "search_query": "q",
            }],
        }, ensure_ascii=False)

    monkeypatch.setattr("dra.llm.chat", fake_chat)
    build_research_plan("研究问题", effort="high")
    assert captured["effort"] == "high"
