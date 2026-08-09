"""计划节点重试预算：当上游 partial 达到激活上限时，下游应能解锁。

验证 _MAX_ACTIVATIONS_PER_NODE=2 的行为：
- 激活 1 次：不开锁
- 激活 2 次：闭锁，下游可推进
"""
from unittest.mock import MagicMock

import pytest
from dra.models import (
    DecisionOutput,
    EvidenceCard,
    NodeKind,
    NodeAssessment,
    NodeStatus,
    ResearchPlan,
    PlanNode,
    ResearchState,
    ResearchTask,
)
from dra.orchestrator import (
    OrchestratorConfig,
    _closed_node_ids,
    _degraded_dependency_ids,
    _sufficient_dep_ids,
    _dependencies_sufficient,
    _ready_research_nodes,
    _unresolved_plan_nodes,
    _MAX_ACTIVATIONS_PER_NODE,
    _resolve_ready_decisions,
    _finalize_run_status,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _research_plan(plan_nodes: list[PlanNode]) -> ResearchPlan:
    return ResearchPlan(
        clarified_query="test",
        plan_nodes=plan_nodes,
        initial_tasks=[],
    )


def _node(
    id: str,
    kind: NodeKind = NodeKind.RESEARCH,
    deps: list[str] | None = None,
) -> PlanNode:
    return PlanNode(
        id=id,
        objective=f"obj {id}",
        kind=kind,
        dependency_ids=deps or [],
        acceptance_criteria="at least 1 card",
    )


def _state(
    results: list[NodeAssessment] | None = None,
    activated: dict[str, int] | None = None,
    outputs: list[DecisionOutput] | None = None,
) -> ResearchState:
    s = ResearchState(query="test")
    if results:
        s.node_assessments = results
    if activated:
        s.node_activation_counts = activated
    if outputs:
        s.decision_outputs = outputs
    return s


def _result(
    mid: str,
    status: NodeStatus,
    *,
    evidence_ids: list[str] | None = None,
    downstream_bindings: dict[str, list[str]] | None = None,
) -> NodeAssessment:
    return NodeAssessment(
        node_id=mid,
        status=status,
        summary="test",
        evidence_ids=evidence_ids or [],
        downstream_bindings=downstream_bindings or {},
    )


# ---------------------------------------------------------------------------
# closed_node_ids
# ---------------------------------------------------------------------------

class TestClosedNodeIds:
    def test_empty_when_no_activations(self):
        b = _research_plan([_node("m1")])
        s = _state()
        assert _closed_node_ids(b, s) == set()

    def test_not_closed_with_one_activation(self):
        b = _research_plan([_node("m1")])
        s = _state(activated={"m1": 1})
        assert _closed_node_ids(b, s) == set()

    def test_closed_with_two_activations(self):
        b = _research_plan([_node("m1")])
        s = _state(
            results=[_result("m1", NodeStatus.PARTIAL)],
            activated={"m1": _MAX_ACTIVATIONS_PER_NODE},
        )
        assert _closed_node_ids(b, s) == {"m1"}

    def test_activation_without_assessment_does_not_close(self):
        b = _research_plan([_node("m1")])
        s = _state(activated={"m1": _MAX_ACTIVATIONS_PER_NODE})
        assert _closed_node_ids(b, s) == set()

    def test_closed_with_more_than_max(self):
        b = _research_plan([_node("m1")])
        s = _state(
            results=[_result("m1", NodeStatus.PARTIAL)],
            activated={"m1": 5},
        )
        assert _closed_node_ids(b, s) == {"m1"}

    def test_only_closes_over_threshold(self):
        b = _research_plan([_node("m1"), _node("m2")])
        s = _state(
            results=[
                _result("m1", NodeStatus.PARTIAL),
                _result("m2", NodeStatus.PARTIAL),
            ],
            activated={"m1": 2, "m2": 1},
        )
        assert _closed_node_ids(b, s) == {"m1"}


# ---------------------------------------------------------------------------
# sufficient_dep_ids
# ---------------------------------------------------------------------------

class TestSufficientDepIds:
    def test_complete_always_sufficient(self):
        b = _research_plan([_node("m1")])
        s = _state(results=[_result("m1", NodeStatus.COMPLETE)])
        assert "m1" in _sufficient_dep_ids(b, s)

    def test_closed_is_sufficient(self):
        b = _research_plan([_node("m1")])
        s = _state(
            results=[_result(
                "m1", NodeStatus.PARTIAL, evidence_ids=["e1"]
            )],
            activated={"m1": _MAX_ACTIVATIONS_PER_NODE},
        )
        assert "m1" in _sufficient_dep_ids(b, s)

    def test_closed_without_evidence_is_not_sufficient(self):
        b = _research_plan([_node("m1")])
        s = _state(
            results=[_result("m1", NodeStatus.PARTIAL)],
            activated={"m1": _MAX_ACTIVATIONS_PER_NODE},
        )
        assert _closed_node_ids(b, s) == {"m1"}
        assert _degraded_dependency_ids(b, s) == set()
        assert "m1" not in _sufficient_dep_ids(b, s)

    def test_blocked_with_evidence_is_not_sufficient(self):
        b = _research_plan([_node("m1")])
        s = _state(
            results=[_result(
                "m1", NodeStatus.BLOCKED, evidence_ids=["e1"]
            )],
            activated={"m1": _MAX_ACTIVATIONS_PER_NODE},
        )
        assert "m1" not in _sufficient_dep_ids(b, s)

    def test_partial_not_closed_not_sufficient(self):
        b = _research_plan([_node("m1")])
        s = _state(
            results=[_result("m1", NodeStatus.PARTIAL)],
            activated={"m1": 1},
        )
        assert "m1" not in _sufficient_dep_ids(b, s)

    def test_closed_complete_idempotent(self):
        """closed 且 complete 时 sufficient 不含重复"""
        b = _research_plan([_node("m1")])
        s = _state(
            results=[_result("m1", NodeStatus.COMPLETE)],
            activated={"m1": _MAX_ACTIVATIONS_PER_NODE},
        )
        assert _sufficient_dep_ids(b, s) == {"m1"}


# ---------------------------------------------------------------------------
# dependencies_sufficient
# ---------------------------------------------------------------------------

class TestDependenciesSufficient:
    def test_no_deps_always_ready(self):
        m = _node("m3", deps=[])
        assert _dependencies_sufficient(m, set())

    def test_all_deps_sufficient(self):
        m = _node("m3", deps=["m1", "m2"])
        assert _dependencies_sufficient(m, {"m1", "m2"})

    def test_partial_dep_not_sufficient(self):
        m = _node("m3", deps=["m1", "m2"])
        assert not _dependencies_sufficient(m, {"m1"})


# ---------------------------------------------------------------------------
# ready_research_plan_nodes — the core scheduling fix
# ---------------------------------------------------------------------------

class TestReadyPlanNodes:
    def test_returns_root_when_no_deps(self):
        """根计划节点无依赖应直接 ready"""
        b = _research_plan([_node("m1")])
        s = _state()
        ready = _ready_research_nodes(b, s)
        assert len(ready) == 1
        assert ready[0].id == "m1"

    def test_excludes_already_complete(self):
        b = _research_plan([_node("m1")])
        s = _state(results=[_result("m1", NodeStatus.COMPLETE)])
        assert _ready_research_nodes(b, s) == []

    def test_excludes_closed_node(self):
        """关闭重试的计划节点不应再被返回"""
        b = _research_plan([_node("m1")])
        s = _state(
            results=[_result(
                "m1", NodeStatus.PARTIAL, evidence_ids=["e1"]
            )],
            activated={"m1": _MAX_ACTIVATIONS_PER_NODE},
        )
        assert _ready_research_nodes(b, s) == []

    def test_downstream_unlocks_when_upstream_closed(self):
        """核心场景：上游 partial 关闭后，下游应解锁"""
        b = _research_plan([
            _node("m1"),
            _node("m2", deps=["m1"]),
        ])
        s = _state(
            results=[_result(
                "m1", NodeStatus.PARTIAL, evidence_ids=["e1"]
            )],
            activated={"m1": _MAX_ACTIVATIONS_PER_NODE},
        )
        ready = _ready_research_nodes(b, s)
        assert len(ready) == 1
        assert ready[0].id == "m2"

    def test_downstream_blocked_when_upstream_not_closed(self):
        """上游 partial 但未关闭 → 下游仍被锁，但上游自己仍可被重试"""
        b = _research_plan([
            _node("m1"),
            _node("m2", deps=["m1"]),
        ])
        s = _state(
            results=[_result("m1", NodeStatus.PARTIAL)],
            activated={"m1": 1},  # 只激活了 1 次
        )
        ready = _ready_research_nodes(b, s)
        ready_ids = {m.id for m in ready}
        assert "m1" in ready_ids, "m1 仅激活 1 次，仍可重试"
        assert "m2" not in ready_ids, "m2 的上游未 sufficient，不能解锁"

    def test_decision_downstream_unlocks_when_upstream_closed(self):
        """Decision node 的下游 research 也能解锁"""
        b = _research_plan([
            _node("m1"),
            _node("d1", kind=NodeKind.DECISION, deps=["m1"]),
            _node("m2", deps=["d1"]),
        ])
        s = _state(
            results=[
                _result("m1", NodeStatus.PARTIAL),
                _result("d1", NodeStatus.COMPLETE),
            ],
            activated={"m1": _MAX_ACTIVATIONS_PER_NODE},
        )
        ready = _ready_research_nodes(b, s)
        assert len(ready) == 1
        assert ready[0].id == "m2"

    def test_only_research_returned(self):
        """_ready_research_nodes 只返回 research 类型"""
        b = _research_plan([
            _node("m1"),
            _node("d1", kind=NodeKind.DECISION, deps=["m1"]),
        ])
        s = _state(
            results=[_result("m1", NodeStatus.COMPLETE)],
        )
        ready = _ready_research_nodes(b, s)
        # m1 is complete, d1 is decision → no research ready
        ready_ids = {m.id for m in ready}
        assert "d1" not in ready_ids, "decision 不应被 _ready_research_nodes 返回"


# ---------------------------------------------------------------------------
# decision node 关闭 → 解锁下游
# ---------------------------------------------------------------------------

class TestDecisionNodeClosing:
    def test_validated_decision_with_binding_unlocks_downstream(self):
        """确定性 Validator 落 complete 后，Decision 正常解锁下游。"""
        b = _research_plan([
            _node("m1"),
            _node("d1", kind=NodeKind.DECISION, deps=["m1"]),
            _node("m2", deps=["d1"]),
        ])
        s = _state(
            results=[
                _result("m1", NodeStatus.COMPLETE),
                _result(
                    "d1", NodeStatus.COMPLETE, evidence_ids=["e1"],
                    downstream_bindings={"selected": ["A"]},
                ),
            ],
            outputs=[DecisionOutput(
                node_id="d1",
                decision_summary="selected A",
                evidence_ids=["e1"],
                downstream_bindings={"selected": ["A"]},
            )],
        )
        closed = _closed_node_ids(b, s)
        assert "d1" in closed, "已裁决的 decision 应视为 closed"

        ready = _ready_research_nodes(b, s)
        ready_ids = {m.id for m in ready}
        assert "m2" in ready_ids, "m2 的上游 decision 已关闭，应解锁"

    def test_resolver_output_is_validated_without_second_llm(self, monkeypatch):
        root = _node("root")
        decision = _node("m4", kind=NodeKind.DECISION, deps=["root"])
        downstream = _node("m5", deps=["m4"])
        plan = _research_plan([root, decision, downstream])
        evidence = EvidenceCard(
            id="e1",
            claim="方向 A 有需求证据",
            support_quote="方向 A 有需求证据原文",
            source_url="https://example.com/e1",
        )
        state = _state(results=[NodeAssessment(
            node_id="root", status=NodeStatus.COMPLETE,
            summary="上游完成", evidence_ids=["e1"],
        )])
        state.evidence = [evidence]
        resolver = MagicMock(return_value=[DecisionOutput(
            node_id="m4",
            decision_summary="选择方向 A",
            evidence_ids=["e1"],
            downstream_bindings={"selected": ["方向 A"]},
        )])
        llm_chat = MagicMock()
        monkeypatch.setattr("dra.orchestrator.resolve_decisions", resolver)
        monkeypatch.setattr("dra.llm.chat", llm_chat)

        _resolve_ready_decisions(state, plan, OrchestratorConfig())

        assert resolver.call_count == 1
        assert llm_chat.call_count == 0
        assert state.node_activation_counts["m4"] == 1
        assert state.node_assessments[-1].status is NodeStatus.COMPLETE
        assert [node.id for node in _ready_research_nodes(plan, state)] == ["m5"]

    def test_saved_unassessed_output_resumes_at_validator(self, monkeypatch):
        root = _node("root")
        decision = _node("m4", kind=NodeKind.DECISION, deps=["root"])
        downstream = _node("m5", deps=["m4"])
        plan = _research_plan([root, decision, downstream])
        state = _state(
            results=[NodeAssessment(
                node_id="root", status=NodeStatus.COMPLETE,
                summary="上游完成", evidence_ids=["e1"],
            )],
            activated={"m4": 1},
            outputs=[DecisionOutput(
                node_id="m4",
                decision_summary="选择方向 A",
                evidence_ids=["e1"],
                downstream_bindings={"selected": ["方向 A"]},
            )],
        )
        state.evidence = [EvidenceCard(
            id="e1",
            claim="方向 A 有需求证据",
            support_quote="方向 A 有需求证据原文",
            source_url="https://example.com/e1",
        )]
        resolver = MagicMock()
        monkeypatch.setattr("dra.orchestrator.resolve_decisions", resolver)

        _resolve_ready_decisions(state, plan, OrchestratorConfig())

        resolver.assert_not_called()
        assert state.node_activation_counts["m4"] == 1
        assert state.node_assessments[-1].status is NodeStatus.COMPLETE
        assert [node.id for node in _ready_research_nodes(plan, state)] == ["m5"]

    def test_unassessed_decision_not_closed(self):
        """未裁决的 decision 不能解锁下游"""
        b = _research_plan([
            _node("m1"),
            _node("d1", kind=NodeKind.DECISION, deps=["m1"]),
            _node("m2", deps=["d1"]),
        ])
        s = _state(
            results=[_result("m1", NodeStatus.COMPLETE)],
            # d1 尚未裁决
        )
        closed = _closed_node_ids(b, s)
        assert "d1" not in closed, "未裁决的 decision 不应 closed"

        ready = _ready_research_nodes(b, s)
        ready_ids = {m.id for m in ready}
        assert "m2" not in ready_ids, "m2 的上游 decision 未裁决，不能解锁"

    def test_decision_evidence_flows_to_downstream(self):
        """已关闭 decision 的 evidence 应授权给下游"""
        from dra.orchestrator import _allowed_evidence_from_plan
        b = _research_plan([
            _node("m1"),
            _node("d1", kind=NodeKind.DECISION, deps=["m1"]),
            _node("m2", deps=["d1"]),
        ])
        d1_result = NodeAssessment(
            node_id="d1",
            status=NodeStatus.COMPLETE,
            summary="decision made",
            evidence_ids=["e1", "e2"],
            downstream_bindings={"selected": ["A"]},
        )
        s = _state(
            results=[
                _result("m1", NodeStatus.COMPLETE),
                d1_result,
            ],
            outputs=[DecisionOutput(
                node_id="d1",
                decision_summary="selected A",
                evidence_ids=["e1", "e2"],
                downstream_bindings={"selected": ["A"]},
            )],
        )
        allowed = _allowed_evidence_from_plan(s, b, [_node("m2", deps=["d1"])])
        assert "m2" in allowed
        assert "e1" in allowed["m2"], "closed decision 的 evidence 应传给下游"
        assert "e2" in allowed["m2"]
