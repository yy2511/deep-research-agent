"""Planner admission 必须 fail-loud，不能用切片静默缩减研究覆盖。"""

from unittest.mock import MagicMock

import pytest

import dra.nodes as nodes


def _plan(n_tasks: int) -> dict:
    return {
        "plan_nodes": [{
            "id": "root",
            "objective": "核验主题",
            "kind": "research",
            "dependency_ids": [],
            "acceptance_criteria": "覆盖所有正交证据维度",
        }],
        "initial_tasks": [{
            "node_id": "root",
            "objective": f"正交任务 {index}",
            "search_query": f"query {index}",
        } for index in range(1, n_tasks + 1)],
    }


def test_over_budget_plan_is_repaired_instead_of_silently_truncated(monkeypatch):
    planner = MagicMock(side_effect=[_plan(5), _plan(3)])
    monkeypatch.setattr(nodes, "call_json", planner)

    research_plan = nodes.build_research_plan(
        "核验主题",
        max_initial_tasks=8,
        max_tasks_per_round=3,
    )

    assert [task.objective for task in research_plan.initial_tasks] == [
        "正交任务 1", "正交任务 2", "正交任务 3",
    ]
    assert planner.call_count == 2
    repair_messages = planner.call_args_list[1].args[0]
    assert sum(message["role"] == "system" for message in repair_messages) == 1
    assert "输出 5 个有效 initial_tasks" in repair_messages[-1]["content"]


def test_over_budget_plan_fails_after_single_semantic_repair(monkeypatch):
    planner = MagicMock(side_effect=[_plan(5), _plan(4)])
    monkeypatch.setattr(nodes, "call_json", planner)

    with pytest.raises(nodes.PlanValidationError, match="输出 4 个有效 initial_tasks"):
        nodes.build_research_plan(
            "核验主题",
            max_initial_tasks=8,
            max_tasks_per_round=3,
        )

    assert planner.call_count == 2
