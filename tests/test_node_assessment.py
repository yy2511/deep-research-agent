"""计划节点业务完成裁决与动态 task 编译。"""

from unittest.mock import MagicMock

import pytest

from dra.models import (
    DecisionOutput,
    EvidenceCard,
    NodeKind,
    NodeAssessment,
    NodeStatus,
    PlanNode,
    SubAgentReport,
    ResearchTask,
    ResearchPlan,
    ResearchState,
)
from dra.nodes import (
    _ASSESS_RESEARCH_NODES_SYSTEM,
    _COMPILE_READY_TASKS_SYSTEM,
    _RESOLVE_DECISIONS_SYSTEM,
    _assess_research_nodes_system,
    _compile_ready_tasks_system,
    _decision_response_contract_error,
    _parse_initial_tasks,
    _parse_plan_nodes,
    _resolve_decisions_system,
    render_mission_context,
    PlanValidationError,
    _clean_downstream_bindings,
    _fair_select_tasks,
    _select_decision_evidence,
    _task_compilation_contract_error,
    assess_research_nodes,
    compile_ready_tasks,
    resolve_decisions,
    validate_decision_outputs,
    revise_research_plan,
)


def _card(card_id: str, text: str) -> EvidenceCard:
    return EvidenceCard(
        id=card_id,
        claim=text,
        support_quote=f"原文：{text}",
        source_url=f"https://example.com/{card_id}",
    )


def _node(mid: str, *, kind: NodeKind = NodeKind.RESEARCH,
               depends: list[str] | None = None) -> PlanNode:
    return PlanNode(
        id=mid,
        objective=f"完成 {mid}",
        kind=kind,
        dependency_ids=depends or [],
        acceptance_criteria=f"有引用地完成 {mid}",
    )


def _task(mid: str, *, tid: str = "t1") -> ResearchTask:
    return ResearchTask(
        id=tid, node_id=mid, objective=f"研究 {mid}", search_query=f"query {mid}"
    )


def _report(task: ResearchTask, evidence: list[EvidenceCard], *, status: str = "ok") -> SubAgentReport:
    return SubAgentReport(
        research_task_id=task.id,
        objective=task.objective,
        evidence=evidence,
        status=status,
    )


def test_planner_schema_placeholders_cannot_become_real_node_ids():
    payload = {
        "plan_nodes": [{
            "id": "<generated_node_id>",
            "objective": "研究目标",
            "kind": "research",
            "dependency_ids": [],
            "acceptance_criteria": "至少一条证据",
        }],
        "initial_tasks": [{
            "node_id": "<generated_node_id>",
            "objective": "研究目标",
            "search_query": "query",
        }],
    }

    assert _parse_plan_nodes(payload) == []
    assert _parse_initial_tasks(payload) == []


def test_decision_output_separates_execution_output_from_completion_status():
    output = DecisionOutput(
        node_id="choose-db",
        decision_summary="选择 pgvector 作为当前方案",
        evidence_ids=["e1", "e2"],
        downstream_bindings={"selected_database": ["pgvector"]},
    )

    assert output.node_id == "choose-db"
    assert output.evidence_ids == ["e1", "e2"]
    assert output.downstream_bindings == {"selected_database": ["pgvector"]}
    # 决策执行产物不自带状态；完成度由确定性 Validator 写入统一账本。
    assert "status" not in output.model_dump()


def test_decision_output_collection_defaults_are_isolated():
    first = DecisionOutput(node_id="d1", decision_summary="结论一")
    second = DecisionOutput(node_id="d2", decision_summary="结论二")

    first.evidence_ids.append("e1")
    first.downstream_bindings["selected"] = ["A"]

    assert second.evidence_ids == []
    assert second.downstream_bindings == {}


def test_node_assessment_digest_default_is_isolated_from_summary():
    result = NodeAssessment(
        node_id="m1",
        status=NodeStatus.PARTIAL,
        summary="这是验收理由",
    )

    assert result.node_digest == ""


def test_legacy_checkpoint_models_default_new_protocol_fields():
    assessment = NodeAssessment.model_validate({
        "node_id": "legacy",
        "status": "partial",
        "summary": "旧 checkpoint",
    })
    state = ResearchState.model_validate({"query": "旧 checkpoint"})

    assert assessment.assessment_contract_error is None
    assert state.node_terminal_reasons == {}


def test_execution_prompts_bind_real_node_ids_without_static_m1_or_real_entity():
    static_prompts = "\n".join([
        _RESOLVE_DECISIONS_SYSTEM,
        _ASSESS_RESEARCH_NODES_SYSTEM,
        _COMPILE_READY_TASKS_SYSTEM,
    ])
    assert '"node_id": "m1"' not in static_prompts
    assert "Milvus" not in static_prompts
    assert '"node_id": "m4"' in _resolve_decisions_system("m4")
    assert '"decision_summary": "..."' not in _resolve_decisions_system("m4")
    assert '"node_id": "research_after_decision"' in _assess_research_nodes_system(
        "research_after_decision"
    )
    assert 'allowed_node_ids=["m5"]' in _compile_ready_tasks_system(["m5"])


def test_ready_task_contract_rejects_copied_schema_placeholders():
    error = _task_compilation_contract_error({
        "tasks": [{
            "node_id": "m5",
            "objective": "<填写具体研究目标>",
            "search_query": "...",
        }],
    }, {"m5"})

    assert "结构示例占位符" in error






def test_research_assessor_retries_wrong_node_id_without_new_worker(monkeypatch):
    node = _node("research_after_decision")
    task = _task(node.id)
    evidence = [_card("e1", "试点成本和合规边界已核验")]
    responses = iter([
        '{"results":[{"node_id":"m1","status":"complete",'
        '"summary":"写错节点","node_digest":"错误（证据1）。",'
        '"evidence_ids":[1],"gaps":[]}]}',
        '{"results":[{"node_id":"research_after_decision","status":"complete",'
        '"summary":"验收通过","node_digest":"试点条件已核验（证据1）。",'
        '"evidence_ids":[1],"gaps":[]}]}',
    ])
    chat = MagicMock(side_effect=lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("dra.llm.chat", chat)

    result = assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[_report(task, evidence)],
    )[0]

    assert chat.call_count == 2
    assert result.status is NodeStatus.COMPLETE
    assert result.node_digest == "试点条件已核验（证据1）。"


def test_ready_compiler_retries_node_outside_allowlist(monkeypatch):
    node = _node("m5")
    responses = iter([
        '{"reason":"错误路由","tasks":[{"node_id":"m1",'
        '"objective":"错误任务","search_query":"wrong query"}]}',
        '{"reason":"正确路由","tasks":[{"node_id":"m5",'
        '"objective":"核验试点","search_query":"pilot compliance evidence"}]}',
    ])
    chat = MagicMock(side_effect=lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("dra.llm.chat", chat)

    compiled = compile_ready_tasks(
        "q", [], [node], [], round_index=1, max_tasks=1,
    )

    assert chat.call_count == 2
    assert [task.node_id for task in compiled.tasks] == ["m5"]


def test_plan_revision_cannot_silently_rename_existing_node(monkeypatch):
    current = ResearchPlan(
        clarified_query="q",
        plan_nodes=[_node("root")],
        initial_tasks=[_task("root")],
    )
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "plan_nodes": [{
            "id": "m1",
            "objective": "完成 root，但偷偷换 ID",
            "kind": "research",
            "dependency_ids": [],
            "acceptance_criteria": "有引用地完成 root",
        }],
        "initial_tasks": [{
            "node_id": "m1",
            "objective": "研究 root",
            "search_query": "query root",
        }],
    }))

    with pytest.raises(PlanValidationError, match="擅自删除或改名"):
        revise_research_plan(current, "把验收标准写具体一点")


