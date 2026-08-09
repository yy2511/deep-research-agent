"""默认 Final Research Pass 只执行一次并行补查，不能演变成串行烧预算循环。"""

import asyncio

from dra.models import (
    ReportPlan,
    ReportPlanSection,
    EvidenceCard,
    CrossWorkerAudit,
    TaskCompilation,
    NodeKind,
    NodeAssessment,
    NodeStatus,
    Report,
    ReportSection,
    ResearchPlan,
    PlanNode,
    ResearchState,
    SubAgentReport,
    ResearchTask,
    WorkerAttempt,
)
from dra.orchestrator import (
    OrchestratorConfig,
    _build_final_research_pass_fallback_task,
    _fallback_research_task_text,
    _final_research_pass_candidates,
    run_orchestrator,
)


def _node(mid: str) -> PlanNode:
    return PlanNode(
        id=mid,
        objective=f"核验 {mid}",
        kind=NodeKind.RESEARCH,
        acceptance_criteria=f"完成 {mid} 的必要公开证据覆盖",
    )


def _task(mid: str, tid: str) -> ResearchTask:
    return ResearchTask(
        id=tid,
        node_id=mid,
        objective=f"研究 {mid}",
        search_query=f"query {mid}",
    )


def test_final_research_pass_candidates_exclude_decision_plan_nodes():
    research = _node("research")
    decision = PlanNode(
        id="decision",
        objective="基于证据做选择",
        kind=NodeKind.DECISION,
        acceptance_criteria="给出有依据的选择",
    )
    research_plan = ResearchPlan(
        clarified_query="测试问题",
        initial_tasks=[_task("research", "w0")],
        plan_nodes=[research, decision],
    )
    state = ResearchState(
        query="测试问题",
        node_assessments=[
            NodeAssessment(node_id="research", status=NodeStatus.PARTIAL),
            NodeAssessment(node_id="decision", status=NodeStatus.PARTIAL),
        ],
    )

    assert [node.id for node in _final_research_pass_candidates(research_plan, state)] == ["research"]


def test_final_research_pass_candidates_include_newly_unlocked_research_node():
    """上游重试耗尽 Research Round 后，下游不能因尚无 result 被静默遗弃。"""
    upstream = _node("upstream")
    downstream = PlanNode(
        id="downstream",
        objective="基于上游结果继续研究",
        kind=NodeKind.RESEARCH,
        dependency_ids=["upstream"],
        acceptance_criteria="完成下游证据覆盖",
    )
    research_plan = ResearchPlan(
        clarified_query="测试问题",
        initial_tasks=[_task("upstream", "w0")],
        plan_nodes=[upstream, downstream],
    )
    state = ResearchState(
        query="测试问题",
        node_assessments=[NodeAssessment(
            node_id="upstream", status=NodeStatus.COMPLETE,
        )],
    )

    assert [node.id for node in _final_research_pass_candidates(research_plan, state)] == [
        "downstream",
    ]


def test_final_research_pass_excludes_all_upstream_of_completed_decision():
    """Decision 冻结后不回补其直接或间接上游，但刚解锁的下游仍可首次执行。"""
    root = _node("root")
    prepared = PlanNode(
        id="prepared",
        objective="整理候选约束",
        kind=NodeKind.RESEARCH,
        dependency_ids=[root.id],
        acceptance_criteria="形成候选约束证据",
    )
    decision = PlanNode(
        id="selection",
        objective="选择方向",
        kind=NodeKind.DECISION,
        dependency_ids=[prepared.id],
        acceptance_criteria="给出有依据的选择",
    )
    downstream = PlanNode(
        id="validation",
        objective="验证已选方向",
        kind=NodeKind.RESEARCH,
        dependency_ids=[decision.id],
        acceptance_criteria="核验已选对象",
    )
    research_plan = ResearchPlan(
        clarified_query="测试问题",
        initial_tasks=[_task(root.id, "w0")],
        plan_nodes=[root, prepared, decision, downstream],
    )
    state = ResearchState(
        query="测试问题",
        node_assessments=[
            NodeAssessment(
                node_id=root.id, status=NodeStatus.PARTIAL,
                evidence_ids=["e-root"],
            ),
            NodeAssessment(
                node_id=prepared.id, status=NodeStatus.PARTIAL,
                evidence_ids=["e-prepared"],
            ),
            NodeAssessment(
                node_id=decision.id, status=NodeStatus.COMPLETE,
                evidence_ids=["e-prepared"],
            ),
        ],
        node_activation_counts={root.id: 2, prepared.id: 2, decision.id: 1},
    )

    assert [
        node.id for node in _final_research_pass_candidates(research_plan, state)
    ] == [downstream.id]


