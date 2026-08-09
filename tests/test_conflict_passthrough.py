"""worker 申报矛盾透传:finish 契约无 card_ids,不能整条丢弃(2026-07-26 P0 修复)。

背景:tool-loop 的 finish schema 只有 dimension/description/severity,
_parse_conflicts 恒产 card_ids=[];_remap_subagent_conflicts 旧规则
「new_ids 为空 → 丢弃」导致 worker 申报的矛盾 100% 到不了 writer。
"""
from dra.models import Conflict, EvidenceCard, CrossWorkerAudit, ResearchState, SubAgentReport
from dra.orchestrator import _dedupe_conflicts, _remap_subagent_conflicts


def _card(i: int) -> EvidenceCard:
    return EvidenceCard(id=f"e{i}", claim=f"事实{i}", support_quote="原文",
                        source_url=f"https://example.com/{i}")


def _report(evidence, conflicts) -> SubAgentReport:
    return SubAgentReport(research_task_id="t1", objective="调查",
                          evidence=evidence, conflicts=conflicts)


def test_worker_conflict_without_anchors_passes_through():
    """无锚点(card_ids=[])的 worker 矛盾原样透传——这是当前 finish 契约的唯一形态。"""
    cards = [_card(1)]
    rep = _report(cards, [Conflict(
        dimension="融资规模口径", card_ids=[],
        description="来源 A 称 up to $1.4B,来源 B 给出不同数字", severity="high",
    )])
    state = ResearchState(query="q", evidence=cards, sub_reports=[rep])

    out = _remap_subagent_conflicts(state, None)

    assert len(out) == 1
    assert out[0].dimension == "融资规模口径"
    assert out[0].card_ids == []
    assert out[0].severity == "high"


def test_anchored_conflict_remaps_local_to_global_positions():
    """带锚点的矛盾仍走局部→全局重映射(修张冠李戴的原有行为不回退)。"""
    other, a, b = _card(0), _card(1), _card(2)
    rep = _report([a, b], [Conflict(dimension="d", card_ids=[1, 2], description="x")])
    state = ResearchState(query="q", evidence=[other, a, b], sub_reports=[rep])

    out = _remap_subagent_conflicts(state, None)

    assert len(out) == 1
    assert out[0].card_ids == [2, 3]   # 局部 [1,2] → 全局位置 [2,3]


def test_unresolvable_anchor_still_dropped():
    """锚点全部越界/被去重删除的矛盾仍丢弃(引用的是真重复卡,原兜底语义保留)。"""
    a = _card(1)
    rep = _report([a], [Conflict(dimension="d", card_ids=[99], description="x")])
    state = ResearchState(query="q", evidence=[a], sub_reports=[rep])

    assert _remap_subagent_conflicts(state, None) == []


def test_unanchored_conflicts_dedupe_by_description():
    """无锚点矛盾按 (dimension, description) 去重:同维度不同矛盾都保留,逐字重复合并。"""
    conflicts = [
        Conflict(dimension="口径", card_ids=[], description="A vs B"),
        Conflict(dimension="口径", card_ids=[], description="C vs D"),
        Conflict(dimension="口径", card_ids=[], description="A vs B"),
    ]
    out = _dedupe_conflicts(conflicts)
    assert len(out) == 2
    assert {c.description for c in out} == {"A vs B", "C vs D"}


def test_audit_conflicts_still_appended():
    """跨 Worker 审查的矛盾(已是全局编号)原样追加,不受透传规则影响。"""
    cards = [_card(1)]
    rep = _report(cards, [Conflict(dimension="w", card_ids=[], description="worker 侧")])
    audit = CrossWorkerAudit(has_findings=True, reason="r", conflicts=[
        Conflict(dimension="g", card_ids=[1], description="审计侧"),
    ])
    state = ResearchState(query="q", evidence=cards, sub_reports=[rep])

    out = _remap_subagent_conflicts(state, audit)

    assert {c.dimension for c in out} == {"w", "g"}