def test_decision_resolver_produces_summary_consistent_output_without_status(monkeypatch):
    node = _node("select", kind=NodeKind.DECISION, depends=["criteria"])
    evidence = [_card("e1", "候选 Milvus 与 Qdrant 满足代表性标准")]
    prior = [NodeAssessment(
        node_id="criteria",
        status=NodeStatus.COMPLETE,
        summary="标准已定",
        evidence_ids=["e1"],
    )]
    call = MagicMock(return_value={
        "decisions": [{
            "node_id": "select",
            "decision_summary": "选择 Milvus 与 Qdrant",
            "evidence_ids": [1],
            "downstream_bindings": {"selected": ["Milvus", "Qdrant", "Pinecone"]},
        }],
    })
    monkeypatch.setattr("dra.nodes.call_json", call)

    output = resolve_decisions(
        "q", [node], evidence, prior_results=prior,
    )[0]

    assert output.decision_summary == "选择 Milvus 与 Qdrant"
    assert output.evidence_ids == ["e1"]
    # Resolver 的一致性检查会留下 decision_summary 中的值、剔除模型夹带值。
    assert output.downstream_bindings == {"selected": ["Milvus", "Qdrant"]}
    assert "status" not in output.model_dump()
    prompt = call.call_args.args[0][1]["content"]
    system = call.call_args.args[0][0]["content"]
    assert "【待执行决策】" in prompt
    assert "dependency_ids=" not in prompt
    assert "正式引用、并继续授权给后续节点的证据集合" in system
    assert "上游节点摘要】只用于快速理解上下文" in system
    # Resolver 不能复用 build_research_plan 的旧 3000 token 预算：推理会挤掉可见 JSON。
    assert call.call_args.kwargs["max_tokens"] == 10000
    # 重型决策给单次请求完整时间，不用两个 90s 传输重试重复计算同一产物。
    assert call.call_args.kwargs["request_timeout_s"] == 180.0
    assert call.call_args.kwargs["max_retries"] == 0


def test_decision_resolver_hides_global_ids_but_maps_local_evidence(monkeypatch):
    """Prior 的全局 ID 只参与代码侧授权；模型只看并返回局部 1-based 编号。"""
    node = _node("select", kind=NodeKind.DECISION, depends=["criteria"])
    evidence = [EvidenceCard(
        id="deadbeef",
        claim="候选 Milvus 满足标准",
        support_quote="Milvus 满足已确定的筛选标准。",
        source_url="https://example.com/source",
    )]
    prior = [NodeAssessment(
        node_id="criteria",
        status=NodeStatus.COMPLETE,
        summary="标准已定",
        evidence_ids=["deadbeef"],
    )]
    call = MagicMock(return_value={
        "decisions": [{
            "node_id": "select",
            "decision_summary": "选择 Milvus",
            "evidence_ids": [1],
            "downstream_bindings": {"selected": ["Milvus"]},
        }],
    })
    monkeypatch.setattr("dra.nodes.call_json", call)

    output = resolve_decisions("q", [node], evidence, prior_results=prior)[0]

    prompt = call.call_args.args[0][1]["content"]
    assert "deadbeef" not in prompt
    assert "[1] claim=候选 Milvus 满足标准" in prompt
    assert output.evidence_ids == ["deadbeef"]


def test_decision_contract_rejects_prompt_placeholders():
    node = _node("select", kind=NodeKind.DECISION, depends=["criteria"])
    evidence = [_card("e1", "候选 Alpha 满足标准")]

    summary_error = _decision_response_contract_error({
        "decisions": [{
            "node_id": "select",
            "decision_summary": "<填写基于授权证据的决策与理由>",
            "evidence_ids": [1],
            "downstream_bindings": {},
        }],
    }, node, evidence)
    binding_error = _decision_response_contract_error({
        "decisions": [{
            "node_id": "select",
            "decision_summary": "选择 <binding_value>",
            "evidence_ids": [1],
            "downstream_bindings": {"selected": ["<binding_value>"]},
        }],
    }, node, evidence)

    assert "decision_summary 仍是提示词占位符" in summary_error
    assert "downstream_bindings 含提示词占位 value" in binding_error


def test_decision_evidence_selection_preserves_branch_quota_and_order():
    left = _node("left")
    right = _node("right")
    decision = PlanNode(
        id="decide",
        objective="综合比较两支量子芯片研究",
        kind=NodeKind.DECISION,
        dependency_ids=["left", "right"],
        acceptance_criteria="给出有证据的结论",
    )
    cards = [
        *[_card(f"left-{index}", f"左分支证据 {index}") for index in range(20)],
        *[_card(f"right-{index}", f"右分支证据 {index}") for index in range(20)],
    ]
    cards[-1].claim = "量子芯片关键参数"
    cards[-1].support_quote = "原文：量子芯片关键参数"
    prior = {
        "left": NodeAssessment(
            node_id="left", status=NodeStatus.COMPLETE,
            evidence_ids=[card.id for card in cards[:20]],
        ),
        "right": NodeAssessment(
            node_id="right", status=NodeStatus.COMPLETE,
            evidence_ids=[card.id for card in cards[20:]],
        ),
    }

    selected, remainder = _select_decision_evidence(decision, cards, prior)

    selected_ids = {card.id for card in selected}
    assert len(selected) == 30
    assert len(remainder) == 10
    assert len(selected_ids & set(prior["left"].evidence_ids)) >= 5
    assert len(selected_ids & set(prior["right"].evidence_ids)) >= 5
    assert "right-19" in selected_ids
    assert selected == [card for card in cards if card.id in selected_ids]


def test_decision_resolver_uses_digests_topk_and_stable_remainder_index(monkeypatch):
    research = _node("research")
    upstream_decision = _node("screen", kind=NodeKind.DECISION)
    decision = PlanNode(
        id="decide",
        objective="综合上游结论完成最终判断",
        kind=NodeKind.DECISION,
        dependency_ids=["research", "screen"],
        acceptance_criteria="给出一个有证据的最终结论",
    )
    cards = [
        *[_card(f"r-{index}", f"研究分支证据 {index}") for index in range(18)],
        *[_card(f"d-{index}", f"筛选分支证据 {index}") for index in range(18)],
    ]
    prior = [
        NodeAssessment(
            node_id="research",
            status=NodeStatus.COMPLETE,
            summary="research 验收通过",
            node_digest="研究分支已得出可供决策的核心结论（证据1）。",
            evidence_ids=[card.id for card in cards[:18]],
        ),
        NodeAssessment(
            node_id="screen",
            status=NodeStatus.COMPLETE,
            summary="这是旧 Decision 验收理由，不是决策结论",
            evidence_ids=[card.id for card in cards[18:]],
        ),
    ]
    call = MagicMock(return_value={"decisions": [{
        "node_id": "decide",
        "decision_summary": "最终选择 Alpha",
        "evidence_ids": [35],
        "downstream_bindings": {},
    }]})
    monkeypatch.setattr("dra.nodes.call_json", call)

    output = resolve_decisions(
        "q", [decision], cards,
        prior_results=prior,
        prior_decision_outputs=[DecisionOutput(
            node_id="screen",
            decision_summary="上游 Decision 已筛选 Alpha",
            evidence_ids=[cards[18].id],
        )],
        all_plan_nodes=[research, upstream_decision, decision],
    )[0]

    prompt = call.call_args.args[0][1]["content"]
    assert "研究分支已得出可供决策的核心结论" in prompt
    assert "上游 Decision 已筛选 Alpha" in prompt
    assert "这是旧 Decision 验收理由，不是决策结论" not in prompt
    assert prompt.count("\n    quote=") == 30
    assert "【其余授权证据索引（可引用原编号）】" in prompt
    assert "[35] claim=" in prompt
    assert cards[34].support_quote not in prompt
    assert output.evidence_ids == [cards[34].id]