def test_final_research_pass_prioritizes_never_activated_node_over_old_partial():
    old_partial = _node("old_partial")
    dependency = _node("decision_done")
    newly_unlocked = PlanNode(
        id="newly_unlocked",
        objective="首次核验下游",
        kind=NodeKind.RESEARCH,
        dependency_ids=["decision_done"],
        acceptance_criteria="完成下游证据覆盖",
    )
    research_plan = ResearchPlan(
        clarified_query="测试问题",
        initial_tasks=[_task("old_partial", "w0")],
        plan_nodes=[old_partial, dependency, newly_unlocked],
    )
    state = ResearchState(
        query="测试问题",
        node_assessments=[
            NodeAssessment(
                node_id="old_partial", status=NodeStatus.PARTIAL,
                evidence_ids=["e-old"],
            ),
            NodeAssessment(
                node_id="decision_done", status=NodeStatus.COMPLETE,
                evidence_ids=["e-dep"],
            ),
        ],
        node_activation_counts={"old_partial": 2, "decision_done": 1},
    )

    assert [
        node.id for node in _final_research_pass_candidates(research_plan, state)
    ] == ["newly_unlocked", "old_partial"]


def test_final_research_pass_excludes_assessment_contract_error():
    node = _node("protocol_failed")
    plan = ResearchPlan(
        clarified_query="测试问题",
        initial_tasks=[_task(node.id, "w0")],
        plan_nodes=[node],
    )
    state = ResearchState(
        query="测试问题",
        node_assessments=[NodeAssessment(
            node_id=node.id,
            status=NodeStatus.BLOCKED,
            assessment_contract_error="wrong node id",
        )],
    )

    assert _final_research_pass_candidates(plan, state) == []


def test_final_research_pass_fallback_preserves_gap_before_rich_bindings():
    decision = PlanNode(
        id="select",
        objective="选择候选方向",
        kind=NodeKind.DECISION,
        acceptance_criteria="选出可执行方向",
    )
    target = PlanNode(
        id="validate",
        objective="核验已选方向的落地可行性",
        kind=NodeKind.RESEARCH,
        dependency_ids=[decision.id],
        acceptance_criteria="逐一核验需求、成本、合规和低成本试水办法",
    )
    gap = (
        "补查乡村长者食堂兼营宴席与盒饭在正式投入厨房和场地前的低成本试水流程，"
        "包括限定天数供餐、预售登记、小批量盒饭试单及验证指标"
    )
    unrelated = [
        "村镇快递收发与农产品代收服务点",
        "特色农产品直播电商代运营与培训",
        "乡镇本地原料平价茶饮社交店",
        "乡镇居民、网购消费者和寄递农产品的农户",
        "派件服务费、寄件差价和农产品代收服务费",
    ]
    selected = "乡村长者食堂兼营宴席与盒饭"
    evidence = EvidenceCard(
        id="e-select",
        claim="上游已选定四个候选方向",
        support_quote="selected directions",
        source_url="https://example.com/select",
    )
    plan = ResearchPlan(
        clarified_query="测试问题",
        initial_tasks=[_task(target.id, "w0")],
        plan_nodes=[decision, target],
    )
    state = ResearchState(
        query="测试问题",
        evidence=[evidence],
        node_assessments=[
            NodeAssessment(
                node_id=decision.id,
                status=NodeStatus.COMPLETE,
                evidence_ids=[evidence.id],
                downstream_bindings={"selected": [unrelated[0], selected, *unrelated[1:]]},
            ),
            NodeAssessment(
                node_id=target.id,
                status=NodeStatus.PARTIAL,
                gaps=[gap],
            ),
        ],
    )

    task = _build_final_research_pass_fallback_task(
        plan, state, target, round_index=3,
    )

    assert task is not None
    assert task.objective == f"补齐证据缺口：{gap}"
    assert task.search_query == gap
    assert task.prerequisite_context == f"必须沿用的对象或参数：{selected}"
    assert task.prerequisite_evidence_ids == [evidence.id]
    assert all(value not in task.search_query for value in unrelated)


