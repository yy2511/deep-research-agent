"""P1-1b CLI Scoping 测试（已适配 V1 orchestrator）。

全部 mock 掉研究主循环，不联网、不花 API：
- 明确 query 直接进入 run_orchestrator；
- 模糊 query 先读一次用户回答，再把合成 query 传给主循环；
- 用户空回答时取消，不启动研究。

本文件的 main() 测试全部带 --yes：跳过计划确认门，专注测 scoping 分支；
计划门本身（确认环/注入/取消）的测试在 test_plan_confirm.py。
"""

from unittest.mock import MagicMock

import pytest

from dra.__main__ import build_clarified_query, main
from dra.models import Report, ResearchState


def _done_state(query: str) -> ResearchState:
    return ResearchState(
        query=query,
        report=Report(title="Mock"),
        status="done",
    )


def _mock_orchestrator(monkeypatch, return_state: ResearchState):
    """用 async 函数 mock run_orchestrator，asyncio.run 能正常调用。"""
    async def fake(query, config=None, *, verbose=True, research_plan=None) -> ResearchState:
        return return_state
    monkeypatch.setattr("dra.__main__.run_orchestrator", fake)


def test_build_clarified_query_preserves_original_and_answer():
    query = build_clarified_query(" Mercury 是什么？ ", " 我想了解所有含义 ")
    assert query == "Mercury 是什么？\n用户补充说明：我想了解所有含义"


def test_clear_query_runs_without_prompt(monkeypatch):
    input_mock = MagicMock()
    monkeypatch.setattr("sys.argv", ["dra", "--yes", "什么是 RAG？"])
    monkeypatch.setattr("builtins.input", input_mock)
    _mock_orchestrator(monkeypatch, _done_state("什么是 RAG？"))

    main()

    input_mock.assert_not_called()


def test_cli_inherits_current_orchestrator_defaults(monkeypatch):
    """CLI 只显式选模型，控制流一律来自当前 OrchestratorConfig。"""
    captured = {}

    async def fake(query, config=None, *, verbose=True, research_plan=None):
        captured["config"] = config
        return _done_state(query)

    monkeypatch.setattr("sys.argv", ["dra", "--yes", "什么是 RAG？"])
    monkeypatch.setattr("builtins.input", MagicMock())
    monkeypatch.setattr("dra.__main__.run_orchestrator", fake)

    main()

    cfg = captured["config"]
    assert "enable_draft" not in type(cfg).model_fields
    assert "enable_second_opinion" not in type(cfg).model_fields
    assert "max_replan" not in type(cfg).model_fields
    assert "enable_gap_replan" not in type(cfg).model_fields


def test_ambiguous_query_prompts_once_and_runs_clarified_query(
    monkeypatch, capsys
):
    input_mock = MagicMock(return_value="分别介绍罗马神、汞和水星")
    monkeypatch.setattr("sys.argv", ["dra", "--yes", "Mercury", "是什么？"])
    monkeypatch.setattr("builtins.input", input_mock)
    _mock_orchestrator(monkeypatch, _done_state("Mercury 是什么？"))

    main()
    output = capsys.readouterr().out

    input_mock.assert_called_once()
    assert "[澄清]" in output
    assert "[澄清完成]" in output


def test_blank_clarification_cancels_without_research(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["dra", "--yes", "最好的框架是什么？"])
    monkeypatch.setattr("builtins.input", MagicMock(return_value="   "))
    _mock_orchestrator(monkeypatch, _done_state(""))

    main()
    output = capsys.readouterr().out

    assert "[取消]" in output


# ---------------------------------------------------------------------------
# CLI 参数解析
# ---------------------------------------------------------------------------

from dra.__main__ import parse_cli_args  # noqa: E402


def test_parse_args_keeps_only_supported_flags():
    args, stream, yes = parse_cli_args(["--stream", "--yes", "问题"])
    assert (args, stream, yes) == (["问题"], True, True)