def test_decision_resolver_retries_missing_nested_contract_once(monkeypatch):
    """回归 20260728-202409：合法 JSON 只有 summary、漏结构化引用时必须退回修复。"""
    node = _node("m4", kind=NodeKind.DECISION, depends=["m1"])
    evidence = [_card("e1", "AI应用研发岗位需求增长")]
    prior = [NodeAssessment(
        node_id="m1", status=NodeStatus.COMPLETE,
        summary="上游完成", evidence_ids=["e1"],
    )]
    replies = iter([
        '{"decisions":[{"node_id":"m4","decision_summary":"优先AI应用研发[1]"}]}',
        ('{"decisions":[{"node_id":"m4","decision_summary":"优先AI应用研发[1]",'
         '"evidence_ids":[1],"downstream_bindings":{}}]}'),
    ])
    calls: list[list[dict]] = []

    def fake_chat(messages, **kwargs):
        calls.append(messages)
        return next(replies)

    monkeypatch.setattr("dra.llm.chat", fake_chat)

    output = resolve_decisions(
        "q", [node], evidence, prior_results=prior,
        all_plan_nodes=[_node("m1"), node],
    )[0]

    assert len(calls) == 2
    assert output.decision_summary == "优先AI应用研发[1]"
    assert output.evidence_ids == ["e1"]
    assert output.downstream_bindings == {}
    assert output.contract_error is None
    correction = calls[1][-1]["content"]
    assert "evidence_ids" in correction and "downstream_bindings" in correction














def test_decision_resolver_repairs_full_contract_once(monkeypatch):
    criteria = _node("criteria")
    decision = _node("select", kind=NodeKind.DECISION, depends=["criteria"])
    downstream = _node("compare", depends=["select"])
    evidence = [_card("e1", "候选 Alpha 与 Beta 已完成初步核验")]
    prior = [NodeAssessment(
        node_id="criteria", status=NodeStatus.COMPLETE,
        summary="上游完成", evidence_ids=["e1"],
    )]
    replies = iter([
        '{"decisions":[{"node_id":"select","decision_summary":"选择 Alpha",'
        '"evidence_ids":[1],"downstream_bindings":{"selected":["Beta"]}}]}',
        '{"decisions":[{"node_id":"select","decision_summary":"选择 Alpha",'
        '"evidence_ids":[1],"downstream_bindings":{"selected":["Alpha"]}}]}',
    ])
    calls: list[list[dict]] = []

    def fake_chat(messages, **kwargs):
        calls.append(messages)
        return next(replies)

    monkeypatch.setattr("dra.llm.chat", fake_chat)
    output = resolve_decisions(
        "q", [decision], evidence, prior_results=prior,
        all_plan_nodes=[criteria, decision, downstream],
    )[0]

    assert len(calls) == 2
    assert "downstream_bindings 与 decision_summary 不一致" in calls[1][-1]["content"]
    assert output.contract_error is None
    assert output.downstream_bindings == {"selected": ["Alpha"]}


def test_decision_resolver_blocks_after_single_repair_is_still_illegal(monkeypatch):
    criteria = _node("criteria")
    decision = _node("select", kind=NodeKind.DECISION, depends=["criteria"])
    downstream = _node("compare", depends=["select"])
    evidence = [_card("e1", "候选 Alpha 已完成初步核验")]
    prior = [NodeAssessment(
        node_id="criteria", status=NodeStatus.COMPLETE,
        summary="上游完成", evidence_ids=["e1"],
    )]
    chat = MagicMock(return_value=(
        '{"decisions":[{"node_id":"select","decision_summary":"选择 Alpha",'
        '"evidence_ids":[1],"downstream_bindings":{}}]}'
    ))
    monkeypatch.setattr("dra.llm.chat", chat)
    output = resolve_decisions(
        "q", [decision], evidence, prior_results=prior,
        all_plan_nodes=[criteria, decision, downstream],
    )[0]

    assert chat.call_count == 2
    assert output.evidence_ids == []
    assert output.downstream_bindings == {}
    assert output.contract_error == "存在 research 下游，但 downstream_bindings 为空"


def test_deterministic_decision_validator_marks_legal_output_complete():
    criteria = _node("criteria")
    decision = _node("select", kind=NodeKind.DECISION, depends=["criteria"])
    downstream = _node("compare", depends=["select"])
    evidence = [_card("e1", "候选 Alpha 已完成初步核验")]
    prior = [NodeAssessment(
        node_id="criteria", status=NodeStatus.COMPLETE,
        summary="上游完成", evidence_ids=["e1"],
    )]
    output = DecisionOutput(
        node_id="select", decision_summary="选择 Alpha", evidence_ids=["e1"],
        downstream_bindings={"selected": ["Alpha"]},
    )

    result = validate_decision_outputs(
        [decision], [output], evidence, prior_results=prior,
        all_plan_nodes=[criteria, decision, downstream],
    )[0]

    assert result.status is NodeStatus.COMPLETE
    assert result.summary == "选择 Alpha"
    assert result.evidence_ids == ["e1"]
    assert result.downstream_bindings == {"selected": ["Alpha"]}


def test_deterministic_decision_validator_blocks_unauthorized_evidence():
    root = _node("root")
    sibling = _node("sibling")
    decision = _node("select", kind=NodeKind.DECISION, depends=["root"])
    evidence = [_card("root-e", "根分支证据"), _card("sibling-e", "兄弟分支证据")]
    prior = [
        NodeAssessment(
            node_id="root", status=NodeStatus.COMPLETE, evidence_ids=["root-e"],
        ),
        NodeAssessment(
            node_id="sibling", status=NodeStatus.COMPLETE, evidence_ids=["sibling-e"],
        ),
    ]
    output = DecisionOutput(
        node_id="select", decision_summary="选择 Alpha", evidence_ids=["sibling-e"],
    )

    result = validate_decision_outputs(
        [decision], [output], evidence, prior_results=prior,
        all_plan_nodes=[root, sibling, decision],
    )[0]

    assert result.status is NodeStatus.BLOCKED
    assert "授权域外证据" in result.summary
    assert output.contract_error == "evidence_ids 含当前 Decision 授权域外证据"


def test_clean_downstream_bindings_flattens_nested_values():
    """值摊平单测：嵌套 dict/list 取字符串叶值并去重；非标量垃圾丢弃。"""
    assert _clean_downstream_bindings({
        "selected": [{"name": "A", "meta": {"route": "r1"}}, "B", {"skip": 1}, "A"],
        "single": "C",
        "empty": [],
        "bad": 42,
    }) == {"selected": ["A", "r1", "B"], "single": ["C"]}






def test_assessor_accepts_grounded_complete_research(monkeypatch):
    node = _node("criteria")
    task = _task("criteria")
    evidence = [_card("e1", "召回率和延迟是评价指标")]
    call = MagicMock(return_value={
        "results": [{
            "node_id": "criteria",
            "status": "complete",
            "summary": "已建立召回率和延迟两个指标",
            "node_digest": "证据支持以召回率与延迟作为核心评价指标（证据1）。",
            "evidence_ids": [1],
            "downstream_bindings": {"criteria": ["召回率", "延迟"]},
        }],
    })
    monkeypatch.setattr("dra.nodes.call_json", call)

    results = assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[_report(task, evidence)]
    )

    assert results[0].status is NodeStatus.COMPLETE
    assert results[0].evidence_ids == ["e1"]
    assert results[0].node_digest == "证据支持以召回率与延迟作为核心评价指标（证据1）。"
    # research 强制 downstream_bindings={}；叙述/criteria 不得进入控制字段。
    assert results[0].downstream_bindings == {}
    # Assessor 不能复用 build_research_plan 的 3000 token 预算；先给 reasoning + JSON
    # 足够 headroom，后续再按成功样本 P95 与撞顶率决定是否收紧。
    assert call.call_args.kwargs["max_tokens"] == 10000
    system_prompt = call.call_args.args[0][0]["content"]
    assert "Writer 不直接消费" in system_prompt
    assert "正式授权给后续节点的证据集合" in system_prompt
    assert "partial 表示已有可消费证据" in system_prompt