def test_fallback_without_gap_limits_binding_keyword_pile():
    node = _node("newly_unlocked")
    values = [f"候选对象{i}" for i in range(1, 7)]

    objective, query, selected = _fallback_research_task_text(node, [], values)

    assert objective == node.objective
    assert selected == values[:4]
    assert all(value in query for value in values[:4])
    assert values[4] not in query
    assert query.endswith(node.objective)


def test_final_research_pass_is_one_parallel_batch_with_one_task_per_node(monkeypatch):
    """即使 compiler 对同一 gap 给多条 task，也只能补一批且每个节点一条。"""
    initial_tasks = [_task("m1", "w0-m1"), _task("m2", "w0-m2"), _task("m3", "w0-m3")]
    research_plan = ResearchPlan(
        clarified_query="测试问题",
        initial_tasks=initial_tasks,
        plan_nodes=[_node("m1"), _node("m2"), _node("m3")],
    )
    dispatches: list[list[ResearchTask]] = []
    compiler_targets: list[list[str]] = []

    async def fake_dispatch(tasks, config, *, verbose, deadline, mission_context_by_task=None):
        dispatches.append(list(tasks))
        reports = [
            SubAgentReport(
                research_task_id=task.id,
                objective=task.objective,
                evidence=[EvidenceCard(
                    id=f"e-{task.id}",
                    claim=f"{task.node_id} 证据",
                    support_quote=f"{task.node_id} source quote",
                    source_url=f"https://example.com/{task.id}",
                )],
            )
            for task in tasks
        ]
        attempts = [WorkerAttempt(
            task_id=task.id,
            node_id=task.node_id,
            round_index=task.round_index,
            status="ok",
        ) for task in tasks]
        return reports, 0, attempts

    def fake_assess(state, _research_plan, plan_nodes, *, tasks, reports, config):
        # Round 0 与 Final Research Pass 都故意保留 partial，验证不会因此继续 R2/R3。
        state.node_assessments = [NodeAssessment(
            node_id=node.id,
            status=NodeStatus.PARTIAL,
            summary="仍有一个明确缺口",
            gaps=[f"补齐 {node.id} 的最后一项证据"],
            evidence_ids=[f"e-{task.id}" for task in tasks if task.node_id == node.id],
        ) for node in _research_plan.plan_nodes]
        return state.node_assessments

    def fake_compiler(_query, _evidence, plan_nodes, _results, **kwargs):
        compiler_targets.append([node.id for node in plan_nodes])
        # 故意给 m1 两条，Final Research Pass 必须丢掉重复 m1，保留 m2/m3 各一条。
        return TaskCompilation(
            reason="targeted gaps",
            tasks=[
                _task("m1", "r-m1-first"),
                _task("m1", "r-m1-duplicate"),
                _task("m2", "r-m2"),
                _task("m3", "r-m3"),
            ],
        )

    monkeypatch.setattr("dra.orchestrator._dispatch_task_batch", fake_dispatch)
    monkeypatch.setattr("dra.orchestrator._assess_batch", fake_assess)
    monkeypatch.setattr("dra.orchestrator.compile_ready_tasks", fake_compiler)
    monkeypatch.setattr(
        "dra.orchestrator.build_report_plan",
        lambda *args, **kwargs: ReportPlan(sections=[ReportPlanSection(heading="结论")]),
    )
    monkeypatch.setattr(
        "dra.orchestrator.run_cross_worker_audit",
        lambda *args, **kwargs: CrossWorkerAudit(has_findings=False, reason="ok"),
    )
    monkeypatch.setattr(
        "dra.orchestrator.write_report",
        lambda *args, **kwargs: Report(
            title="报告", sections=[ReportSection(heading="结论", markdown="已完成")],
        ),
    )
    monkeypatch.setattr("dra.orchestrator.format_gate", lambda *args, **kwargs: [])

    state = asyncio.run(run_orchestrator(
        "测试问题",
        config=OrchestratorConfig(
            max_research_rounds=1,
            max_tasks_per_round=3,
            max_total_tasks=5,
            total_timeout_s=None,
        ),
        verbose=False,
        research_plan=research_plan,
    ))

    assert compiler_targets == [["m1", "m2"]]  # Round 0 已用 3，residual 总预算只剩 2
    assert len(dispatches) == 2  # Round 0 + 一次 Final Research Pass，没有 R2/R3
    assert [task.node_id for task in dispatches[1]] == ["m1", "m2"]
    assert state.final_research_passes_completed == 1
    assert len(state.worker_attempts) == 5
    assert state.node_activation_counts == {"m1": 2, "m2": 2, "m3": 1}
