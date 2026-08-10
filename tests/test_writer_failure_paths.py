"""writer 错误路径与超时路径对称降级(2026-07-26 P1 修复)。

背景:首稿/重写只 except LLMRequestTimeout,单次 429/500/网络错直接崩掉整个
run(研究成果不落盘);重写失败还会丢弃内存里已成功的首稿——与「格式优化失败
不能污染首稿」的注释意图相反。修复后:任何 writer 异常与超时同层降级,
但标签诚实区分 writer_timeout / writer_failed。
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from dra import llm
from dra.models import (
    ReportPlan,
    ReportPlanSection,
    EvidenceCard,
    NodeKind,
    NodeAssessment,
    NodeStatus,
    Report,
    ReportSection,
    ResearchPlan,
    PlanNode,
    SubAgentReport,
    ResearchTask,
    WorkerAttempt,
)
from dra.orchestrator import OrchestratorConfig, run_orchestrator
from dra.nodes import format_gate

_GOOD_REPORT = Report(
    title="首稿",
    sections=[ReportSection(heading="发现", markdown="正文[1]" * 40, coverage_ids=[])],
)


def _run(monkeypatch, *, write_report_mock, format_gate_mock=None):
    card = EvidenceCard(
        id="e1", claim="事实", support_quote="原文事实",
        source_url="https://example.com/e1",
    )
    research_plan = ResearchPlan(
        clarified_query="核验主题",
        plan_nodes=[PlanNode(
            id="m1", objective="核验", kind=NodeKind.RESEARCH,
            dependency_ids=[], acceptance_criteria="有证据",
        )],
        initial_tasks=[ResearchTask(
            id="t1", node_id="m1", objective="搜", search_query="q",
        )],
    )

    async def fake_dispatch(tasks, config, *, verbose, deadline, mission_context_by_task=None):
        reports = [
            SubAgentReport(research_task_id=t.id, objective=t.objective,
                           evidence=[card], status="ok")
            for t in tasks
        ]
        attempts = [
            WorkerAttempt(task_id=t.id, node_id=t.node_id,
                          round_index=t.round_index, status="ok")
            for t in tasks
        ]
        return reports, 0, attempts

    def fake_assess(state, _research_plan, plan_nodes, *, tasks, reports, config):
        state.node_assessments = [NodeAssessment(
            node_id="m1", status=NodeStatus.COMPLETE,
            summary="ok", evidence_ids=["e1"],
        )]
        return state.node_assessments

    monkeypatch.setattr("dra.orchestrator._dispatch_task_batch", fake_dispatch)
    monkeypatch.setattr("dra.orchestrator._assess_batch", fake_assess)
    monkeypatch.setattr(
        "dra.orchestrator.build_report_plan",
        lambda *a, **k: ReportPlan(sections=[
            ReportPlanSection(heading="发现", covers="主题", limitations=[]),
        ]),
    )
    monkeypatch.setattr("dra.orchestrator.write_report", write_report_mock)
    monkeypatch.setattr(
        "dra.orchestrator.format_gate",
        format_gate_mock or (lambda *a, **k: []),
    )

    return asyncio.run(run_orchestrator(
        "核验主题",
        config=OrchestratorConfig(max_research_rounds=1, total_timeout_s=None),
        research_plan=research_plan,
        verbose=False,
    ))


def test_writer_error_degrades_to_fallback_partial(monkeypatch):
    """首稿 500/网络错不再崩整个 run:降级证据摘要,partial + 诚实标签。"""
    boom = MagicMock(side_effect=RuntimeError("provider 500"))

    state = _run(monkeypatch, write_report_mock=boom)

    assert state.report is not None and state.report.sections
    assert state.status == "partial"
    assert "writer_failed" in state.completion_blockers
    assert "writer_failed_fallback" in state.warnings
    assert "writer_timeout" not in state.completion_blockers  # 不是超时就别叫超时


def test_writer_timeout_keeps_timeout_labels(monkeypatch):
    """超时分支标签不变(回归网)。"""
    boom = MagicMock(side_effect=llm.LLMRequestTimeout("slow"))

    state = _run(monkeypatch, write_report_mock=boom)

    assert state.status == "partial"
    assert "writer_timeout" in state.completion_blockers
    assert "writer_failed" not in state.completion_blockers


def test_rewrite_error_keeps_first_report_plan(monkeypatch):
    """格式重写失败回退首稿,不把成功首稿一起带走;run 不因此失败。"""
    calls = {"write": 0, "gate": 0}

    def fake_write(*a, **k):
        calls["write"] += 1
        if calls["write"] == 1:
            return _GOOD_REPORT
        raise RuntimeError("rewrite 500")

    def fake_gate(*a, **k):
        calls["gate"] += 1
        # 首检报缺(触发重写),复检通过
        return ["缺【关键发现】"] if calls["gate"] == 1 else []

    state = _run(monkeypatch, write_report_mock=fake_write, format_gate_mock=fake_gate)

    assert calls["write"] == 2                     # 确认真的走到了重写
    assert state.report is _GOOD_REPORT            # 首稿保住
    assert state.status == "done"
    assert "writer_rewrite_failed" in state.warnings


def test_invalid_letter_citation_triggers_rewrite(monkeypatch):
    calls = {"write": 0}

    def fake_write(*_args, **kwargs):
        calls["write"] += 1
        plan_id = kwargs["report_plan"].sections[0].id
        suffix = "[E]" if calls["write"] == 1 else ""
        return Report(title="T", sections=[ReportSection(
            heading="执行摘要",
            markdown=f"事实[1]{suffix}",
            coverage_ids=[plan_id],
        )])

    state = _run(
        monkeypatch,
        write_report_mock=fake_write,
        format_gate_mock=format_gate,
    )

    assert calls["write"] == 2
    assert "[E]" not in state.report.sections[0].markdown
    assert "shape_gate_failed" not in state.warnings


def test_invalid_letter_citation_is_removed_if_rewrite_repeats_it(monkeypatch):
    calls = {"write": 0}

    def fake_write(*_args, **kwargs):
        calls["write"] += 1
        plan_id = kwargs["report_plan"].sections[0].id
        return Report(title="T", sections=[ReportSection(
            heading="执行摘要",
            markdown="事实[1][E]",
            coverage_ids=[plan_id],
        )])

    state = _run(
        monkeypatch,
        write_report_mock=fake_write,
        format_gate_mock=format_gate,
    )

    assert calls["write"] == 2
    assert state.report.sections[0].markdown == "事实[1]"
    assert "shape_gate_failed" in state.warnings


def test_cancellation_not_swallowed(monkeypatch):
    """协作式取消(CancelledError 属 BaseException)必须穿透降级兜底继续上抛。"""
    boom = MagicMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        _run(monkeypatch, write_report_mock=boom)