def test_worker_mission_context_marks_node_criteria_as_aggregate_not_per_worker():
    root = PlanNode(
        id="root",
        objective="建立候选池并核验关键字段",
        acceptance_criteria="累计覆盖十个对象及其关键字段",
    )
    downstream = PlanNode(
        id="select",
        kind=NodeKind.DECISION,
        objective="从候选池中筛选两个对象",
        dependency_ids=["root"],
        acceptance_criteria="给出两个入选对象及理由",
    )
    plan = ResearchPlan(
        clarified_query="q",
        plan_nodes=[root, downstream],
        initial_tasks=[ResearchTask(
            node_id="root",
            objective="只核验其中两个对象",
            search_query="two candidates official sources",
        )],
    )

    context = render_mission_context(
        plan, [], view="worker", focus_node_id="root", activation_count=0,
    )

    assert "【所属计划节点的总体验收标准】" in context
    assert "不要求单个 Worker 独立覆盖全部验收项" in context
    assert "【本计划节点的结果将支持以下后续工作】" in context
    assert "第 0/2 次激活" not in context
    assert "[research]" not in context and "[decision]" not in context
    assert "下游依赖我的产出" not in context


def test_research_assessor_prompt_excludes_scheduler_noise(monkeypatch):
    node = _node("criteria")
    task = _task("criteria")
    evidence = [_card("e1", "召回率和延迟是评价指标")]
    call = MagicMock(return_value={
        "results": [{
            "node_id": "criteria", "status": "complete", "summary": "完成",
            "node_digest": "证据支持该评价指标（证据1）。",
            "evidence_ids": [1], "gaps": [],
        }],
    })
    monkeypatch.setattr("dra.nodes.call_json", call)

    assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[_report(task, evidence)]
    )

    prompt = call.call_args.args[0][1]["content"]
    assert "【本计划节点 task 状态】" not in prompt
    assert "【计划全貌】" not in prompt
    assert "【预算现实】" not in prompt
    assert task.id not in prompt
    assert "kind=research" not in prompt
    assert "acceptance_criteria=" in prompt


def test_research_blocked_with_cited_evidence_normalizes_to_partial(monkeypatch):
    node = _node("criteria")
    task = _task("criteria")
    evidence = [_card("e1", "已有一部分可消费证据")]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "criteria", "status": "blocked", "summary": "仍有缺口",
            "node_digest": "已有部分发现（证据1）。",
            "evidence_ids": [1], "gaps": ["补查官方延迟基准"],
        }],
    }))

    result = assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[_report(task, evidence)]
    )[0]

    assert result.status is NodeStatus.PARTIAL
    assert result.evidence_ids == ["e1"]


def test_forced_partial_keeps_deterministic_actionable_gap(monkeypatch):
    node = _node("criteria")
    task = _task("criteria")
    evidence = [_card("e1", "有证据但本批 worker 超时")]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "criteria", "status": "complete", "summary": "模型认为完成",
            "node_digest": "已有发现（证据1）。",
            "evidence_ids": [1], "gaps": [],
        }],
    }))

    result = assess_research_nodes(
        "q", [node], evidence, tasks=[task],
        reports=[_report(task, evidence, status="timeout")],
    )[0]

    assert result.status is NodeStatus.PARTIAL
    assert result.gaps == [task.objective]


def test_assessor_prompt_exposes_stored_evidence_metadata(monkeypatch):
    node = _node("criteria")
    task = _task("criteria")
    evidence = [_card("e1", "召回率和延迟是评价指标")]
    evidence[0].published_at = "2026-07-20"
    call = MagicMock(return_value={
        "results": [{
            "node_id": "criteria", "status": "complete", "summary": "完成",
            "evidence_ids": [1], "gaps": [],
        }],
    })
    monkeypatch.setattr("dra.nodes.call_json", call)

    assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[_report(task, evidence)]
    )

    prompt = call.call_args.args[0][1]["content"]
    assert "source_url=https://example.com/e1" in prompt
    assert "published_at=2026-07-20" in prompt


def test_assessor_can_complete_when_one_of_multiple_workers_is_empty(monkeypatch):
    node = _node("criteria")
    ok_task = _task("criteria", tid="ok")
    empty_task = _task("criteria", tid="empty")
    evidence = [_card("e1", "召回率和延迟是评价指标")]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "criteria", "status": "complete", "summary": "完成",
            "evidence_ids": [1], "gaps": [],
        }],
    }))

    result = assess_research_nodes(
        "q",
        [node],
        evidence,
        tasks=[ok_task, empty_task],
        reports=[_report(ok_task, evidence), _report(empty_task, [], status="empty")],
    )[0]

    assert result.status is NodeStatus.COMPLETE


@pytest.mark.parametrize("status", ["empty", "timeout", "failed"])
def test_assessor_never_completes_research_with_bad_worker(monkeypatch, status):
    node = _node("criteria")
    task = _task("criteria")
    evidence = [_card("e1", "有一条证据")]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "criteria", "status": "complete", "summary": "完成",
            "evidence_ids": [1], "downstream_bindings": {},
        }],
    }))

    result = assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[_report(task, evidence, status=status)]
    )[0]

    assert result.status is NodeStatus.PARTIAL


def test_assessor_never_completes_research_with_missing_report(monkeypatch):
    node = _node("criteria")
    task = _task("criteria")
    evidence = [_card("e1", "有一条证据")]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "criteria", "status": "complete", "summary": "完成",
            "evidence_ids": [1], "downstream_bindings": {},
        }],
    }))

    result = assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[]
    )[0]

    assert result.status is NodeStatus.PARTIAL


def test_assessor_successful_retry_can_recover_business_completion(monkeypatch):
    """历史失败是运行健康度事实，不是业务 node 的永久毒药。

    第一批失败必须降为 partial；scheduler 后续为同一 node 显式补派且
    当前批全部成功时，可用 prior evidence + 新 evidence 重新满足完成标准。
    run 级 worker_failure 的诚实 partial 由 orchestrator 单独负责。
    """
    node = _node("criteria")
    first_task = _task("criteria", tid="attempt-1")
    retry_task = _task("criteria", tid="attempt-2")
    evidence = [
        _card("e1", "召回率是评价指标"),
        _card("e2", "延迟是评价指标"),
    ]
    assessor = MagicMock(side_effect=[
        {"results": [{
            "node_id": "criteria", "status": "complete",
            "summary": "证据尚不完整", "evidence_ids": [1], "downstream_bindings": {},
        }]},
        {"results": [{
            "node_id": "criteria", "status": "complete",
            "summary": "重试后满足标准", "evidence_ids": [1, 2], "downstream_bindings": {},
        }]},
    ])
    monkeypatch.setattr("dra.nodes.call_json", assessor)

    first = assess_research_nodes(
        "q", [node], evidence[:1], tasks=[first_task],
        reports=[_report(first_task, evidence[:1], status="empty")],
    )[0]
    recovered = assess_research_nodes(
        "q", [node], evidence, tasks=[retry_task],
        reports=[_report(retry_task, evidence[1:])], prior_results=[first],
    )[0]

    assert first.status is NodeStatus.PARTIAL
    assert recovered.status is NodeStatus.COMPLETE
    assert recovered.evidence_ids == ["e1", "e2"]


def test_assessor_prompt_hides_sibling_evidence(monkeypatch):
    """物理隔离：assessor 输入不得包含兄弟分支证据全文。"""
    sibling_task = _task("sibling", tid="ts")
    own_task = _task("own", tid="to")
    sibling_card = _card("sib", "兄弟分支机密：应不可见的专有名词 XYZ_SECRET")
    own_card = _card("own", "本节点可见证据：召回率")
    evidence = [sibling_card, own_card]
    captured: list = []

    def fake_call_json(messages, **kwargs):
        captured.append(messages)
        return {"results": [{
            "node_id": "own", "status": "complete", "summary": "ok",
            "evidence_ids": [1], "downstream_bindings": {},
        }]}

    monkeypatch.setattr("dra.nodes.call_json", fake_call_json)
    result = assess_research_nodes(
        "q",
        [_node("own")],
        evidence,
        tasks=[own_task],
        reports=[
            _report(sibling_task, [sibling_card]),
            _report(own_task, [own_card]),
        ],
    )[0]

    assert result.status is NodeStatus.COMPLETE
    assert result.evidence_ids == ["own"]
    assert len(captured) == 1
    user = captured[0][1]["content"]
    assert "XYZ_SECRET" not in user
    assert "本节点可见证据" in user
    assert "兄弟分支" not in user


