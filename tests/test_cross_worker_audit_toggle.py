"""enable_cross_worker_audit 开关：关时不调 run_cross_worker_audit。"""

import asyncio
from unittest.mock import MagicMock

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
from dra.models import CrossWorkerAudit
from dra.orchestrator import OrchestratorConfig, run_orchestrator


def _run_with_audit_config(monkeypatch, audit_mock, *, enable: bool):
    """公共脚手架:mock 派发/裁决/写作,只让 enable_cross_worker_audit 一个变量变化。"""
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
            SubAgentReport(
                research_task_id=task.id, objective=task.objective,
                evidence=[card], status="ok",
            )
            for task in tasks
        ]
        attempts = [
            WorkerAttempt(
                task_id=task.id, node_id=task.node_id,
                round_index=task.round_index, status="ok",
            )
            for task in tasks
        ]
        return reports, 0, attempts

    def fake_assess(state, _research_plan, plan_nodes, *, tasks, reports, config):
        state.node_assessments = [
            NodeAssessment(
                node_id="m1", status=NodeStatus.COMPLETE,
                summary="ok", evidence_ids=["e1"],
            )
        ]
        return state.node_assessments

    monkeypatch.setattr("dra.orchestrator._dispatch_task_batch", fake_dispatch)
    monkeypatch.setattr("dra.orchestrator._assess_batch", fake_assess)
    monkeypatch.setattr(
        "dra.orchestrator.build_report_plan",
        lambda *a, **k: ReportPlan(sections=[
            ReportPlanSection(heading="发现", covers="主题", limitations=[]),
        ]),
    )
    monkeypatch.setattr("dra.orchestrator.run_cross_worker_audit", audit_mock)
    monkeypatch.setattr(
        "dra.orchestrator.write_report",
        lambda *a, **k: Report(
            title="t",
            sections=[ReportSection(heading="发现", markdown="正文[1]", coverage_ids=[])],
        ),
    )
    monkeypatch.setattr("dra.orchestrator.format_gate", lambda *a, **k: [])

    return asyncio.run(run_orchestrator(
        "核验主题",
        config=OrchestratorConfig(
            enable_cross_worker_audit=enable,
            max_research_rounds=1,
            total_timeout_s=None,
        ),
        research_plan=research_plan,
        verbose=False,
    ))


def test_cross_worker_audit_skipped_when_disabled(monkeypatch):
    audit = MagicMock()
    state = _run_with_audit_config(monkeypatch, audit, enable=False)
    audit.assert_not_called()
    assert state.cross_worker_audit is None
    # 显式关闭是有意行为,不该以 warning 呈现(warnings 信噪比);见 DEVLOG 2026-07-26
    assert "cross_worker_audit_skipped" not in state.warnings


def test_cross_worker_audit_runs_when_enabled(monkeypatch):
    """对称用例:开着时真调审计、结果入 state、无 skipped 告警。"""
    audit = MagicMock(return_value=CrossWorkerAudit(
        has_findings=False, reason="覆盖完整", conflicts=[],
    ))
    state = _run_with_audit_config(monkeypatch, audit, enable=True)
    audit.assert_called_once()
    assert state.cross_worker_audit is not None
    assert state.cross_worker_audit.reason == "覆盖完整"
    assert "cross_worker_audit_skipped" not in state.warnings
