"""当前唯一运行契约的回归守卫。"""

import inspect
import json

import pytest
from pydantic import ValidationError

from dra.events import EVENT_SCHEMA_VERSION, EventType
from dra.memory import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointCompatibilityError,
    load_checkpoint,
    save_checkpoint,
)
from dra.models import (
    EvidenceCard,
    PlanNode,
    ReportPlan,
    ResearchPlan,
    ResearchState,
    ResearchTask,
)
from dra.nodes import write_report
from dra.orchestrator import OrchestratorConfig
from dra.subagent import SubAgentConfig
from dra.web import _payload_to_research_plan, _research_plan_to_payload


def _plan() -> ResearchPlan:
    node = PlanNode(
        id="root",
        objective="核验主题",
        acceptance_criteria="至少一条可引用证据",
    )
    return ResearchPlan(
        clarified_query="核验主题",
        plan_nodes=[node],
        initial_tasks=[ResearchTask(
            id="task-root",
            node_id="root",
            objective="寻找可引用证据",
            search_query="核验主题 官方资料",
        )],
    )


def test_deleted_and_legacy_fields_are_rejected_instead_of_silently_ignored():
    with pytest.raises(ValidationError, match="source_id"):
        EvidenceCard(claim="c", support_quote="q", source_id="legacy")
    with pytest.raises(ValidationError, match="title"):
        ReportPlan(title="legacy", sections=[])
    with pytest.raises(ValidationError, match="pending_goals"):
        ResearchPlan(
            clarified_query="q",
            plan_nodes=[],
            initial_tasks=[],
            pending_goals=[],
        )

    for deleted in (
        "milestone_recovery_stalls",
        "milestone_recovery_service_counts",
        "pending_recovery_assessment",
    ):
        with pytest.raises(ValidationError, match=deleted):
            ResearchState.model_validate({"query": "q", deleted: {}})


def test_public_plan_round_trip_accepts_only_current_payload():
    valid = {
        "clarified_query": "核验主题",
        "plan_nodes": [{
            "id": "root",
            "objective": "核验主题",
            "kind": "research",
            "dependency_ids": [],
            "acceptance_criteria": "至少一条可引用证据",
        }],
        "initial_tasks": [{
            "id": "task-root",
            "node_id": "root",
            "objective": "寻找可引用证据",
            "search_query": "核验主题 官方资料",
        }],
    }
    parsed = _payload_to_research_plan(valid)
    assert parsed is not None
    assert _research_plan_to_payload(parsed) == valid

    old_payloads = [
        {**valid, "milestones": []},
        {**valid, "sub_questions": []},
        {**valid, "planner_contract_version": "milestone-v1"},
        {
            "clarified_query": "核验主题",
            "milestones": [{
                "id": "root",
                "objective": "核验主题",
                "kind": "research",
                "depends_on_ids": [],
                "completion_criteria": "至少一条可引用证据",
            }],
            "sub_questions": [{
                "id": "task-root",
                "milestone_id": "root",
                "objective": "寻找可引用证据",
                "search_query": "核验主题 官方资料",
            }],
        },
    ]
    assert all(_payload_to_research_plan(payload) is None for payload in old_payloads)


def test_checkpoint_v11_round_trip_and_v10_fails_closed(tmp_path):
    state = ResearchState(query="核验主题", research_plan=_plan())
    config = OrchestratorConfig()
    path = save_checkpoint(state, tmp_path, config=config)
    envelope = json.loads(path.read_text(encoding="utf-8"))

    assert envelope["schema_version"] == CHECKPOINT_SCHEMA_VERSION == 11
    assert set(envelope) == {
        "schema_version", "query_hash", "plan_hash", "config_hash", "state_hash", "state",
    }
    assert load_checkpoint(tmp_path, "核验主题", config=config, expected_plan=_plan())

    envelope["schema_version"] = 10
    path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(CheckpointCompatibilityError, match="schema_version 不兼容"):
        load_checkpoint(tmp_path, "核验主题", config=config, expected_plan=_plan())


def test_only_current_worker_report_plan_and_audit_contract_remains():
    fields = OrchestratorConfig.model_fields
    assert {
        "max_initial_tasks", "max_research_rounds", "max_tasks_per_round",
        "enable_cross_worker_audit",
    } <= fields.keys()
    assert {
        "max_subquestions", "max_frontier_waves", "max_frontier_tasks_per_wave",
        "enable_global_audit", "max_replan", "enable_gap_replan", "enable_draft",
        "enable_second_opinion",
    }.isdisjoint(fields)
    assert "worker_mode" not in SubAgentConfig.model_fields
    assert inspect.signature(write_report).parameters["report_plan"].default is inspect.Parameter.empty

    event_values = {event.value for event in EventType}
    assert EVENT_SCHEMA_VERSION == 2
    assert {
        "research_plan", "task_batch_dispatched", "ready_set_computed",
        "research_round_completed", "nodes_assessed", "report_plan",
        "cross_worker_audit",
    } <= event_values
    assert {
        "brief", "dispatch", "frontier_dispatch", "frontier_advance", "frontier_done",
        "milestone_assessed", "draft", "global_audit", "replan", "second_opinion",
        "subagent_searching", "subagent_reading", "subagent_extracting",
        "subagent_reflecting", "subagent_round", "reflect",
    }.isdisjoint(event_values)