def test_assessor_maps_local_citation_to_global_id(monkeypatch):
    """局部 1-based 编号按 scoped 列表映射；全局列表顺序不得泄漏进编号空间。"""
    # 全局列表把 sibling 放在前面；scoped 仅 own → 局部 [1] 必须映到 own。
    sibling = _card("sib", "不该编号到的 sibling")
    own = _card("own", "合法本节点证据")
    task = _task("m1")
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "m1", "status": "complete", "summary": "ok",
            "evidence_ids": [1], "downstream_bindings": {},
        }],
    }))
    result = assess_research_nodes(
        "q", [_node("m1")], [sibling, own],
        tasks=[task], reports=[_report(task, [own])],
    )[0]
    assert result.status is NodeStatus.COMPLETE
    assert result.evidence_ids == ["own"]


@pytest.mark.parametrize(
    "raw_results",
    [
        [{
            "node_id": "other",
            "status": "complete",
            "summary": "错误节点",
            "evidence_ids": [1],
            "downstream_bindings": {},
        }],
        [
            {
                "node_id": "m1",
                "status": "complete",
                "summary": "第一条",
                "evidence_ids": [1],
                "downstream_bindings": {},
            },
            {
                "node_id": "other",
                "status": "complete",
                "summary": "多余结果",
                "evidence_ids": [1],
                "downstream_bindings": {},
            },
        ],
        [
            {
                "node_id": "m1",
                "status": "complete",
                "summary": "重复一",
                "evidence_ids": [1],
                "downstream_bindings": {},
            },
            {
                "node_id": "m1",
                "status": "complete",
                "summary": "重复二",
                "evidence_ids": [1],
                "downstream_bindings": {},
            },
        ],
    ],
    ids=["wrong-id", "multiple-results", "duplicate-id"],
)
def test_assessor_rejects_malformed_single_result_contract(monkeypatch, raw_results):
    """单 node 调用必须恰好返回一条且 ID 精确匹配。"""
    task = _task("m1")
    evidence = [_card("e1", "合法证据")]
    monkeypatch.setattr(
        "dra.nodes.call_json",
        MagicMock(return_value={"results": raw_results}),
    )

    result = assess_research_nodes(
        "q", [_node("m1")], evidence,
        tasks=[task], reports=[_report(task, evidence)],
    )[0]

    assert result.status is NodeStatus.PARTIAL
    assert result.evidence_ids == []
    assert result.downstream_bindings == {}


@pytest.mark.parametrize(
    "evidence_ids",
    [
        [1, "2"],
        [1, 0],
        [1, -1],
        [1, True],
        [1, 99],
    ],
    ids=["string", "zero", "negative", "bool", "out-of-range"],
)
def test_assessor_rejects_any_malformed_citation_item(monkeypatch, evidence_ids):
    """合法引用中混入任意非法项，也必须把 complete 降级。"""
    task = _task("m1")
    evidence = [_card("e1", "合法证据")]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "m1",
            "status": "complete",
            "summary": "混合引用",
            "evidence_ids": evidence_ids,
            "downstream_bindings": {},
        }],
    }))

    result = assess_research_nodes(
        "q", [_node("m1")], evidence,
        tasks=[task], reports=[_report(task, evidence)],
    )[0]

    assert result.status is NodeStatus.PARTIAL
    assert result.evidence_ids == ["e1"]


def test_assessor_allows_duplicate_valid_citations_and_deduplicates(monkeypatch):
    task = _task("m1")
    evidence = [_card("e1", "合法证据")]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "m1",
            "status": "complete",
            "summary": "重复但合法",
            "evidence_ids": [1, 1],
            "downstream_bindings": {},
        }],
    }))

    result = assess_research_nodes(
        "q", [_node("m1")], evidence,
        tasks=[task], reports=[_report(task, evidence)],
    )[0]

    assert result.status is NodeStatus.COMPLETE
    assert result.evidence_ids == ["e1"]


def test_assessor_rejects_forged_or_out_of_range_local_ids(monkeypatch):
    task = _task("m1")
    evidence = [_card("e1", "仅一条合法证据")]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "m1", "status": "complete", "summary": "伪造引用",
            "evidence_ids": [0, 2, 99], "downstream_bindings": {},
        }],
    }))
    result = assess_research_nodes(
        "q", [_node("m1")], evidence,
        tasks=[task], reports=[_report(task, evidence)],
    )[0]
    assert result.status is NodeStatus.PARTIAL
    assert result.evidence_ids == []




def test_assessor_sees_own_history_and_completed_ancestors(monkeypatch):
    """当前 batch + 本节点历史引用 + 已完成祖先引用均应进入局部证据清单。"""
    ancestor_card = _card("anc", "祖先证据：候选列表含 Milvus")
    own_hist = _card("hist", "本节点历史：延迟指标")
    batch_card = _card("batch", "本批新证：召回率指标")
    sibling = _card("sib", "兄弟不可见 SIB_SECRET")
    evidence = [ancestor_card, own_hist, batch_card, sibling]
    task = _task("m2", tid="t2")
    prior = [
        NodeAssessment(
            node_id="m1", status=NodeStatus.COMPLETE,
            summary="祖先完成", evidence_ids=["anc"],
        ),
        NodeAssessment(
            node_id="m2", status=NodeStatus.PARTIAL,
            summary="上次 partial", evidence_ids=["hist"],
        ),
    ]
    captured: list = []

    def fake_call_json(messages, **kwargs):
        captured.append(messages)
        return {"results": [{
            "node_id": "m2", "status": "complete", "summary": "齐了",
            "evidence_ids": [1, 2, 3], "downstream_bindings": {},
        }]}

    monkeypatch.setattr("dra.nodes.call_json", fake_call_json)
    result = assess_research_nodes(
        "q",
        [_node("m2", depends=["m1"])],
        evidence,
        tasks=[task],
        reports=[_report(task, [batch_card])],
        prior_results=prior,
    )[0]

    assert result.status is NodeStatus.COMPLETE
    assert set(result.evidence_ids) == {"anc", "hist", "batch"}
    user = captured[0][1]["content"]
    assert "祖先证据" in user
    assert "本节点历史" in user
    assert "本批新证" in user
    assert "SIB_SECRET" not in user
    # prior 文字上下文也不得泄漏无关兄弟 result
    assert "sibling" not in user.casefold()


def test_research_assessor_hides_global_ids_but_maps_local_evidence(monkeypatch):
    """Assessor 的 prior 文本不泄漏全局 ID，局部编号仍能映射回全局卡片。"""
    ancestor = EvidenceCard(
        id="deadbeef",
        claim="祖先已确定统一比较口径",
        support_quote="比较采用统一口径。",
        source_url="https://example.com/ancestor",
    )
    current = EvidenceCard(
        id="current-card",
        claim="本轮补齐延迟数据",
        support_quote="延迟数据已经补齐。",
        source_url="https://example.com/current",
    )
    node = _node("m2", depends=["m1"])
    task = _task("m2", tid="t2")
    prior = [NodeAssessment(
        node_id="m1",
        status=NodeStatus.COMPLETE,
        summary="口径已确定",
        evidence_ids=["deadbeef"],
    )]
    call = MagicMock(return_value={
        "results": [{
            "node_id": "m2",
            "status": "complete",
            "summary": "已按统一口径补齐数据",
            "evidence_ids": [1, 2],
            "gaps": [],
        }],
    })
    monkeypatch.setattr("dra.nodes.call_json", call)

    result = assess_research_nodes(
        "q",
        [node],
        [ancestor, current],
        tasks=[task],
        reports=[_report(task, [current])],
        prior_results=prior,
    )[0]

    prompt = call.call_args.args[0][1]["content"]
    assert "deadbeef" not in prompt
    assert "[1] claim=祖先已确定统一比较口径" in prompt
    assert result.evidence_ids == ["deadbeef", "current-card"]


