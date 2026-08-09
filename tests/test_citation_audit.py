"""引用台账：candidate（喂了多少）vs used（实际引了多少），按研究任务分组可审计。"""
from unittest.mock import MagicMock

from dra.models import (
    EvidenceCard,
    Report,
    ReportPlan,
    ReportPlanSection,
    ReportSection,
    ResearchState,
    SubAgentReport,
)
from dra.orchestrator import _group_evidence_by_task, build_citation_audit
from dra.nodes import write_report


def _ev(n):
    return [EvidenceCard(claim=f"c{i}", support_quote=f"q{i}",
                         published_at=f"2026-0{i}-01") for i in range(n)]


def _report_plan() -> ReportPlan:
    return ReportPlan(sections=[
        ReportPlanSection(id="test-plan", heading="研究发现", covers="综合现有证据"),
    ])


def test_audit_counts_used_per_group():
    evidence = _ev(5)
    groups = [("子问题A", [1, 2, 3]), ("子问题B", [4, 5])]
    report = Report(title="t", sections=[
        ReportSection(heading="s1", markdown="结论一[1][3]。"),
        ReportSection(heading="s2", markdown="结论二[4]，越界忽略[9]。"),
    ])
    audit = build_citation_audit(report, evidence, groups)
    assert audit["n_evidence"] == 5 and audit["used_ids"] == [1, 3, 4]
    assert audit["n_candidates"] == 5
    assert audit["groups"] == [
        {"objective": "子问题A", "candidates": 3, "used": 2},
        {"objective": "子问题B", "candidates": 2, "used": 1},
    ]
    assert audit["used_ratio"] == 0.6


def test_writer_group_budget_truncates(monkeypatch):
    """max_cards_per_group=2：每组 listing 只保留日期最新的 2 张（编号不变）。"""
    import dra.nodes as nodes
    seen = {}

    def fake_call_json(messages, **kw):
        seen["user"] = messages[-1]["content"]
        return {"title": "t", "sections": [{"heading": "h", "markdown": "x[1]"}]}

    monkeypatch.setattr(nodes, "call_json", fake_call_json)
    evidence = _ev(4)
    groups = [("A", [1, 2, 3, 4])]
    report = write_report(
        "q", evidence, evidence_groups=groups, max_cards_per_group=2,
        report_plan=_report_plan(),
    )
    # published_at 随编号递增 → 保留 [3][4]，丢 [1][2]
    assert "[4]" in seen["user"] and "[3]" in seen["user"]
    assert "[1] date=" not in seen["user"]
    assert report.sections[0].markdown == "x"


def test_writer_groups_only_include_assessor_authorized_evidence():
    evidence = [
        EvidenceCard(id="e1", claim="c1", support_quote="q1"),
        EvidenceCard(id="e2", claim="c2", support_quote="q2"),
        EvidenceCard(id="e3", claim="c3", support_quote="q3"),
    ]
    state = ResearchState(
        query="q",
        evidence=evidence,
        sub_reports=[
            SubAgentReport(
                research_task_id="t1", objective="任务 A", evidence=evidence[:2],
            ),
            SubAgentReport(
                research_task_id="t2", objective="任务 B", evidence=evidence[2:],
            ),
        ],
    )

    groups = _group_evidence_by_task(
        state,
        allowed_evidence_ids={"e1", "e3"},
    )

    assert groups == [("任务 A", [1]), ("任务 B", [3])]


def test_empty_authorized_groups_do_not_fall_back_to_all_evidence(monkeypatch):
    import dra.nodes as nodes
    call = MagicMock()
    monkeypatch.setattr(nodes, "call_json", call)

    report = write_report(
        "q",
        _ev(2),
        evidence_groups=[],
        report_plan=_report_plan(),
    )

    assert report.sections == []
    call.assert_not_called()