def test_assessor_calls_llm_once_per_node(monkeypatch):
    """每 node 独立一次 LLM；不得再把多节点塞进同一次 prompt。"""
    m1 = _node("m1")
    m2 = _node("m2")
    t1, t2 = _task("m1", tid="t1"), _task("m2", tid="t2")
    e1, e2 = _card("e1", "证据一"), _card("e2", "证据二")
    calls: list = []

    def fake_call_json(messages, **kwargs):
        calls.append(messages[1]["content"])
        # node id 只存在最高优先级输出契约，不再作为业务上下文噪声重复到 user prompt。
        system = messages[0]["content"]
        mid = "m1" if 'expected_node_id="m1"' in system else "m2"
        return {"results": [{
            "node_id": mid, "status": "complete", "summary": "ok",
            "evidence_ids": [1], "downstream_bindings": {},
        }]}

    monkeypatch.setattr("dra.nodes.call_json", fake_call_json)
    results = assess_research_nodes(
        "q", [m1, m2], [e1, e2],
        tasks=[t1, t2],
        reports=[_report(t1, [e1]), _report(t2, [e2])],
    )
    assert len(calls) == 2
    assert all(r.status is NodeStatus.COMPLETE for r in results)
    # 第一次调用不得同时出现另一节点的证据全文作为可引用材料
    assert "证据二" not in calls[0] or "证据一" in calls[0]
    assert "证据一" not in calls[1] or "证据二" in calls[1]
    assert "证据二" not in calls[0]
    assert "证据一" not in calls[1]


def test_ready_set_compiler_derives_downstream_binding_from_complete_results(monkeypatch):
    ready = _node("compare", depends=["select"])
    evidence = [_card("e1", "候选 Milvus 与 Qdrant 满足代表性标准")]
    completed = [NodeAssessment(
        node_id="select", status=NodeStatus.COMPLETE,
        summary="已选择 Milvus 与 Qdrant", evidence_ids=["e1"],
        downstream_bindings={"selected": ["Milvus", "Qdrant"]},
    )]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "reason": "选择结果已确定",
        "tasks": [{
            "node_id": "compare",
            "objective": "比较 Milvus 与 Qdrant",
            "search_query": "Milvus Qdrant benchmark scalability operations",
            "prerequisite_context": "模型夹带的自由结论",
            "evidence_ids": [999],
        }],
    }))

    advance = compile_ready_tasks(
        "q", evidence, [ready], completed, round_index=1, max_tasks=5
    )

    task = advance.tasks[0]
    assert task.node_id == "compare"
    assert task.prerequisite_context == "必须沿用的对象或参数：Milvus；Qdrant"
    assert "候选 Milvus 与 Qdrant 满足代表性标准" not in task.prerequisite_context
    assert task.prerequisite_evidence_ids == ["e1"]
    assert "模型夹带" not in task.prerequisite_context
    assert {task.node_id for task in advance.tasks} == {"compare"}


def test_ready_set_context_uses_grounded_digest_not_freeform_assessor_summary(monkeypatch):
    ready = _node("compare", depends=["criteria"])
    evidence = [_card("e1", "召回率和延迟是评价指标")]
    completed = [NodeAssessment(
        node_id="criteria",
        status=NodeStatus.COMPLETE,
        summary="已选择并确认 Milvus 是唯一优胜者",
        node_digest="召回率和延迟是评价指标（证据1）",
        evidence_ids=["e1"],
    )]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "tasks": [{
            "node_id": "compare",
            "objective": "继续研究评价体系",
            "search_query": "vector database recall latency benchmark",
        }],
    }))

    advance = compile_ready_tasks(
        "q", evidence, [ready], completed, round_index=1, max_tasks=5
    )

    context = advance.tasks[0].prerequisite_context
    assert "召回率和延迟是评价指标" in context
    assert "证据1" not in context
    assert "上游证据" not in context
    assert "Milvus" not in context


def test_ready_set_projects_only_task_binding_context(monkeypatch):
    ready = _node("validate", depends=["select"])
    evidence = [
        _card("e1", "Alpha 的原始候选证据"),
        _card("e2", "Beta 的原始候选证据"),
    ]
    completed = [NodeAssessment(
        node_id="select",
        status=NodeStatus.COMPLETE,
        summary=(
            "入选 Alpha。目标客户是客户 A，采用订阅收费。\n\n"
            "入选 Beta。目标客户是客户 B，采用项目收费。"
        ),
        evidence_ids=["e1", "e2"],
        downstream_bindings={
            "selected": ["Alpha", "Beta"],
            "target_customers": ["客户 A", "客户 B"],
        },
    )]
    captured: list = []

    def fake_call_json(messages, **kwargs):
        captured.append(messages)
        return {"tasks": [{
            "node_id": "validate",
            "objective": "验证 Alpha 的收费与风险",
            "search_query": "Alpha pricing risk 2026",
        }]}

    monkeypatch.setattr("dra.nodes.call_json", fake_call_json)
    advance = compile_ready_tasks(
        "q", evidence, [ready], completed, round_index=1, max_tasks=5,
    )

    compiler_input = captured[0][1]["content"]
    assert "入选 Alpha" in compiler_input
    assert "入选 Beta" in compiler_input
    assert "Alpha 的原始候选证据" not in compiler_input
    context = advance.tasks[0].prerequisite_context
    assert context == "必须沿用的对象或参数：Alpha"
    assert "Beta" not in context
    assert "客户 B" not in context


def test_ready_set_ignores_decision_summary_format_for_worker_context(monkeypatch):
    """Decision 自由文本无论如何排版，Worker 都只接收命中的 binding。"""
    ready = _node("validate", depends=["select"])
    evidence = [_card("e1", "Alpha 与 Beta 均已入选")]
    completed = [NodeAssessment(
        node_id="select",
        status=NodeStatus.COMPLETE,
        summary=(
            "筛选两个方向：1. Alpha：面向客户 A，价格100美元。"
            "优势是交付快，风险是需要人工复核。[1][2] "
            "2. Beta：面向客户 B，价格200美元。"
            "优势是可扩展，风险是获客较慢。[3] "
            "综合取舍后选择 Alpha 与 Beta；共同要求遵守数据合规。[4]"
        ),
        evidence_ids=["e1"],
        downstream_bindings={
            "selected_directions": ["Alpha", "Beta"],
            "alpha_price": ["100美元"],
            "beta_price": ["200美元"],
        },
    )]

    def fake_call_json(messages, **kwargs):
        return {"tasks": [{
            "node_id": "validate",
            "objective": "核验 Alpha 的100美元价格和交付风险",
            "search_query": "Alpha 100美元 pricing delivery risk",
        }]}

    monkeypatch.setattr("dra.nodes.call_json", fake_call_json)
    advance = compile_ready_tasks(
        "q", evidence, [ready], completed, round_index=1, max_tasks=5,
    )

    context = advance.tasks[0].prerequisite_context
    assert context == "必须沿用的对象或参数：Alpha；100美元"
    assert "Beta" not in context
    assert "客户 B" not in context
    assert "200美元" not in context
    assert "综合取舍" not in context
    assert "上游证据" not in context


def test_ready_set_rejects_task_with_price_but_no_primary_selected_object(monkeypatch):
    """多对象 Decision 下只命中价格仍不够，task 必须点名一个入选对象。"""
    ready = _node("validate", depends=["select"])
    evidence = [_card("e1", "Alpha 与 Beta 均已入选")]
    completed = [NodeAssessment(
        node_id="select",
        status=NodeStatus.COMPLETE,
        summary="选择 Alpha 与 Beta",
        evidence_ids=["e1"],
        downstream_bindings={
            "selected_directions": ["Alpha", "Beta"],
            "alpha_price": ["100美元"],
        },
    )]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "tasks": [{
            "node_id": "validate",
            "objective": "核验100美元价格的市场依据",
            "search_query": "100美元 pricing evidence",
        }],
    }))

    advance = compile_ready_tasks(
        "q", evidence, [ready], completed, round_index=1, max_tasks=5,
    )

    assert advance.tasks == []
    assert "1 条 task 因未含必须继承的对象或参数被丢弃" in advance.reason


def test_ready_set_compiler_rejects_task_that_drops_grounded_entity_bindings(monkeypatch):
    """绑定后的对象是执行契约；compiler 不能换成另一个看似合理的实体。"""
    ready = _node("compare", depends=["select"])
    evidence = [_card("e1", "基于评价结果选择 Milvus 与 Qdrant")]
    completed = [NodeAssessment(
        node_id="select",
        status=NodeStatus.COMPLETE,
        summary="自由摘要不可信",
        evidence_ids=["e1"],
        downstream_bindings={"selected": ["Milvus", "Qdrant"]},
    )]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "tasks": [{
            "node_id": "compare",
            "objective": "比较另一个产品",
            "search_query": "Pinecone benchmark latency throughput",
        }],
    }))

    advance = compile_ready_tasks(
        "q", evidence, [ready], completed, round_index=1, max_tasks=5,
    )

    assert advance.tasks == []


def test_ready_set_compiler_batch_union_covers_all_downstream_binding_values(monkeypatch):
    """多 task 可分别覆盖不同实体；并集齐全才激活。"""
    ready = _node("compare", depends=["select"])
    evidence = [_card("e1", "选择了 Milvus 与 Qdrant")]
    completed = [NodeAssessment(
        node_id="select", status=NodeStatus.COMPLETE,
        summary="ok", evidence_ids=["e1"],
        downstream_bindings={"selected": ["Milvus", "Qdrant"]},
    )]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "tasks": [
            {
                "node_id": "compare",
                "objective": "查 Milvus",
                "search_query": "Milvus benchmark latency",
            },
            {
                "node_id": "compare",
                "objective": "查 Qdrant",
                "search_query": "Qdrant benchmark latency",
            },
        ],
    }))
    advance = compile_ready_tasks(
        "q", evidence, [ready], completed, round_index=1, max_tasks=5,
    )
    assert len(advance.tasks) == 2
    assert {task.node_id for task in advance.tasks} == {"compare"}


def test_ready_set_compiler_keeps_partial_downstream_binding_batch_without_false_gap(monkeypatch):
    """只覆盖部分 binding 时保留进展，但不把所有属性误报成正式 gaps。"""
    ready = _node("compare", depends=["select"])
    evidence = [_card("e1", "选择了 Milvus 与 Qdrant")]
    completed = [NodeAssessment(
        node_id="select", status=NodeStatus.COMPLETE,
        summary="ok", evidence_ids=["e1"],
        downstream_bindings={"selected": ["Milvus", "Qdrant"]},
    )]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "tasks": [{
            "node_id": "compare",
            "objective": "只查 Milvus",
            "search_query": "Milvus benchmark latency",
        }],
    }))
    advance = compile_ready_tasks(
        "q", evidence, [ready], completed, round_index=1, max_tasks=5,
    )
    assert len(advance.tasks) == 1
    assert {task.node_id for task in advance.tasks} == {"compare"}
    assert "Qdrant" not in advance.reason
    assert advance.tasks[0].prerequisite_context == "必须沿用的对象或参数：Milvus"


def test_fair_selector_prevents_one_ready_node_from_taking_every_slot():
    candidates = [
        _task("m1", tid="m1-a"),
        _task("m1", tid="m1-b"),
        _task("m1", tid="m1-c"),
        _task("m2", tid="m2-a"),
        _task("m2", tid="m2-b"),
    ]

    selected = _fair_select_tasks(candidates, ["m1", "m2"], 4)

    assert [task.node_id for task in selected] == ["m1", "m2", "m1", "m2"]


def test_ready_set_compiler_binding_word_boundary(monkeypatch):
    """短实体 Go 不能被无关的 Google 前缀冒充为已覆盖 binding。"""
    ready = _node("compare", depends=["select"])
    evidence = [_card("e1", "Go is the selected language")]
    completed = [NodeAssessment(
        node_id="select", status=NodeStatus.COMPLETE,
        summary="自由摘要不可信", evidence_ids=["e1"],
        downstream_bindings={"selected": ["Go"]},
    )]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "tasks": [{
            "node_id": "compare",
            "objective": "Benchmark Google database",
            "search_query": "Google database benchmark latency",
        }],
    }))

    advance = compile_ready_tasks(
        "q", evidence, [ready], completed, round_index=1, max_tasks=5,
    )

    assert advance.tasks == []


def test_ready_set_compiler_does_not_activate_downstream_of_partial_result(monkeypatch):
    ready = _node("compare", depends=["select"])
    partial = [NodeAssessment(
        node_id="select", status=NodeStatus.PARTIAL,
        summary="尚未选定", evidence_ids=["e1"],
    )]
    call = MagicMock()
    monkeypatch.setattr("dra.nodes.call_json", call)

    advance = compile_ready_tasks(
        "q", [_card("e1", "候选仍待筛选")], [ready], partial,
        round_index=1, max_tasks=5,
    )

    call.assert_not_called()
    assert advance.tasks == []


def test_research_assessor_parses_gaps_on_partial_and_clears_on_complete(monkeypatch):
    """partial 必须落账 gaps 供 compiler 补查；complete 强制 gaps=[]。"""
    node = _node("criteria")
    task = _task("criteria")
    evidence = [_card("e1", "召回率是评价指标")]
    assessor = MagicMock(side_effect=[
        {"results": [{
            "node_id": "criteria",
            "status": "partial",
            "summary": "缺延迟指标",
            "evidence_ids": [1],
            "gaps": ["缺少延迟指标的公开基准来源", "  缺少延迟指标的公开基准来源  ", "", 12],
        }]},
        {"results": [{
            "node_id": "criteria",
            "status": "complete",
            "summary": "覆盖完成",
            "evidence_ids": [1],
            "gaps": ["不应保留的缺口"],
        }]},
    ])
    monkeypatch.setattr("dra.nodes.call_json", assessor)

    partial = assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[_report(task, evidence)],
    )[0]
    complete = assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[_report(task, evidence)],
    )[0]

    assert partial.status is NodeStatus.PARTIAL
    assert partial.gaps == ["缺少延迟指标的公开基准来源"]
    assert complete.status is NodeStatus.COMPLETE
    assert complete.gaps == []


def test_ready_set_compiler_surfaces_known_gaps_for_retry(monkeypatch):
    """root research 重试时，gaps 只进 compiler；Worker 继承紧凑 digest。"""
    ready = _node("criteria")
    evidence = [_card("e1", "已有召回率指标证据")]
    prior = [NodeAssessment(
        node_id="criteria",
        status=NodeStatus.PARTIAL,
        summary="尚缺延迟",
        node_digest="已有召回率指标证据（证据1），仍需补齐延迟与来源机构。",
        gaps=["需要延迟指标的公开基准", "需要指标来源机构说明"],
        evidence_ids=["e1"],
    )]
    captured: list = []

    def fake_call_json(messages, **kwargs):
        captured.append(messages)
        return {
            "reason": "对准 known_gaps 补查",
            "tasks": [{
                "node_id": "criteria",
                "objective": "补齐延迟指标与来源机构",
                "search_query": "vector database latency benchmark source",
            }],
        }

    monkeypatch.setattr("dra.nodes.call_json", fake_call_json)
    advance = compile_ready_tasks(
        "q", evidence, [ready], prior, round_index=1, max_tasks=5,
    )

    assert len(captured) == 1
    user = captured[0][1]["content"]
    assert "【需要补齐的证据】" in user
    assert "需要延迟指标的公开基准" in user
    assert "需要指标来源机构说明" in user
    assert "已有召回率指标证据" in user
    assert "evidence_claims=" not in user
    assert advance.tasks
    context = advance.tasks[0].prerequisite_context
    assert "已有召回率指标证据" in context
    assert "需要延迟指标的公开基准" not in context


# ---------------------------------------------------------------------------
# 统一完成判定口径回归（2026-07-15）。
#
# 设计意图：完成判定的唯一语义来源是 assessor 依据 acceptance_criteria 给的
# 判定；binding 的逐字 grounding 不再作为完成否决项，只把不接地的 binding 字段
# 清空用于展示。防 decision 瞎编对象由「decision 必须有 binding 产出」兜底——
# 瞎编的 binding 被清空 → 触发无 binding → partial。research 计划节点不要求
# binding 产出，所以模型顺手附的不接地 binding 不再误伤完成。所有题型走同一
# 条判定路径，无题型分支，避免针对某类题打补丁引入其他题型的 bug。
# ---------------------------------------------------------------------------


def test_research_complete_survives_ungrounded_adventitious_binding(monkeypatch):
    """今天 20260714 run 的真复现：纯 research node、worker 全 ok、证据满足
    criteria，但 model 顺手附了一坨不接地 binding。旧逻辑把 complete 误降为
    partial，导致系统永不开闸、假性烧光 Research Round 预算。A 口径：binding 既不清空也
    不否决完成，原样透传；status 保持 complete。防瞎编靠 LLM 按 criteria 自判，
    该夹具覆盖证据不足时不得将推测写入 downstream_binding 的边界。
    """
    node = PlanNode(
        id="m1",
        objective="调研评测指标体系",
        kind=NodeKind.RESEARCH,
        dependency_ids=[],
        acceptance_criteria="输出包含至少3-5个核心评测维度",
    )
    task = ResearchTask(
        id="t1", node_id="m1", objective="检索评测指标",
        search_query="deep research agent evaluation metrics",
    )
    evidence = [_card("e1", "FACT 框架评估引用准确性"),
                 _card("e2", "TRACE 量化轨迹效用")]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "m1", "status": "complete",
            "summary": "覆盖6个评测维度，满足3-5要求",
            "evidence_ids": [1, 2],
            "downstream_bindings": {"core_task_designs": ["1266 high-difficulty questions"],
                         "publishing_institutions": ["OpenAI"]},
        }],
    }))

    result = assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[_report(task, evidence)],
    )[0]

    assert result.status is NodeStatus.COMPLETE, (
        "纯 research node 不应因 model 顺手附的叙述字段被降级")
    assert result.evidence_ids == ["e1", "e2"]
    # research 强制清空 downstream_binding；叙述性字段不得进入控制面。
    assert result.downstream_bindings == {}


def test_research_without_any_binding_completes_normally(monkeypatch):
    """research 没附 binding 时照常 complete。"""
    node = _node("m1")
    task = _task("m1")
    evidence = [_card("e1", "召回率是评价指标")]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "results": [{
            "node_id": "m1", "status": "complete",
            "summary": "满足", "evidence_ids": [1], "downstream_bindings": {},
        }],
    }))

    result = assess_research_nodes(
        "q", [node], evidence, tasks=[task], reports=[_report(task, evidence)],
    )[0]
    assert result.status is NodeStatus.COMPLETE


















def test_research_assessor_rejects_decision_node_before_llm(monkeypatch):
    """旧混合入口已删除：Decision 不得重新进入 research assessor。"""
    call = MagicMock()
    monkeypatch.setattr("dra.nodes.call_json", call)

    with pytest.raises(ValueError, match="只接受 research plan_nodes"):
        assess_research_nodes(
            "q",
            [_node("select", kind=NodeKind.DECISION, depends=["root"])],
            [],
            tasks=[],
            reports=[],
        )

    call.assert_not_called()


# ---------------------------------------------------------------------------
# 批次2 护栏：digest 剥号 / Resolver 下游视图 / blocked 归一化 / 内部布尔不暴露
# ---------------------------------------------------------------------------


def test_upstream_digest_scoped_numbers_stripped_before_resolver(monkeypatch):
    """上游 digest 里的「证据N」是上游自己的 scoped 编号，投影前剥掉，防 Resolver 误当自己编号引用。"""
    research = _node("research")
    decision = _node("decide", kind=NodeKind.DECISION, depends=["research"])
    cards = [_card("e1", "研究分支证据")]
    prior = [NodeAssessment(
        node_id="research", status=NodeStatus.COMPLETE,
        summary="验收通过",
        node_digest="研究分支核心结论（证据3、4、5、6）[4]。另有 evidence 7, 8 佐证。",
        evidence_ids=["e1"],
    )]
    call = MagicMock(return_value={"decisions": [{
        "node_id": "decide", "decision_summary": "选择 Alpha",
        "evidence_ids": [1], "downstream_bindings": {},
    }]})
    monkeypatch.setattr("dra.nodes.call_json", call)

    resolve_decisions(
        "q", [decision], cards,
        prior_results=prior,
        all_plan_nodes=[research, decision],
    )[0]

    prompt = call.call_args.args[0][1]["content"]
    assert "研究分支核心结论" in prompt
    assert "证据3" not in prompt
    assert "evidence 7" not in prompt
    assert "[4]" not in prompt
    assert "（、4、5、6）" not in prompt
    assert "[上游证据]" not in prompt


def test_upstream_digest_reference_cleanup_preserves_business_years(monkeypatch):
    """证据编号链可删除，但紧邻的年份/业务数字不能被误当作后续编号。"""
    research = _node("research")
    decision = _node("decide", kind=NodeKind.DECISION, depends=["research"])
    cards = [_card("e1", "研究分支证据")]
    prior = [NodeAssessment(
        node_id="research", status=NodeStatus.COMPLETE,
        summary="验收通过",
        node_digest="核心结论（证据2）；2026年收入达到8500美元。",
        evidence_ids=["e1"],
    )]
    call = MagicMock(return_value={"decisions": [{
        "node_id": "decide", "decision_summary": "选择 Alpha",
        "evidence_ids": [1], "downstream_bindings": {},
    }]})
    monkeypatch.setattr("dra.nodes.call_json", call)

    resolve_decisions(
        "q", [decision], cards,
        prior_results=prior,
        all_plan_nodes=[research, decision],
    )[0]

    prompt = call.call_args.args[0][1]["content"]
    assert "证据2" not in prompt
    assert "2026年收入达到8500美元" in prompt


def test_decision_resolver_sees_downstream_research_scope(monkeypatch):
    """Resolver 能看到下游 research 节点的 objective/criteria，binding 才有依据而不是靠猜。"""
    research_up = _node("research_up")
    research_down = _node("research_after", kind=NodeKind.RESEARCH)
    decision = _node("decide", kind=NodeKind.DECISION, depends=["research_up"])
    research_down.dependency_ids = ["decide"]
    cards = [_card("e1", "授权证据")]
    prior = [NodeAssessment(
        node_id="research_up", status=NodeStatus.COMPLETE,
        summary="上游完成", evidence_ids=["e1"],
    )]
    call = MagicMock(return_value={"decisions": [{
        "node_id": "decide", "decision_summary": "选择 Alpha",
        "evidence_ids": [1], "downstream_bindings": {"selected": ["Alpha"]},
    }]})
    monkeypatch.setattr("dra.nodes.call_json", call)

    resolve_decisions(
        "q", [decision], cards,
        prior_results=prior,
        all_plan_nodes=[research_up, decision, research_down],
    )[0]

    prompt = call.call_args.args[0][1]["content"]
    assert "【后续研究需要（downstream_bindings 的取值依据）】" in prompt
    assert "完成 research_after" in prompt
    assert "有引用地完成 research_after" in prompt
    assert "[research_after]" not in prompt






def test_ready_set_compiler_records_dropped_tasks_in_reason(monkeypatch):
    """binding 硬闸丢弃的 task 记进 reason，不再静默。"""
    ready = _node("compare", depends=["select"])
    evidence = [_card("e1", "选择了 Milvus 与 Qdrant")]
    completed = [NodeAssessment(
        node_id="select", status=NodeStatus.COMPLETE,
        summary="自由摘要不可信", evidence_ids=["e1"],
        downstream_bindings={"selected": ["Milvus", "Qdrant"]},
    )]
    monkeypatch.setattr("dra.nodes.call_json", MagicMock(return_value={
        "reason": "已完成编译",
        "tasks": [{
            "node_id": "compare",
            "objective": "比较另一个产品",
            "search_query": "Pinecone benchmark latency throughput",
        }],
    }))

    advance = compile_ready_tasks(
        "q", evidence, [ready], completed, round_index=1, max_tasks=5,
    )

    assert advance.tasks == []
    assert "1 条 task 因未含必须继承的对象或参数被丢弃" in advance.reason
    assert "compare" in advance.reason
