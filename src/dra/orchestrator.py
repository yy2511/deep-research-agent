"""V1 架构升级：Orchestrator-worker 主控制器。

负责：
1. build_research_plan 生成 typed node DAG 与当前可执行的初始 ReadySet。
2. 每个 Research Round 内用 asyncio.gather 并发 dispatch，多 Research Round 间按证据动态推进。
3. 收集 SubAgentReport，把所有证据合并到全局 ResearchState。
4. 计划内 Research Round 完成后做一次跨 Worker 审查，收集覆盖风险与矛盾。
5. write_report 综合多研究任务证据成报告。

设计要点：
- 并发数硬上限（默认 ≤ 5）：避免 token 爆炸、Tavily 限流；
  超过 max_concurrent 时用 Semaphore 限流，仍可处理更多研究任务。
- 不引 LangGraph：asyncio 单机够用，手写编排更透明（V0 决策延续）。
- 任一子代理失败不应拖垮整个研究：用 return_exceptions=True 收集异常，
  失败的子代理记录到 status，不抛出。
- Research Round/ReadySet：node_assessments 是唯一控制流真相源。
- checkpoint 在每个完整 Research Round 后落盘；崩在 Research Round 中间可能 at-least-once 重跑该 Research Round，
  不宣称 exactly-once durable execution。
"""

import asyncio
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

from pydantic import BaseModel, ConfigDict, model_validator

from dra.models import (
    Conflict,
    DecisionOutput,
    EvidenceCard,
    NodeKind,
    NodeAssessment,
    NodeStatus,
    CrossWorkerAudit,
    ResearchPlan,
    PlanNode,
    ResearchState,
    Report,
    ReportSection,
    SubAgentReport,
    ResearchTask,
    WorkerAttempt,
    _short_id,
    deduplicate_evidence,
)
from dra.memory import load_checkpoint, save_checkpoint
from dra.nodes import (
    _contains_binding_value,
    assess_research_nodes,
    build_research_plan,
    compile_ready_tasks,
    fallback_report_plan,
    format_gate,
    run_cross_worker_audit,
    render_mission_context,
    resolve_decisions,
    validate_decision_outputs,
    build_report_plan,
    write_report,
    validate_research_plan,
)
from dra.subagent import SubAgentConfig, _expired as _expired_deadline, run_subagent
from dra import events, llm, timing
from dra.events import EventType


class OrchestratorConfig(BaseModel):
    """Orchestrator 顶层配置。"""

    model_config = ConfigDict(extra="forbid")

    max_initial_tasks: int = 8        # build_research_plan 安全硬上限；planner 不再以拆满为目标
    max_concurrent: int = 5          # asyncio.gather 并发上限
    subagent: SubAgentConfig = SubAgentConfig()
    # Python API 的代码默认档（CLI 会显式覆盖；Web 读 runtime_config.json）。
    # 入口差异和验证边界见 STATUS.md。
    planner_model: str = "glm-5.2"
    planner_provider: str = "opencode"
    # 非 None 时向支持该参数的 provider 透传 reasoning_effort。
    planner_effort: str | None = None
    writer_model: str = "deepseek-v4-pro"
    writer_provider: str = "opencode"
    writer_reasoning: bool = False
    # writer 思维强度：设它必须同时 writer_reasoning=True（校验器 fail-loud，语义同 SubAgentConfig.effort）
    writer_effort: str | None = None
    # 每组证据喂 writer 的可选上限（按 published_at 降序取 top-K，编号不重排）。
    # None 表示使用全部授权证据。
    writer_max_cards_per_group: int | None = None
    max_research_rounds: int = 3          # 含初始 Research Round；1 = 只跑当前 ReadySet
    max_tasks_per_round: int = 5 # 每次动态推进最多并行任务数
    max_total_tasks: int = 18             # Round 0、ReadySet 与 Final Research Pass 的总 worker 尝试上限
    # 流式输出（demo 体感）：开启则主 write_report 走 stream=True，逐 token 打到 stdout。
    # 默认关；CLI --stream 会显式开启。
    stream_report: bool = False
    # 全局墙钟预算：到点后不再派新 worker，子代理协作式提前返回，直接进 write_report
    # 强制出报告（partial 优于卡死）。None 表示不设总墙钟。
    total_timeout_s: float | None = 2400.0
    # 从总预算中硬切给最终写作的时间。研究阶段到这个点不再派新的 worker / 启动
    # Report Plan 或跨 Worker 审查，保证最后至少有一段可解释的写作窗口；writer 内部的
    # 2 × 180s 上界与默认 360s 对齐。
    writer_reserve_s: float = 360.0
    # 跨 Worker 审查：冻结证据后的覆盖/矛盾检查，只进 warning 与 Writer，不补派。
    # 默认关：它增加一次 planner 档调用，但不改变调度结果。
    # 可通过 OrchestratorConfig 或 runtime_config.json 同名字段开启。
    enable_cross_worker_audit: bool = False

    @model_validator(mode="after")
    def _co_knob_invariants(self) -> "OrchestratorConfig":
        if self.max_research_rounds < 1:
            raise ValueError("max_research_rounds 必须 >= 1（包含初始 Research Round）")
        if self.max_tasks_per_round < 1:
            raise ValueError("max_tasks_per_round 必须 >= 1")
        if self.max_total_tasks < 1:
            raise ValueError("max_total_tasks 必须 >= 1")
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent 必须 >= 1")
        # writer_effort=开思考并定档，与 writer_reasoning=False 互斥（同 SubAgentConfig.effort）
        if self.writer_effort is not None and not self.writer_reasoning:
            raise ValueError(
                f"writer_effort={self.writer_effort!r} 需要 writer_reasoning=True"
                f"（effort=开思考并定强度，与关思考互斥，co-knob）")
        if self.writer_reserve_s <= 0:
            raise ValueError("writer_reserve_s 必须 > 0")
        if self.total_timeout_s is not None and self.total_timeout_s <= self.writer_reserve_s:
            raise ValueError(
                f"total_timeout_s={self.total_timeout_s} 必须大于 writer_reserve_s={self.writer_reserve_s}")
        # 全局预算必须罩得住单个子代理预算 + 写报告余量，否则子代理还没到点
        # 全局先过期，等于变相把所有子代理截短——那应该显式调 wall_timeout_s 而不是被动发生
        st = self.subagent.wall_timeout_s
        if self.total_timeout_s is not None and st is not None \
                and self.total_timeout_s < st + self.writer_reserve_s:
            raise ValueError(
                f"total_timeout_s={self.total_timeout_s} < 子代理 wall_timeout_s={st}"
                f"+writer_reserve_s={self.writer_reserve_s}：全局预算罩不住单子代理（co-knob）")
        return self


async def _bounded_run(
    sem: asyncio.Semaphore,
    task: ResearchTask,
    config: SubAgentConfig,
    *,
    verbose: bool,
    deadline: float | None = None,
    mission_context: str | None = None,
) -> SubAgentReport:
    """Semaphore 限流包装：超过 max_concurrent 的子代理排队。"""
    async with sem:
        return await run_subagent(
            task, config, verbose=verbose, deadline=deadline,
            mission_context=mission_context,
        )


async def _dispatch_task_batch(
    questions: list[ResearchTask],
    config: OrchestratorConfig,
    *,
    verbose: bool,
    deadline: float | None,
    mission_context_by_task: dict[str, str] | None = None,
) -> tuple[list[SubAgentReport], int, list[WorkerAttempt]]:
    """并发执行一个 Task Batch；返回成功回传的 reports 与硬异常数。

    timeout/empty/failed 是 worker 的软状态，仍作为 report 回传供最终 partial 判定；
    只有协程直接抛异常才计 hard failure。Research Round 内共享 Semaphore，Research Round 间由调用方 await 隔离。
    """
    sem = asyncio.Semaphore(config.max_concurrent)
    results = await asyncio.gather(
        *[_bounded_run(
            sem, sq, config.subagent, verbose=verbose, deadline=deadline,
            mission_context=(mission_context_by_task or {}).get(sq.id),
        )
          for sq in questions],
        return_exceptions=True,
    )
    reports: list[SubAgentReport] = []
    attempts: list[WorkerAttempt] = []
    failures = 0
    for sq, result in zip(questions, results, strict=True):
        if isinstance(result, BaseException):
            failures += 1
            attempts.append(WorkerAttempt(
                task_id=sq.id,
                node_id=sq.node_id,
                round_index=sq.round_index,
                status="hard_error",
                error=f"{type(result).__name__}: {result}",
            ))
            # worker 内部若直接抛异常，自己的 SUBAGENT_DONE 来不及发；调度层补一张
            # terminal card，避免前端永远停在 running/误画绿灯。
            events.emit(
                EventType.SUBAGENT_DONE,
                sid=sq.id,
                objective=sq.objective,
                node_id=sq.node_id,
                round_index=sq.round_index,
                tool_calls=0,
                evidence_count=0,
                status="hard_error",
                error=f"{type(result).__name__}: {result}",
            )
            if verbose:
                print(f"[Orchestrator] ⚠️ 子代理 {sq.id} 失败："
                      f"{type(result).__name__}: {result}")
            continue
        attempts.append(WorkerAttempt(
            task_id=sq.id,
            node_id=sq.node_id,
            round_index=sq.round_index,
            status=(result.status if result.status in {"ok", "empty", "timeout", "failed"}
                    else "failed"),
        ))
        reports.append(result)
        if verbose:
            print(f"[Orchestrator] ✅ Round {sq.round_index} 子代理 {sq.id} 完成 "
                  f"| {result.tool_calls} 次工具动作 | {len(result.evidence)} 张证据 "
                  f"| {len(result.conflicts)} 个矛盾")
    return reports, failures, attempts


def _dedupe_conflicts(conflicts: list[Conflict]) -> list[Conflict]:
    """有锚按 (dimension, sorted card_ids) 去重,无锚按 (dimension, description)。

    子代理与跨 Worker 审查可能各自检测到同一个矛盾——避免重复呈现。
    无锚矛盾(worker finish 申报,card_ids=[])若也用空 ids 做键,同维度的
    **不同**矛盾会被误并成一条,故降级用描述文本区分;逐字重复仍合并。
    """
    seen: set[tuple[str, tuple[int, ...] | str]] = set()
    out: list[Conflict] = []
    for c in conflicts:
        anchor: tuple[int, ...] | str = (
            tuple(sorted(c.card_ids)) if c.card_ids else c.description.strip()
        )
        key = (c.dimension.strip(), anchor)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _remap_subagent_conflicts(
    state: ResearchState,
    cross_worker_audit: CrossWorkerAudit | None,
) -> list[Conflict]:
    """把子代理 conflicts 的局部 card_ids 重映射成全局编号，再并入全局 conflicts。

    背景（修前 bug）：子代理用局部证据编号给模型，
    产出的 card_ids 是**局部 1-based 编号**。orchestrator 拼接 + 去重后全局编号空间
    已变（A 占 [1..nA]、B 占 [nA+1..]，且去重会删卡移位），但旧代码直接把局部编号
    当全局编号喂 writer → 矛盾挂到错误证据（张冠李戴）。静默 bug：writer 只打印
    card_ids 不索引、judge 不核 conflict 归因、测试不验全局位置，三层逃逸没被发现。

    映射方式（为什么用 card id 不用位置偏移）：去重按置信度选 best 并重排删卡，
    位置偏移只在「零去重」时成立。card id 稳定——保留卡就是原对象（id 不变），
    精确去重下被删卡 = 真·重复（同 url + 归一化后同 claim），无独立语义需重定向。

    处理规则：
    1. 局部 pos → card.id（越界丢弃，二次兜底，审计出口已拦过）。
    2. card.id → 全局 pos（被精确去重删的卡 → miss，丢弃；语义无损因为它本就是
       同 url+claim 的真重复）。
    3. card_ids 本来为空 → **原样透传**。tool-loop 的 finish 契约只收
       dimension/description/severity（没有 card_ids 参数），worker 申报的矛盾
       全部无锚——旧规则「new_ids 为空即丢弃」曾把这类矛盾 100% 灭口
       （实证：20260722 run 5 个 worker 申报、报告 0 呈现），无锚≠无效。
    4. card_ids 非空但全部解析失败 → 丢弃（引用的都是真重复卡,原兜底语义）。
    跨 Worker 审查的 conflicts 本就是全局编号（它接收去重后 state.evidence），
    原样追加，不二次重映射——结构性隔离，不需 flag。
    """
    # 全局卡 id → 去重后全局 1-based 位置
    global_pos: dict[str, int] = {
        c.id: i for i, c in enumerate(state.evidence, 1)
    }

    out: list[Conflict] = []
    for r in state.sub_reports:
        # 该子代理局部 pos → card.id（子代理最终证据列表，跨轮 append 不重排）
        local_id: dict[int, str] = {i: c.id for i, c in enumerate(r.evidence, 1)}
        for c in r.conflicts:
            if not c.card_ids:
                out.append(c)                       # 无锚申报（当前 finish 契约唯一形态）→ 透传
                continue
            new_ids: list[int] = []
            for pos in c.card_ids:
                cid = local_id.get(pos)           # 局部 pos → card.id（越界丢弃）
                if cid is None:
                    continue
                gpos = global_pos.get(cid)          # card.id → 全局 pos（去重删的 miss 丢弃）
                if gpos is not None:
                    new_ids.append(gpos)
            if not new_ids:
                continue                            # 有锚但全解析失败 → 丢整个 conflict
            out.append(Conflict(
                dimension=c.dimension,
                card_ids=sorted(set(new_ids)),
                description=c.description,
                severity=c.severity,
            ))

    # 跨 Worker 审查 conflicts 原样追加（已是全局编号，不二次重映射）
    if cross_worker_audit:
        out.extend(cross_worker_audit.conflicts)
    return _dedupe_conflicts(out)


def _merge_evidence(state: ResearchState) -> int:
    """从 state.sub_reports 重算去重后的全局证据（就地更新 state），返回去重前总数。

    每批 ReadySet 或 Final Research Pass worker 回传后都整体重去重，保证全集一致。
    """
    merged: list[EvidenceCard] = [c for r in state.sub_reports for c in r.evidence]
    before = len(merged)
    merged = deduplicate_evidence(merged)
    state.evidence = merged
    state.raw_evidence_count = before
    return before


def _assessment_map(state: ResearchState) -> dict[str, NodeAssessment]:
    return {result.node_id: result for result in state.node_assessments}


def _decision_output_map(state: ResearchState) -> dict[str, DecisionOutput]:
    return {
        output.node_id: output for output in state.decision_outputs
    }


def _upsert_decision_outputs(
    state: ResearchState,
    research_plan: ResearchPlan,
    outputs: list[DecisionOutput],
) -> None:
    """按 node ID 覆盖最新决策产物，同时保持计划顺序便于 checkpoint 回放。"""
    merged = _decision_output_map(state)
    merged.update({output.node_id: output for output in outputs})
    state.decision_outputs = [
        merged[node.id]
        for node in research_plan.plan_nodes
        if node.id in merged
    ]


def _upsert_node_assessments(
    state: ResearchState,
    research_plan: ResearchPlan,
    results: list[NodeAssessment],
) -> None:
    """按 node ID 覆盖最新裁决，同时保持计划顺序便于回放。"""
    merged = _assessment_map(state)
    merged.update({result.node_id: result for result in results})
    state.node_assessments = [
        merged[node.id]
        for node in research_plan.plan_nodes
        if node.id in merged
    ]


def _completed_node_ids(state: ResearchState) -> set[str]:
    return {
        result.node_id
        for result in state.node_assessments
        if result.status is NodeStatus.COMPLETE
    }


_MAX_ACTIVATIONS_PER_NODE: int = 2
"""单个 research node 最多被激活两次。

根计划节点 Round 0 + 1 次重试后仍未 complete → 关闭重试，让下游基于 partial 证据推进。
非根计划节点继承上游证据，理论上 1-2 轮就够——统一上限是结构性的、不按 query 调参。
Decision 的首次生成与唯一一次契约修复都封装在 Resolver 内，不使用本计数重跑。
"""


def _closed_node_ids(
    research_plan: ResearchPlan, state: ResearchState
) -> set[str]:
    """执行进度闸：不再为这些 node 重复花费预算。

    closed 不代表 complete，也不自动代表依赖可用。research 达到激活上限后
    关闭重试；decision 经确定性校验落账后即关闭，合法产物 complete，非法产物
    blocked。是否足以让下游降级推进只适用于 research partial。
    """
    result_by_id = {r.node_id: r for r in state.node_assessments}
    closed: set[str] = set()
    for node in research_plan.plan_nodes:
        if node.id not in result_by_id:
            continue
        if result_by_id[node.id].assessment_contract_error:
            # Assessor 已在同一次调用内原地重试；协议失败不能再触发 Resolver 或 Worker。
            closed.add(node.id)
            continue
        if (
            node.kind is NodeKind.RESEARCH
            and state.node_activation_counts.get(node.id, 0)
            >= _MAX_ACTIVATIONS_PER_NODE
        ):
            closed.add(node.id)
        elif node.kind is NodeKind.DECISION:
            closed.add(node.id)
    return closed



def _degraded_dependency_ids(
    research_plan: ResearchPlan, state: ResearchState
) -> set[str]:
    """质量闸：只让有可消费产物的 closed partial 参与依赖解锁。"""
    closed = _closed_node_ids(research_plan, state)
    result_by_id = _assessment_map(state)
    degraded: set[str] = set()
    for node in research_plan.plan_nodes:
        if node.id not in closed:
            continue
        result = result_by_id.get(node.id)
        if (
            result is None
            or result.status is not NodeStatus.PARTIAL
            or not result.evidence_ids
        ):
            continue
        if node.kind is NodeKind.RESEARCH:
            degraded.add(node.id)
    return degraded


def _sufficient_dep_ids(
    research_plan: ResearchPlan, state: ResearchState
) -> set[str]:
    """依赖判定用：complete ∪ 有证据产物的 degraded partial。"""
    return _completed_node_ids(state) | _degraded_dependency_ids(research_plan, state)


def _dependencies_sufficient(
    node: PlanNode,
    sufficient_ids: set[str],
) -> bool:
    return all(dep in sufficient_ids for dep in node.dependency_ids)


def _unresolved_plan_nodes(
    research_plan: ResearchPlan, state: ResearchState
) -> list[PlanNode]:
    """控制流唯一真相：尚未 complete 的计划节点（含 closed-retry 的 partial）。"""
    complete = _completed_node_ids(state)
    return [m for m in research_plan.plan_nodes if m.id not in complete]


def _ready_research_nodes(
    research_plan: ResearchPlan, state: ResearchState
) -> list[PlanNode]:
    sufficient = _sufficient_dep_ids(research_plan, state)
    closed = _closed_node_ids(research_plan, state)
    return [
        node
        for node in research_plan.plan_nodes
        if node.kind is NodeKind.RESEARCH
        and node.id not in sufficient
        and node.id not in closed
        and _dependencies_sufficient(node, sufficient)
    ]


def _record_final_research_pass_unactionable(state: ResearchState, node_id: str) -> None:
    if node_id not in state.final_research_pass_unactionable_ids:
        state.final_research_pass_unactionable_ids.append(node_id)


def _final_research_pass_candidates(
    research_plan: ResearchPlan, state: ResearchState
) -> list[PlanNode]:
    """正常 ReadySet 后可进入一次并行补查的 research node。

    准入：research、依赖 sufficient，且要么尚未首次执行、要么最新结果为
    PARTIAL/BLOCKED。decision 永不进入。已经被 complete Decision 消费的全部祖先也不再进入：
    Decision 产物和 bindings 已冻结，系统没有级联重算，事后补上游只会制造与既有决策不一致的
    新证据。这样上游重试挤占计划 Research Round 后，刚解锁的下游 research 节点仍有一次首次
    执行机会，同时不会回头重做已经跨过的决策前置研究。

    返回顺序先放从未激活的新解锁节点，再放已有结果的补查节点；同级保持 Planner 声明顺序。
    单次 Final Research Pass 只取前若干个候选，每个最多一个 Worker，不在同一次运行里重复补派。
    """
    result_by_id = _assessment_map(state)
    sufficient = _sufficient_dep_ids(research_plan, state)
    node_by_id = {node.id: node for node in research_plan.plan_nodes}
    consumed_upstream_ids: set[str] = set()
    for decision in research_plan.plan_nodes:
        decision_result = result_by_id.get(decision.id)
        if (
            decision.kind is not NodeKind.DECISION
            or decision_result is None
            or decision_result.status is not NodeStatus.COMPLETE
        ):
            continue
        stack = list(decision.dependency_ids)
        while stack:
            ancestor_id = stack.pop()
            if ancestor_id in consumed_upstream_ids:
                continue
            consumed_upstream_ids.add(ancestor_id)
            ancestor = node_by_id.get(ancestor_id)
            if ancestor is not None:
                stack.extend(ancestor.dependency_ids)

    candidates: list[PlanNode] = []
    for node in research_plan.plan_nodes:
        if node.kind is not NodeKind.RESEARCH:
            continue
        if node.id in consumed_upstream_ids:
            continue
        result = result_by_id.get(node.id)
        if result is not None and result.status is NodeStatus.COMPLETE:
            continue
        if result is not None and result.assessment_contract_error:
            continue
        if result is not None and result.status not in (
            NodeStatus.PARTIAL,
            NodeStatus.BLOCKED,
        ):
            continue
        if not _dependencies_sufficient(node, sufficient):
            continue
        candidates.append(node)
    # 预算不足时先保障刚解锁、尚未获得任何 Worker 的节点；同级保持 Planner 顺序。
    candidates.sort(
        key=lambda node: state.node_activation_counts.get(node.id, 0) > 0
    )
    return candidates


_FALLBACK_BINDING_VALUE_LIMIT = 4


def _fallback_research_task_text(
    node: PlanNode,
    gaps: list[str],
    downstream_binding_values: list[str],
) -> tuple[str, str, list[str]]:
    """构造确定性 fallback 文本，优先完整保留 Assessor 的可执行 gap。

    有 gap 时它已经按契约包含实体、时间或来源要求，不能再被整批 bindings
    挤到 query 尾部后截断。没有 gap 通常表示刚解锁节点首次执行；此时才取少量
    上游值帮助解析“已选对象”等指代，避免把整份 rich bindings 变成关键词堆。
    """
    binding_values = list(dict.fromkeys(
        value.strip() for value in downstream_binding_values if value.strip()
    ))
    gap = next((item.strip() for item in gaps if item.strip()), "")
    if gap:
        matched_values = [
            value for value in binding_values
            if _contains_binding_value(gap, value)
        ]
        return f"补齐证据缺口：{gap}", gap, matched_values

    base = (node.objective or "").strip() or (node.acceptance_criteria or "").strip()
    if not base:
        return "", "", []
    selected_values = binding_values[:_FALLBACK_BINDING_VALUE_LIMIT]
    search_query = " ".join([*selected_values, base]) if selected_values else base
    return base, search_query, selected_values


def _build_final_research_pass_fallback_task(
    research_plan: ResearchPlan,
    state: ResearchState,
    target: PlanNode,
    *,
    round_index: int,
) -> ResearchTask | None:
    """compiler 无合法 task 时的确定性补查。

    优先 gaps[0]；PARTIAL/BLOCKED 但 gaps 为空时用 acceptance_criteria / objective，
    避免静默跳过。仍构造不出可执行 query 则返回 None。
    """
    result_by_id = _assessment_map(state)
    result = result_by_id.get(target.id)
    gaps = list(result.gaps) if result is not None and result.gaps else []
    downstream_binding_values = _grounded_downstream_bindings_for_node(
        research_plan, target, result_by_id
    )
    objective, search_query, task_binding_values = _fallback_research_task_text(
        target,
        gaps,
        downstream_binding_values,
    )
    if not objective or not search_query:
        return None
    fallback_evidence = _allowed_evidence_from_plan(state, research_plan, [target])
    return ResearchTask(
        node_id=target.id,
        objective=objective,
        search_query=search_query,
        round_index=round_index,
        prerequisite_context=(
            "必须沿用的对象或参数：" + "；".join(task_binding_values)
            if task_binding_values else None
        ),
        prerequisite_evidence_ids=sorted(fallback_evidence.get(target.id, set())),
    )


def _ensure_unique_task_ids(
    tasks: list[ResearchTask],
    state: ResearchState,
) -> None:
    """Final Research Pass dispatch 前保证本 run 内 task id 唯一，避免 resume 按 id 混入历史 report。"""
    used: set[str] = {task.id for task in state.executed_tasks if task.id}
    used.update(attempt.task_id for attempt in state.worker_attempts if attempt.task_id)
    used.update(
        report.research_task_id for report in state.sub_reports if report.research_task_id
    )
    for task in tasks:
        if task.id and task.id not in used:
            used.add(task.id)
            continue
        new_id = _short_id()
        while new_id in used:
            new_id = _short_id()
        task.id = new_id
        used.add(new_id)


def _finalize_run_status(
    state: ResearchState,
    research_plan: ResearchPlan,
    *,
    config: OrchestratorConfig,
    cross_worker_audit,
    final_missing: list,
    deadline_expired: bool,
) -> None:
    """业务完成 vs 运行告警分层：status 只看 completion_blockers。"""
    unresolved = _unresolved_plan_nodes(research_plan, state)
    complete_ids = _completed_node_ids(state)
    sufficient_ids = _sufficient_dep_ids(research_plan, state)
    closed_ids = _closed_node_ids(research_plan, state)

    terminal_reasons: dict[str, str] = {}
    for node in unresolved:
        assessment = _assessment_map(state).get(node.id)
        missing_dependencies = [
            dependency_id for dependency_id in node.dependency_ids
            if dependency_id not in sufficient_ids
        ]
        if assessment is not None and assessment.assessment_contract_error:
            reason = f"assessment_contract_error:{assessment.assessment_contract_error}"
        elif missing_dependencies:
            reason = "blocked_by_dependencies:" + ",".join(missing_dependencies)
        elif node.id in state.final_research_pass_unactionable_ids:
            reason = "final_research_pass_unactionable"
        elif node.id in closed_ids:
            reason = "closed_partial_retry_limit"
        elif deadline_expired:
            reason = "deadline_exhausted"
        elif state.research_rounds_completed >= config.max_research_rounds:
            reason = "research_round_budget_exhausted"
        elif len(state.worker_attempts) >= config.max_total_tasks:
            reason = "task_budget_exhausted"
        elif assessment is None:
            reason = "unassessed"
        else:
            reason = f"unresolved_{assessment.status.value}"
        terminal_reasons[node.id] = reason
    state.node_terminal_reasons = terminal_reasons

    blockers: list[str] = []
    if unresolved:
        blockers.append("unresolved_plan_nodes")
    report_empty = (
        state.report is None
        or not state.report.sections
        or not any(section.markdown.strip() for section in state.report.sections)
    )
    if report_empty:
        blockers.append("report_empty")

    warnings: list[str] = []
    all_business_complete = not unresolved
    recovered = any(
        attempt.status != "ok"
        and (
            (
                attempt.node_id is not None
                and attempt.node_id in complete_ids
            )
            or (
                attempt.node_id is None
                and all_business_complete
            )
        )
        for attempt in state.worker_attempts
    )
    if recovered:
        warnings.append("recovered_worker_failure")
    # Cross-Worker Audit 是冻结证据后的 advisory，不承担节点验收或调度裁决。
    # 显式关闭(enable_cross_worker_audit=False)是有意配置,不进 warnings——
    # skipped 只在「开着却没跑成」(无证据/截止耗尽)时出现,保住告警信噪比。
    if cross_worker_audit is None:
        if config.enable_cross_worker_audit:
            warnings.append("cross_worker_audit_skipped")
    elif cross_worker_audit.has_findings:
        warnings.append("cross_worker_audit_findings")
    # 报告已空由 report_empty 阻断；有正文时 shape 失败只作 warning。
    if final_missing and not report_empty:
        warnings.append("shape_gate_failed")
    if deadline_expired:
        warnings.append("deadline_exhausted")
    if unresolved and state.research_rounds_completed >= config.max_research_rounds:
        # 计划内 ReadySet 的 round_index 上限事实；若 Final Research Pass 已跑过，下面还会补
        # task/stall 等停止原因，避免把 max_research_rounds 误读成唯一终点。
        warnings.append("research_round_budget_exhausted")
    if unresolved and len(state.worker_attempts) >= config.max_total_tasks:
        warnings.append("task_budget_exhausted")
    if unresolved and state.final_research_pass_unactionable_ids:
        warnings.append("final_research_pass_unactionable")
    if any(
        result.assessment_contract_error for result in state.node_assessments
    ):
        warnings.append("assessment_contract_error")
    closed_unresolved = [
        mid for mid in _closed_node_ids(research_plan, state)
        if mid not in _completed_node_ids(state)
    ]
    if closed_unresolved:
        warnings.append("plan_nodes_closed_retry")

    state.completion_blockers = list(dict.fromkeys(blockers))
    state.warnings = list(dict.fromkeys(warnings))
    state.status = "partial" if state.completion_blockers else "done"


def _writer_timeout_fallback(
    query: str,
    evidence: list[EvidenceCard],
    *,
    allowed_indices: set[int] | None = None,
) -> Report:
    """writer 未在预算内返回时，交付可追溯的证据摘要而非整次 run 失败。

    这里只机械列出已经通过 grounding 的 evidence claim 与原始全局编号，不做新的
    综合推断；因此它是诚实的 partial report，仍可让用户读取和追溯已完成的研究。
    """
    if not evidence:
        return Report(title=query)
    ranked = sorted(
        (
            pair for pair in enumerate(evidence, 1)
            if allowed_indices is None or pair[0] in allowed_indices
        ),
        key=lambda pair: pair[1].published_at or "",
        reverse=True,
    )[:30]
    if not ranked:
        return Report(title=query)
    lines = [
        "最终成稿模型未在限定时间内返回。以下列出已通过证据校验的要点；"
        "这不是模型综合结论，完整叙事可在调整模型后重试生成。",
        "",
    ]
    lines.extend(f"- {card.claim} [{idx}]" for idx, card in ranked)
    return Report(
        title=query,
        sections=[ReportSection(
            heading="已收集证据（成稿超时）",
            markdown="\n".join(lines),
        )],
    )


def _allowed_evidence_from_plan(
    state: ResearchState,
    research_plan: ResearchPlan,
    plan_nodes: list[PlanNode],
) -> dict[str, set[str]]:
    """给每个节点计算 complete/degraded 祖先闭包授权的 evidence IDs。"""
    node_by_id = {node.id: node for node in research_plan.plan_nodes}
    prior_by_id = _assessment_map(state)
    sufficient = _sufficient_dep_ids(research_plan, state)

    def ancestor_ids(mid: str) -> set[str]:
        result: set[str] = set()
        stack = list(node_by_id[mid].dependency_ids)
        while stack:
            ancestor_id = stack.pop()
            if ancestor_id in result:
                continue
            result.add(ancestor_id)
            stack.extend(node_by_id[ancestor_id].dependency_ids)
        return result

    return {
        node.id: {
            evidence_id
            for ancestor_id in ancestor_ids(node.id)
            for evidence_id in (
                prior_by_id[ancestor_id].evidence_ids
                if ancestor_id in prior_by_id
                and ancestor_id in sufficient
                else []
            )
        }
        for node in plan_nodes
    }


def _grounded_downstream_bindings_for_node(
    research_plan: ResearchPlan,
    node: PlanNode,
    result_by_id: dict[str, NodeAssessment],
) -> list[str]:
    """收集上游已裁决的 grounded downstream_binding，供 deterministic fallback 定向检索。"""
    node_by_id = {item.id: item for item in research_plan.plan_nodes}
    values: list[str] = []
    stack = list(node.dependency_ids)
    seen: set[str] = set()
    while stack:
        ancestor_id = stack.pop()
        if ancestor_id in seen:
            continue
        seen.add(ancestor_id)
        result = result_by_id.get(ancestor_id)
        if result is not None:
            for bound_values in result.downstream_bindings.values():
                for value in bound_values:
                    if value and value not in values:
                        values.append(value)
        ancestor = node_by_id.get(ancestor_id)
        if ancestor is not None:
            stack.extend(ancestor.dependency_ids)
    return values


def _record_assessment_results(
    state: ResearchState,
    research_plan: ResearchPlan,
    plan_nodes: list[PlanNode],
    assessed: list[NodeAssessment],
) -> list[NodeAssessment]:
    """统一 fail-closed、落 node ledger 并发出完成裁决事件。"""
    by_id = {result.node_id: result for result in assessed}
    normalized = [
        by_id.get(node.id) or NodeAssessment(
            node_id=node.id,
            status=NodeStatus.BLOCKED,
            summary="计划节点裁决缺失",
        )
        for node in plan_nodes
    ]
    _upsert_node_assessments(state, research_plan, normalized)
    completed = _completed_node_ids(state)
    events.emit(
        EventType.NODES_ASSESSED,
        assessments=[{
            "node_id": result.node_id,
            "status": result.status.value,
            "summary": result.summary,
            "gaps": result.gaps,
            "evidence_ids": result.evidence_ids,
            "downstream_bindings": result.downstream_bindings,
            "assessment_contract_error": result.assessment_contract_error,
        } for result in normalized],
        completed_ids=[
            node.id for node in research_plan.plan_nodes
            if node.id in completed
        ],
        unresolved_node_ids=[
            node.id for node in research_plan.plan_nodes
            if node.id not in completed
        ],
    )
    return normalized


def _assess_batch(
    state: ResearchState,
    research_plan: ResearchPlan,
    plan_nodes: list[PlanNode],
    *,
    tasks: list[ResearchTask],
    reports: list[SubAgentReport],
    config: OrchestratorConfig,
) -> list[NodeAssessment]:
    """验收 research worker 产物并落 ledger；worker 跑完本身不改变完成状态。

    这里只传当前 scheduler 批次是有意的 retry 语义：当批次失败时本次不能完成；
    后续同 node 的全成功补派可重新裁决业务完成度，但历史 WorkerAttempt 不删除。
    Decision node 不再进入本函数，由 Resolver + output assessor 独立处理。
    """
    if not plan_nodes:
        return []
    non_research = [m.id for m in plan_nodes if m.kind is not NodeKind.RESEARCH]
    if non_research:
        raise ValueError(f"_assess_batch 只接受 research plan_nodes：{non_research}")
    allowed_from_plan = _allowed_evidence_from_plan(state, research_plan, plan_nodes)
    # node assessor 原本没有 timing context，llm.chat 咽喉因此不知道这次调用
    # 属于哪个编排节点，Web 调试档只能看到裁决结果、看不到真实输入/输出。统一走
    # timing.step 后，现有 llm_call 事件会自动带上截断后的 prompt、完整 output 与 token。
    with timing.step("assess_research_nodes"):
        assessed = assess_research_nodes(
            research_plan.clarified_query,
            plan_nodes,
            state.evidence,
            tasks=tasks,
            reports=reports,
            prior_results=state.node_assessments,
            allowed_evidence_ids_by_node=allowed_from_plan,
            model=config.planner_model,
            provider=config.planner_provider,
            effort=config.planner_effort,
        )
    return _record_assessment_results(state, research_plan, plan_nodes, assessed)


def _validate_decision_batch(
    state: ResearchState,
    research_plan: ResearchPlan,
    plan_nodes: list[PlanNode],
) -> list[NodeAssessment]:
    """确定性校验已落入 state 的 DecisionOutput，并写入统一节点账本。"""
    output_by_id = _decision_output_map(state)
    outputs = [
        output_by_id[node.id]
        for node in plan_nodes
        if node.id in output_by_id
    ]
    allowed_from_plan = _allowed_evidence_from_plan(state, research_plan, plan_nodes)
    validated = validate_decision_outputs(
        plan_nodes,
        outputs,
        state.evidence,
        prior_results=state.node_assessments,
        allowed_evidence_ids_by_node=allowed_from_plan,
        all_plan_nodes=research_plan.plan_nodes,
    )
    return _record_assessment_results(
        state, research_plan, plan_nodes, validated,
    )


def _resolve_ready_decisions(
    state: ResearchState,
    research_plan: ResearchPlan,
    config: OrchestratorConfig,
    *,
    only_unassessed: bool = False,
    checkpoint_dir: str | Path | None = None,
    run_id: str | None = None,
) -> None:
    """按拓扑执行 ready decision，并以确定性 Validator 落账。

    Resolver 的首次生成与唯一一次修复都在同一次 resolve_decisions 调用内。
    若 checkpoint 已保存 DecisionOutput 但尚无 NodeAssessment，恢复时直接做确定性
    校验，不重复调用 Resolver。
    """
    node_by_id = {node.id: node for node in research_plan.plan_nodes}
    result_ids = set(_assessment_map(state))
    saved_unassessed = [
        node_by_id[output.node_id]
        for output in state.decision_outputs
        if output.node_id in node_by_id
        and node_by_id[output.node_id].kind is NodeKind.DECISION
        and output.node_id not in result_ids
    ]
    if saved_unassessed:
        _validate_decision_batch(state, research_plan, saved_unassessed)
        if checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)

    while True:
        sufficient = _sufficient_dep_ids(research_plan, state)
        closed = _closed_node_ids(research_plan, state)
        result_ids = set(_assessment_map(state))
        ready = [
            node
            for node in research_plan.plan_nodes
            if node.kind is NodeKind.DECISION
            and node.id not in sufficient
            and node.id not in closed
            and (not only_unassessed or node.id not in result_ids)
            and _dependencies_sufficient(node, sufficient)
        ]
        if not ready:
            return

        allowed_from_plan = _allowed_evidence_from_plan(state, research_plan, ready)
        with timing.step("resolve_decisions"):
            resolved = resolve_decisions(
                research_plan.clarified_query,
                ready,
                state.evidence,
                prior_results=state.node_assessments,
                prior_decision_outputs=state.decision_outputs,
                allowed_evidence_ids_by_node=allowed_from_plan,
                all_plan_nodes=research_plan.plan_nodes,
                model=config.planner_model,
                provider=config.planner_provider,
                effort=config.planner_effort,
            )
        resolved_by_id = {output.node_id: output for output in resolved}
        normalized = [
            resolved_by_id.get(node.id) or DecisionOutput(
                node_id=node.id,
                decision_summary="Decision Resolver 产物缺失",
                contract_error="Decision Resolver 未返回当前节点产物",
            )
            for node in ready
        ]
        _upsert_decision_outputs(state, research_plan, normalized)
        for node in ready:
            state.node_activation_counts[node.id] = (
                state.node_activation_counts.get(node.id, 0) + 1
            )
        # 先保存昂贵的 Resolver 产物；若随后进程中断，resume 会直接确定性校验。
        if checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)

        _validate_decision_batch(state, research_plan, ready)
        if checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)


def _recover_pending_assessment(
    state: ResearchState,
    research_plan: ResearchPlan,
    config: OrchestratorConfig,
    *,
    verbose: bool = False,
) -> bool:
    """恢复“worker 已完成、assessor 未落账”的批次，不再次 dispatch。

    返回是否实际恢复了一个 research assessment batch。pending task ID 是 checkpoint
    的阶段提交标记；找不到对应 executed task 说明 checkpoint 逻辑损坏，必须 fail-loud。
    """
    pending_ids = list(dict.fromkeys(state.pending_assessment_task_ids))
    if not pending_ids:
        return False
    task_by_id = {task.id: task for task in state.executed_tasks}
    missing = [task_id for task_id in pending_ids if task_id not in task_by_id]
    if missing:
        raise RuntimeError(f"checkpoint 待裁决 task 缺失：{missing}")
    tasks = [task_by_id[task_id] for task_id in pending_ids]
    node_ids = list(dict.fromkeys(
        task.node_id for task in tasks if task.node_id
    ))
    node_by_id = {node.id: node for node in research_plan.plan_nodes}
    unknown = [mid for mid in node_ids if mid not in node_by_id]
    if unknown:
        raise RuntimeError(f"checkpoint 待裁决 node 缺失：{unknown}")
    pending_set = set(pending_ids)
    reports = [
        report for report in state.sub_reports
        if report.research_task_id in pending_set
    ]
    _assess_batch(
        state,
        research_plan,
        [node_by_id[mid] for mid in node_ids],
        tasks=tasks,
        reports=reports,
        config=config,
    )
    state.pending_assessment_task_ids = []
    return True


def _group_evidence_by_task(
    state: ResearchState,
    *,
    allowed_evidence_ids: set[str] | None = None,
) -> list[tuple[str, list[int]]]:
    """把去重后的全局证据按来源研究任务分组，**保留全局 1-based 编号**（第0刀）。

    _merge_evidence 把 sub_reports 的 per-研究任务分组拍平成一锅 → writer 拿到无结构
    listing，只能写大平铺。这里据 card.id 反查它来自哪个研究任务，恢复那层结构喂回 writer。
    返回 [(objective, [global_1based_idx, ...])]，按研究任务首次出现顺序。
    allowed_evidence_ids 给定时只保留节点验收正式授权的卡；全局编号保持原位、不压缩。

    编号仍是 state.evidence 里的全局位置（不重排不重编号）——render /
    conflicts 都靠它索引，动了就张冠李戴。dedup 保留的是原卡对象（id 不变），故
    card.id 必在映射里；所有任务的卡均按 task objective 归组。
    """
    id2obj: dict[str, str] = {
        c.id: r.objective for r in state.sub_reports for c in r.evidence
    }
    groups: dict[str, list[int]] = {}
    order: list[str] = []
    for gidx, c in enumerate(state.evidence, 1):
        if allowed_evidence_ids is not None and c.id not in allowed_evidence_ids:
            continue
        obj = id2obj.get(c.id) or "其它来源"
        if obj not in groups:
            groups[obj] = []
            order.append(obj)
        groups[obj].append(gidx)
    return [(obj, groups[obj]) for obj in order]


_CITE_RE = re.compile(r"\[(\d+)\]")


def build_citation_audit(report, evidence, evidence_groups) -> dict:
    """candidate/used 双层引用台账（对标 ms-agent 双层账 / lunon source_table）：
    喂给 writer 的已授权候选里，多少真被引用了——writer 转化率的直接观测。
    n_evidence 保留全部证据账本规模；n_candidates 才是 Writer 实际可见数量。"""
    used = sorted({
        n for s in report.sections
        for n in (int(m) for m in _CITE_RE.findall(s.markdown))
        if 1 <= n <= len(evidence)
    })
    candidate_ids = {
        idx for _objective, idxs in (evidence_groups or []) for idx in idxs
    } if evidence_groups is not None else set(range(1, len(evidence) + 1))
    used = [idx for idx in used if idx in candidate_ids]
    used_set = set(used)
    groups = [{"objective": obj, "candidates": len(idxs),
               "used": len(used_set & set(idxs))}
              for obj, idxs in (evidence_groups or [])]
    return {"n_evidence": len(evidence), "n_candidates": len(candidate_ids),
            "n_used": len(used),
            "used_ratio": round(len(used) / max(len(candidate_ids), 1), 3),
            "used_ids": used, "groups": groups}


def _norm_gap(text: str) -> str:
    """gap 文本归一化键（③d 跨轮防重复补派用）。

    复用 deduplicate_evidence 的 claim 归一化同款（models.py 内 _key）：
    小写 + 折叠所有空白，吸收大小写/空格抖动，避免同一 gap 被反复补派。
    """
    return "".join(text.lower().split())


def _gap_seen(gap: str, attempted: set[str]) -> bool:
    """gap 是否已补派过（字面 + 模糊）：SequenceMatcher≥0.85 视为同一 gap 的改写
    （实测「同句加年份后缀」ratio≈0.86，0.9 会漏判）。治「语义同义 gap 二次补派浪费
    并发槽」；阈值偏保守——宁可放过真新 gap 不误杀。零依赖、确定性。"""
    ng = _norm_gap(gap)
    return any(ng == a or SequenceMatcher(None, ng, a).ratio() >= 0.85 for a in attempted)


async def run_orchestrator(
    query: str,
    config: OrchestratorConfig | None = None,
    *,
    verbose: bool = True,
    checkpoint_dir: str | Path | None = None,
    resume: bool = False,
    run_id: str | None = None,
    research_plan: ResearchPlan | None = None,
) -> ResearchState:
    """跑一个完整 V1 研究闭环：scope → 并发 dispatch → 全局综合。

    checkpoint_dir 给定时，在付费 worker dispatch 后先落阶段 checkpoint，再在
    assessor/decision 完成后提交结果；恢复时只重跑未落账的幂等裁决，不重复 worker。
    resume=True 且 checkpoint 存在 → 跳过已完成 Research Round；若仍有 pending goals，继续
    计划内动态推进，随后进入跨 Worker 审查。崩在 Research Round 中间可能重跑该 Research Round。
    run_id 隔离多个研究的 checkpoint（对齐 LangGraph thread_id）；缺省用 query 短 hash。
    research_plan 给定时跳过内部 build_research_plan，直接按它 dispatch——plan confirmation HITL 门
    （决策记录 2026-07-06）：计划在载体层（CLI/web）生成并经用户确认后注入，
    orchestrator 核心保持无交互；不传 research_plan 时由内核自行生成计划。resume 命中
    checkpoint 时以 checkpoint 里的 research_plan 为准，注入值被忽略。
    """
    config = config or OrchestratorConfig()
    timing.reset(verbose=verbose)
    events.set_run_id(run_id)   # None 则不附带字段（CLI 旧行为字节级不变）
    _wall_t0 = time.monotonic()
    events.emit(EventType.SCOPE, query=query)  # 首个事件：已收到问题（CLI/web 两路都发，修 drift #1）
    # 全局墙钟预算：每个 LLM HTTP 请求都会被 llm.chat 截断到这个 deadline；不是
    # 只在步骤边界才检查。研究阶段另有更早的 work deadline，给最终 writer 留窗口。
    _deadline = (time.monotonic() + config.total_timeout_s) if config.total_timeout_s else None
    _work_deadline = (
        _deadline - config.writer_reserve_s if _deadline is not None else None
    )
    llm.set_request_deadline(_deadline)

    expected_plan = (
        validate_research_plan(
            research_plan,
            max_research_rounds=config.max_research_rounds,
            max_tasks_per_round=config.max_tasks_per_round,
            max_total_tasks=config.max_total_tasks,
        )
        if research_plan is not None else None
    )
    resume_state = (
        load_checkpoint(
            checkpoint_dir,
            query,
            config=config,
            expected_plan=expected_plan,
            run_id=run_id,
        )
        if (resume and checkpoint_dir)
        else None
    )

    resumed = bool(
        resume_state is not None
        and resume_state.research_plan
        and (
            resume_state.research_rounds_completed > 0
            or resume_state.worker_attempts
            or resume_state.sub_reports
        )
    )
    if resumed:
        # ---- 恢复路径：跳过最贵的 dispatch ----
        state = resume_state
        research_plan = validate_research_plan(
            state.research_plan,
            max_research_rounds=config.max_research_rounds,
            max_tasks_per_round=config.max_tasks_per_round,
            max_total_tasks=config.max_total_tasks,
        )
        state.research_plan = research_plan
        initial_ids = {sq.id for sq in research_plan.initial_tasks}
        initial_reports = sum(1 for r in state.sub_reports if r.research_task_id in initial_ids)
        failures = max(0, len(research_plan.initial_tasks) - initial_reports)
        if verbose:
            print(f"[Orchestrator] 从 checkpoint 恢复：跳过 dispatch"
                  f"（已有 {len(state.sub_reports)} 子代理结果 / {len(state.evidence)} 张证据）")
    else:
        # ---- 正常路径：scope → dispatch → collect ----
        state = ResearchState(query=query)

        # 1) Scoping → ResearchPlan（注入 = 载体层已让用户确认过的计划，跳过拆解；
        #    RESEARCH_PLAN 事件/打印在下方两条路径共用，web 树与 trace 不因注入缺卡）
        if research_plan is None:
            if verbose:
                print(f"[Orchestrator] 拆解研究问题 → ResearchPlan ...")
            with timing.step("build_research_plan"):
                research_plan = build_research_plan(
                    query, max_initial_tasks=config.max_initial_tasks,
                    model=config.planner_model, provider=config.planner_provider,
                    effort=config.planner_effort,
                    max_research_rounds=config.max_research_rounds,
                    max_tasks_per_round=config.max_tasks_per_round,
                    max_total_tasks=config.max_total_tasks,
                )
        else:
            research_plan = expected_plan
            if verbose:
                print(f"[Orchestrator] 使用已确认的研究计划（跳过拆解）")
        research_plan = validate_research_plan(
            research_plan,
            max_research_rounds=config.max_research_rounds,
            max_tasks_per_round=config.max_tasks_per_round,
            max_total_tasks=config.max_total_tasks,
        )
        state.research_plan = research_plan
        events.emit(
            EventType.RESEARCH_PLAN,
            count=len(research_plan.initial_tasks),
            initial_tasks=[
                task.model_dump(mode="json") for task in research_plan.initial_tasks
            ],
            plan_nodes=[{
                "id": node.id,
                "objective": node.objective,
                "kind": node.kind.value,
                "dependency_ids": node.dependency_ids,
                "acceptance_criteria": node.acceptance_criteria,
            } for node in research_plan.plan_nodes],
        )
        if verbose:
            print(f"[Orchestrator] 拆出 {len(research_plan.initial_tasks)} 个研究任务：")
            for i, sq in enumerate(research_plan.initial_tasks, 1):
                print(f"  [{i}] objective={sq.objective}")
                print(f"      search_query={sq.search_query}")

        # 2) 初始 ReadySet（Research Round 0）并发 dispatch。
        # 注入 mission context：让 worker 知道总体任务、自己的计划节点、下游依赖
        initial_mission_contexts = {
            sq.id: render_mission_context(
                research_plan, state.node_assessments,
                view="worker",
                focus_node_id=sq.node_id,
                activation_count=state.node_activation_counts.get(sq.node_id, 0),
            )
            for sq in research_plan.initial_tasks
        }
        events.emit(
            EventType.TASK_BATCH_DISPATCHED,
            count=len(research_plan.initial_tasks),
            objectives=[task.objective for task in research_plan.initial_tasks],
            round_index=0,
            phase="initial",
        )
        if verbose:
            print(f"[Orchestrator] Round 0 并发 dispatch {len(research_plan.initial_tasks)} 个子代理 "
                  f"（max_concurrent={config.max_concurrent}）...")
        with timing.step("dispatch_task_batch(并发)"):
            initial_reports, failures, initial_attempts = await _dispatch_task_batch(
                research_plan.initial_tasks, config, verbose=verbose, deadline=_work_deadline,
                mission_context_by_task=initial_mission_contexts,
            )
        state.sub_reports = initial_reports
        state.executed_tasks = list(research_plan.initial_tasks)
        state.worker_attempts.extend(initial_attempts)
        state.research_rounds_completed = 1

        # 3) 收集证据 + 失败统计
        soft_fails = sum(1 for r in initial_reports if r.status != "ok")
        if verbose and soft_fails:
            n_empty = sum(1 for r in initial_reports if r.status == "empty")
            print(f"[Orchestrator] ⚠️ {soft_fails} 个子代理未正常贡献"
                  f"（timeout/failed/empty，其中 {n_empty} 个零证据覆盖窟窿）"
                  "→ 业务未补齐则 partial，补齐后保留 warning")
        failures += 0  # 保持原 failures 语义（真异常）；state.status 判定改为：

        # 4) 合并证据到全局 state + 去重
        with timing.step("dedup"):
            before = _merge_evidence(state)
        if verbose and before != len(state.evidence):
            print(f"[Orchestrator] 去重：{before} → {len(state.evidence)} 张 "
                  f"（去重 {before - len(state.evidence)} 张）")

        if verbose:
            print(f"[Orchestrator] 合并完毕 | 总证据 {len(state.evidence)} 张 "
                  f"| 子代理失败 {failures}/{len(research_plan.initial_tasks)}")
        events.emit(EventType.COLLECT, before=before, after=len(state.evidence),
                    deduped=before - len(state.evidence), failures=failures,
                    n_sub=len(research_plan.initial_tasks))

        # 4.1) Round 0 worker 结束不等于计划节点完成；按 completion criteria 单独裁决。
        initial_node_ids = list(dict.fromkeys(
            sq.node_id for sq in research_plan.initial_tasks if sq.node_id
        ))
        # Round 0 激活追踪：每个有 task 的 node 记一次激活
        for mid in initial_node_ids:
            state.node_activation_counts[mid] = \
                state.node_activation_counts.get(mid, 0) + 1
        node_by_id = {node.id: node for node in research_plan.plan_nodes}
        state.pending_assessment_task_ids = [sq.id for sq in research_plan.initial_tasks]
        # 最贵的 worker 已完成：必须在 assessor LLM 之前提交阶段 checkpoint。
        # assessor 若抛异常，resume 依据 pending IDs 只重跑裁决，不重复扣 worker 费用。
        if checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)
        _assess_batch(
            state,
            research_plan,
            [node_by_id[mid] for mid in initial_node_ids],
            tasks=research_plan.initial_tasks,
            reports=initial_reports,
            config=config,
        )
        state.pending_assessment_task_ids = []
        # 先持久化 research result，再解析 decision；decision 异常也不会回滚到 dispatch 前。
        if checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)
        _resolve_ready_decisions(
            state,
            research_plan,
            config,
            checkpoint_dir=checkpoint_dir,
            run_id=run_id,
        )

        # checkpoint：最贵的 dispatch 已完成，存一次，崩溃可从这里恢复
        if checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)
            if verbose:
                print(f"[Orchestrator] ✅ checkpoint 已存（dispatch 完成后）")

    if resumed:
        recovered_assessment = _recover_pending_assessment(
            state, research_plan, config, verbose=verbose,
        )
        if recovered_assessment and checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)
        # assessor 可能崩在 ready decision（无 worker task）上；只补没有任何 result 的
        # decision，避免恢复普通 partial decision 时用同一证据重复扣费。
        _resolve_ready_decisions(
            state,
            research_plan,
            config,
            only_unassessed=True,
            checkpoint_dir=checkpoint_dir,
            run_id=run_id,
        )
        if checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)

    # 4.5) 计划内 Research Round：控制流只读 node_assessments（unresolved / ready research）。
    # decision node 在 _resolve_ready_decisions 中消费证据，不占 worker Research Round。
    while (
        _unresolved_plan_nodes(research_plan, state)
        and state.research_rounds_completed < config.max_research_rounds
        and not _expired_deadline(_work_deadline)
        and len(state.worker_attempts) < config.max_total_tasks
    ):
        round_index = state.research_rounds_completed
        ready_research = _ready_research_nodes(research_plan, state)
        unresolved_before = _unresolved_plan_nodes(research_plan, state)
        if not ready_research:
            break
        if verbose:
            print(f"[Orchestrator] 推进 Round {round_index}：{len(ready_research)} 个 research node ready ...")
        remaining_task_budget = config.max_total_tasks - len(state.worker_attempts)
        max_round_tasks = min(config.max_tasks_per_round, remaining_task_budget)
        with timing.step("compile_ready_tasks"):
            advance = compile_ready_tasks(
                research_plan.clarified_query,
                state.evidence,
                ready_research,
                state.node_assessments,
                round_index=round_index,
                max_tasks=max_round_tasks,
                sufficient_dep_ids=_sufficient_dep_ids(research_plan, state),
                model=config.planner_model,
                provider=config.planner_provider,
                effort=config.planner_effort,
            )
        # 代码层强制：compiler 漏掉部分或全部 ready node 时，
        # 在 Research Round 容量内用 gap/objective 补 fallback；容量已满时只替换某个
        # 已拿到两个以上名额的 node，绝不突破 per-round_index/total budget。
        _result_lookup = {r.node_id: r for r in state.node_assessments}
        fallback_evidence = _allowed_evidence_from_plan(state, research_plan, ready_research)
        for rm in ready_research:
            if any(task.node_id == rm.id for task in advance.tasks):
                continue
            counts = {
                mid: sum(task.node_id == mid for task in advance.tasks)
                for mid in {task.node_id for task in advance.tasks}
            }
            if len(advance.tasks) >= max_round_tasks:
                replace_at = next((
                    index
                    for index in range(len(advance.tasks) - 1, -1, -1)
                    if counts.get(
                        advance.tasks[index].node_id or "", 0
                    ) > 1
                ), None)
                if replace_at is None:
                    continue
                advance.tasks.pop(replace_at)
            gaps = (
                list(_result_lookup[rm.id].gaps)
                if rm.id in _result_lookup and _result_lookup[rm.id].gaps
                else []
            )
            downstream_binding_values = _grounded_downstream_bindings_for_node(
                research_plan, rm, _result_lookup
            )
            fallback_objective, fallback_query, fallback_task_binding_values = (
                _fallback_research_task_text(
                    rm,
                    gaps,
                    downstream_binding_values,
                )
            )
            fallback_task = ResearchTask(
                node_id=rm.id,
                objective=fallback_objective,
                search_query=fallback_query,
                round_index=round_index,
                prerequisite_context=(
                    "必须沿用的对象或参数："
                    + "；".join(fallback_task_binding_values)
                    if fallback_task_binding_values else None
                ),
                prerequisite_evidence_ids=sorted(fallback_evidence.get(rm.id, set())),
            )
            advance.tasks.append(fallback_task)
            if verbose:
                print(f"[Orchestrator] ⚠️ Round {round_index} compiler 漏激活 {rm.id}"
                      f" → 补 fallback task")
        activated_ids = set(
            task.node_id
            for task in advance.tasks
        )
        events.emit(
            EventType.READY_SET_COMPUTED,
            round_index=round_index,
            pending=len(unresolved_before),
            reason=advance.reason,
            n_tasks=len(advance.tasks),
        )
        if not advance.tasks:
            if verbose:
                print(f"[Orchestrator] Round {round_index} 无 grounded 可执行任务 → 保留 unresolved 并停止："
                      f"{advance.reason}")
            break

        # 注入 mission context：让 worker 知道总体任务、自己的计划节点、下游依赖
        mission_contexts = {
            task.id: render_mission_context(
                research_plan, state.node_assessments,
                view="worker",
                focus_node_id=task.node_id,
                activation_count=state.node_activation_counts.get(task.node_id, 0),
            )
            for task in advance.tasks
        }

        events.emit(
            EventType.TASK_BATCH_DISPATCHED,
            round_index=round_index,
            count=len(advance.tasks),
            objectives=[q.objective for q in advance.tasks],
            phase="research",
        )
        sufficient_before_round = _sufficient_dep_ids(research_plan, state)
        prev_evidence = len(state.evidence)
        with timing.step(f"dispatch_task_batch(Round {round_index})"):
            round_reports, round_failures, round_attempts = await _dispatch_task_batch(
                advance.tasks, config, verbose=verbose, deadline=_work_deadline,
                mission_context_by_task=mission_contexts,
            )
        failures += round_failures
        state.executed_tasks.extend(advance.tasks)
        state.worker_attempts.extend(round_attempts)
        state.research_rounds_completed += 1
        state.sub_reports.extend(round_reports)
        _merge_evidence(state)

        for mid in activated_ids:
            state.node_activation_counts[mid] = \
                state.node_activation_counts.get(mid, 0) + 1
        state.pending_assessment_task_ids = [
            task.id for task in advance.tasks
        ]
        # 与 Round 0 相同：动态 worker 已计费，先提交待裁决阶段，避免 assessor 异常后
        # resume 从上一 Research Round 重派同一批任务。
        if checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)
        _assess_batch(
            state,
            research_plan,
            [node for node in research_plan.plan_nodes if node.id in activated_ids],
            tasks=advance.tasks,
            reports=round_reports,
            config=config,
        )
        state.pending_assessment_task_ids = []
        if checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)
        _resolve_ready_decisions(
            state,
            research_plan,
            config,
            checkpoint_dir=checkpoint_dir,
            run_id=run_id,
        )
        added = max(0, len(state.evidence) - prev_evidence)
        unresolved_after = _unresolved_plan_nodes(research_plan, state)
        events.emit(
            EventType.RESEARCH_ROUND_COMPLETED,
            round_index=round_index,
            added=added,
            total=len(state.evidence),
            remaining=len(unresolved_after),
        )

        if checkpoint_dir is not None:
            save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)
        if verbose:
            print(f"[Orchestrator] Round {round_index} 完成：净 +{added} 张证据，"
                  f"剩 {len(unresolved_after)} 个 unresolved node")
        sufficient_after_round = _sufficient_dep_ids(research_plan, state)
        node_progressed = bool(sufficient_after_round - sufficient_before_round)
        # 停机同时看“新证据”和“业务状态”：重复证据也可能刚好满足 completion
        # criteria，或达到有证据的 degraded 状态并解锁下一层；只有两者都没进展
        # 才算真正空转。
        if not round_reports or (added == 0 and not node_progressed):
            break

    # 4.55) 单次并行 Final Research Pass：ReadySet 结束后，只补一次当前仍有明确缺口的
    # research node。每个 node 至多一个 task；不按“是否有新增证据”继续
    # 循环。这样补查仍受总任务、每 Research Round 和墙钟预算约束，剩余缺口诚实交给 Report Plan/Writer。
    # final_research_passes_completed 也充当 resume 门：崩在 worker 后恢复时只补 assessor，不会重派。
    candidates = _final_research_pass_candidates(research_plan, state)
    if (
        candidates
        and state.final_research_passes_completed == 0
        and not _expired_deadline(_work_deadline)
        and len(state.worker_attempts) < config.max_total_tasks
    ):
        remaining_task_budget = config.max_total_tasks - len(state.worker_attempts)
        max_round_tasks = min(
            config.max_tasks_per_round,
            remaining_task_budget,
            len(candidates),
        )
        targets = candidates[:max_round_tasks]
        round_index = state.research_rounds_completed
        if verbose:
            print(
                f"[Orchestrator] Final Research Pass 并行补查 {len(targets)} 个 node"
                f"（round_index={round_index}，残留预算 {remaining_task_budget}）..."
            )
        with timing.step("compile_ready_tasks"):
            advance = compile_ready_tasks(
                research_plan.clarified_query,
                state.evidence,
                targets,
                state.node_assessments,
                round_index=round_index,
                max_tasks=max_round_tasks,
                sufficient_dep_ids=_sufficient_dep_ids(research_plan, state),
                model=config.planner_model,
                provider=config.planner_provider,
                effort=config.planner_effort,
            )
        if not _expired_deadline(_work_deadline):
            # 从此 checkpoint 起，这次 run 的 Final Research Pass 名额已经消费；即使 compiler
            # 最终没有可派 task，resume 也不能再重新编译第二批。
            state.final_research_passes_completed = 1
            # Compiler 允许一个 node 返回多条候选；Final Research Pass 的产品契约是
            # “每个缺口节点一条”，因此按 target 顺序取第一条，并对漏项补 fallback。
            compiled_by_target: dict[str, ResearchTask] = {}
            target_ids = {target.id for target in targets}
            for task in advance.tasks:
                if task.node_id in target_ids and task.node_id not in compiled_by_target:
                    compiled_by_target[task.node_id] = task
            final_research_pass_tasks: list[ResearchTask] = []
            unactionable_ids: list[str] = []
            for target in targets:
                task = compiled_by_target.get(target.id)
                if task is None:
                    task = _build_final_research_pass_fallback_task(
                        research_plan, state, target, round_index=round_index,
                    )
                if task is None:
                    _record_final_research_pass_unactionable(state, target.id)
                    unactionable_ids.append(target.id)
                    continue
                task.node_id = target.id
                task.round_index = round_index
                final_research_pass_tasks.append(task)

            _ensure_unique_task_ids(final_research_pass_tasks, state)
            final_research_pass_mission_contexts = {
                task.id: render_mission_context(
                    research_plan, state.node_assessments,
                    view="worker",
                    focus_node_id=task.node_id,
                    activation_count=state.node_activation_counts.get(
                        task.node_id, 0,
                    ),
                )
                for task in final_research_pass_tasks
            }
            events.emit(
                EventType.READY_SET_COMPUTED,
                round_index=round_index,
                pending=len(_unresolved_plan_nodes(research_plan, state)),
                reason=advance.reason or "single_parallel_final_research_pass",
                n_tasks=len(final_research_pass_tasks),
                phase="final_research_pass",
                target_node_ids=[target.id for target in targets],
                unactionable_node_ids=unactionable_ids,
            )

            if final_research_pass_tasks:
                events.emit(
                    EventType.TASK_BATCH_DISPATCHED,
                    round_index=round_index,
                    count=len(final_research_pass_tasks),
                    objectives=[q.objective for q in final_research_pass_tasks],
                    phase="final_research_pass",
                )
                prev_evidence = len(state.evidence)
                with timing.step(
                    f"dispatch_task_batch(Final Research Pass, Round {round_index})"
                ):
                    rec_reports, rec_failures, rec_attempts = await _dispatch_task_batch(
                        final_research_pass_tasks, config, verbose=verbose, deadline=_work_deadline,
                        mission_context_by_task=final_research_pass_mission_contexts,
                    )
                failures += rec_failures
                state.executed_tasks.extend(final_research_pass_tasks)
                state.worker_attempts.extend(rec_attempts)
                state.sub_reports.extend(rec_reports)
                _merge_evidence(state)
                for mid in {
                    task.node_id for task in final_research_pass_tasks if task.node_id
                }:
                    state.node_activation_counts[mid] = (
                        state.node_activation_counts.get(mid, 0) + 1
                    )

                if rec_reports:
                    state.pending_assessment_task_ids = [task.id for task in final_research_pass_tasks]
                    # worker 已计费：assessor 前先 checkpoint，resume 只补裁决。
                    if checkpoint_dir is not None:
                        save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)
                    _assess_batch(
                        state,
                        research_plan,
                        [target for target in targets if target.id in {
                            task.node_id for task in final_research_pass_tasks
                        }],
                        tasks=final_research_pass_tasks,
                        reports=rec_reports,
                        config=config,
                    )
                    state.pending_assessment_task_ids = []
                    if checkpoint_dir is not None:
                        save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)
                    _resolve_ready_decisions(
                        state,
                        research_plan,
                        config,
                        checkpoint_dir=checkpoint_dir,
                        run_id=run_id,
                    )

                added = max(0, len(state.evidence) - prev_evidence)
            else:
                added = 0
            events.emit(
                EventType.RESEARCH_ROUND_COMPLETED,
                round_index=round_index,
                added=added,
                total=len(state.evidence),
                remaining=len(_unresolved_plan_nodes(research_plan, state)),
                phase="final_research_pass",
                final_research_pass_unactionable_ids=unactionable_ids,
            )
            if checkpoint_dir is not None:
                save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)
            if verbose:
                print(
                    f"[Orchestrator] Final Research Pass Round {round_index} 完成："
                    f"{len(final_research_pass_tasks)} 个并行 task，净 +{added} 张证据"
                )

    # 4.6) post-research Report Plan（Report Plan）：计划内 Research Round 停止后才规划叙事结构。
    # 它能看到真实 result/unresolved，但不能反过来把未绑定对象注入 scheduler。
    # Report Plan 是 Writer 的唯一结构输入；若已进入写作保留窗口，使用同契约的确定性最小蓝图。
    unresolved_plan_nodes = _unresolved_plan_nodes(research_plan, state)
    if not _expired_deadline(_work_deadline):
        with timing.step("build_report_plan"):
            state.report_plan = build_report_plan(
                research_plan.clarified_query,
                evidence=state.evidence,
                node_assessments=state.node_assessments,
                decision_outputs=state.decision_outputs,
                executed_tasks=state.executed_tasks,
                unresolved_plan_nodes=unresolved_plan_nodes,
                model=config.planner_model,
                provider=config.planner_provider,
                effort=config.planner_effort,
            )
    else:
        state.report_plan = fallback_report_plan(
            research_plan.clarified_query,
            executed_tasks=state.executed_tasks,
            unresolved_plan_nodes=unresolved_plan_nodes,
            node_assessments=state.node_assessments,
        )
    n_limitations = sum(len(section.limitations) for section in state.report_plan.sections)
    events.emit(
        EventType.REPORT_PLAN,
        sections=[section.heading for section in state.report_plan.sections],
        n_limitations=n_limitations,
        unresolved_node_ids=[m.id for m in unresolved_plan_nodes],
    )
    if verbose:
        print(
            f"[Orchestrator] post-research Report Plan："
            f"{len(state.report_plan.sections)} 节 / {n_limitations} 条局限或后续研究提示 / "
            f"{len(unresolved_plan_nodes)} 个未完成计划节点"
        )

    # 5) 冻结证据后的跨 Worker 审查。它只记录覆盖风险与矛盾，绝不改变计划节点状态、
    #    创建额外 worker。恢复时复用当前证据版本已有的审计结果。
    #    enable_cross_worker_audit=False 时整段静默跳过（有意配置,不告警;skipped warning
    #    只在开着却没跑成时出现,见 _finalize_run_status）。
    cross_worker_audit: CrossWorkerAudit | None = None
    if config.enable_cross_worker_audit:
        audit_is_current = (
            state.cross_worker_audit is not None
            and state.cross_worker_audit_evidence_count == len(state.evidence)
        )
        cross_worker_audit = (
            state.cross_worker_audit if resumed and audit_is_current else None
        )
        if (state.evidence
                and not (resumed and cross_worker_audit is not None)
                and not _expired_deadline(_work_deadline)):
            if verbose:
                print("[Orchestrator] 跨 Worker 审查 → 跨研究任务核对覆盖风险 / 局限 / 矛盾 ...")
            with timing.step("cross_worker_audit"):
                audit_mc = render_mission_context(
                    research_plan, state.node_assessments, view="audit",
                )
                cross_worker_audit = run_cross_worker_audit(
                    research_plan.clarified_query,
                    state.evidence,
                    model=config.planner_model,
                    provider=config.planner_provider,  # 跨 Worker 审查=planner 档，开推理
                    effort=config.planner_effort,
                    mission_context=audit_mc,
                )
            state.cross_worker_audit = cross_worker_audit
            state.cross_worker_audit_evidence_count = len(state.evidence)
            events.emit(
                EventType.CROSS_WORKER_AUDIT,
                findings=cross_worker_audit.has_findings,
                reason=cross_worker_audit.reason,
                conflicts=[
                    {"dimension": c.dimension, "description": c.description}
                    for c in cross_worker_audit.conflicts
                ],
            )
            if verbose:
                print(
                    f"[Orchestrator]   审计 → "
                    f"{'发现风险' if cross_worker_audit.has_findings else '未见明显风险'} | "
                    f"{cross_worker_audit.reason}"
                )
                for c in cross_worker_audit.conflicts:
                    print(f"            ⚠️ 审计发现矛盾: {c.dimension} "
                          f"| 涉及 {c.card_ids} | {c.description}")
    elif verbose:
        print("[Orchestrator] 跨 Worker 审查已关闭（enable_cross_worker_audit=False）")

    # 保存审计结果；恢复时只要证据版本不变即可复用，绝不重派 worker。
    if checkpoint_dir is not None:
        save_checkpoint(state, checkpoint_dir, config=config, run_id=run_id)

    # 6) 汇总所有矛盾（子代理 + 全局），重映射 + 去重后给 writer
    # 用 state.sub_reports（恢复路径下没有局部 sub_reports 变量，state 是唯一来源）。
    # 子代理 conflicts 的 card_ids 是局部编号，必须重映射成全局编号——否则矛盾挂错
    # 证据（张冠李戴，修前 bug，见 _remap_subagent_conflicts docstring）。
    all_conflicts = _remap_subagent_conflicts(state, cross_worker_audit)

    # 7) 写报告（第0刀分组喂 writer + Report Plan 蓝图）
    authorized_evidence_ids = {
        evidence_id
        for result in state.node_assessments
        for evidence_id in result.evidence_ids
    }
    evidence_groups = _group_evidence_by_task(
        state,
        allowed_evidence_ids=authorized_evidence_ids,
    )
    writer_evidence_indices = {
        idx for _objective, idxs in evidence_groups for idx in idxs
    }
    complete_ids = _completed_node_ids(state)
    unresolved_for_writer = [
        node for node in research_plan.plan_nodes if node.id not in complete_ids
    ]
    if verbose:
        print(f"[Orchestrator] 综合 {len(writer_evidence_indices)}/{len(state.evidence)} 张已授权证据"
              f"（{len(evidence_groups)} 个研究任务分组）→ Report"
              + f" | Report Plan {len(state.report_plan.sections)} 节"
              + (f" | {len(all_conflicts)} 个矛盾点" if all_conflicts else ""))
    events.emit(
        EventType.WRITING,
        n_evidence=len(state.evidence),
        n_writer_evidence=len(writer_evidence_indices),
        n_groups=len(evidence_groups),
    )
    stream_cb = None
    if config.stream_report:
        def stream_cb(piece: str) -> None:
            print(piece, end="", flush=True)
        if verbose:
            print("\n[Orchestrator] ✍️ 流式生成报告（原始模型输出，随后渲染为正式报告）...\n")
    writer_timed_out = False
    writer_failed = False
    writer_rewrite_timed_out = False
    writer_rewrite_failed = False
    if _expired_deadline(_deadline):
        # 极端情况下控制面正好耗尽了总墙钟；仍交付已验收的证据，不把整次研究变成
        # 一个空白 failed run。
        writer_timed_out = True
        state.report = _writer_timeout_fallback(
            research_plan.clarified_query,
            state.evidence,
            allowed_indices=writer_evidence_indices,
        )
    else:
        try:
            # write_report 是同步 OpenAI 调用。放进线程后 FastAPI 的事件循环仍可处理
            # /api/runs、SSE 和取消请求；thread 会继承 llm 的 deadline context，最长也
            # 只能占用本次 writer 的受控请求预算。
            with timing.step("write_report"):
                state.report = await asyncio.to_thread(
                    write_report,
                    research_plan.clarified_query,
                    state.evidence,
                    conflicts=all_conflicts,
                    model=config.writer_model,
                    provider=config.writer_provider,
                    reasoning=config.writer_reasoning,
                    effort=config.writer_effort,
                    evidence_groups=evidence_groups,
                    report_plan=state.report_plan,
                    stream_callback=stream_cb,
                    unresolved_plan_nodes=unresolved_for_writer,
                    node_assessments=state.node_assessments,
                    max_cards_per_group=config.writer_max_cards_per_group,
                )
        except llm.LLMRequestTimeout:
            writer_timed_out = True
            state.report = _writer_timeout_fallback(
                research_plan.clarified_query,
                state.evidence,
                allowed_indices=writer_evidence_indices,
            )
            if verbose:
                print("[Orchestrator] ⏱️ 成稿请求超时 → 交付已验收证据摘要（partial）")
        except Exception as e:  # noqa: BLE001 — writer 不可用与超时同层降级
            # 单次 429/500/网络错/模型名失效(writer max_retries=0)不能崩掉整个
            # run 让全部研究成果不落盘;降级路径与超时一致,标签诚实区分。
            # CancelledError 属 BaseException,不会被这里吞——协作式取消照常穿透。
            writer_failed = True
            state.report = _writer_timeout_fallback(
                research_plan.clarified_query,
                state.evidence,
                allowed_indices=writer_evidence_indices,
            )
            if verbose:
                print(f"[Orchestrator] ❌ 成稿请求失败（{type(e).__name__}: {e}）"
                      "→ 交付已验收证据摘要（partial）")

    # 7.5) B1 刀4：确定性格式门 + 定向重写一次（+ deepdog 长度护栏防越改越短）
    final_missing: list[str] = []
    missing = format_gate(state.report, report_plan=state.report_plan)
    # 首稿已超时/失败就不要再发第二个大请求；总墙钟接近耗尽时同样保留首稿，避免重写
    # 反过来把一个可读报告拖成 failed run。
    if (missing and not writer_timed_out and not writer_failed
            and not _expired_deadline(_deadline)):
        prev_report = state.report
        prev_len = sum(len(s.markdown) for s in prev_report.sections)
        events.emit(EventType.SHAPE_GATE, phase="initial", missing=missing)
        if verbose:
            print(f"[Orchestrator] 📐 格式门：{len(missing)} 项结构缺失 → 定向重写一次")
            for m in missing:
                print(f"            ✗ {m}")
        try:
            with timing.step("write_report(shape_gate)"):
                state.report = await asyncio.to_thread(
                    write_report,
                    research_plan.clarified_query,
                    state.evidence,
                    conflicts=all_conflicts,
                    model=config.writer_model,
                    provider=config.writer_provider,
                    reasoning=config.writer_reasoning,
                    effort=config.writer_effort,
                    evidence_groups=evidence_groups,
                    report_plan=state.report_plan,
                    unresolved_plan_nodes=unresolved_for_writer,
                    node_assessments=state.node_assessments,
                    shape_feedback="\n".join(f"- {m}" for m in missing),
                    max_cards_per_group=config.writer_max_cards_per_group,
                )
        except llm.LLMRequestTimeout:
            # 格式优化失败不能污染已经成功的首稿。
            writer_rewrite_timed_out = True
            state.report = prev_report
            if verbose:
                print("[Orchestrator] ⏱️ 格式重写超时 → 保留首稿")
        except Exception as e:  # noqa: BLE001 — 重写是锦上添花,任何失败都回退首稿
            writer_rewrite_failed = True
            state.report = prev_report
            if verbose:
                print(f"[Orchestrator] ❌ 格式重写失败（{type(e).__name__}）→ 保留首稿")
        new_len = sum(len(s.markdown) for s in state.report.sections)
        if new_len < prev_len * 0.6:
            if verbose:
                print(f"[Orchestrator]   ⚠️ 重写后正文坍缩（{new_len} < 0.6×{prev_len}）→ 回退原稿")
            state.report = prev_report
        final_missing = format_gate(state.report, report_plan=state.report_plan)
        events.emit(EventType.SHAPE_GATE, phase="final", missing=final_missing)
        if verbose and final_missing:
            print(f"[Orchestrator]   ⚠️ 复检仍有 {len(final_missing)} 项结构缺失，不再重写")
    elif missing:
        # 没有尝试重写时也要把问题交给终态 warning（而非默默吞掉）。
        final_missing = missing

    # 7.6) 确定性后处理（Task 4）：终稿定格后修格式一致性——悬空引用/标题层级/空行。
    if state.report and state.report.sections:
        from dra.postprocess import postprocess_report
        state.report, _pp = postprocess_report(state.report, len(state.evidence))
        if verbose and any(_pp.values()):
            print(f"[Orchestrator] 🧹 后处理：{_pp}")

    # 7.7) 引用台账（Task 6）：报告终稿定格后，统计每组证据实际被引用多少——
    #    writer 证据→内容转化率的直接观测，恒跑不设开关（纯观测无副作用）。
    if state.report and state.report.sections:
        state.citation_audit = build_citation_audit(state.report, state.evidence, evidence_groups)
        if verbose:
            a = state.citation_audit
            print(f"[Orchestrator] 📒 引用台账：{a['n_used']}/{a['n_candidates']} 张 Writer 候选被引用（{a['used_ratio']:.0%}）")

    # 8) 终态分层：completion_blockers 决定 done/partial；warnings 只告警。
    unresolved = _unresolved_plan_nodes(research_plan, state)
    _finalize_run_status(
        state,
        research_plan,
        config=config,
        cross_worker_audit=cross_worker_audit,
        final_missing=final_missing,
        deadline_expired=_expired_deadline(_deadline),
    )
    # _finalize_run_status 重建 warnings/blockers，因此把 writer 的运行级降级结果
    # 在它之后追加。首稿成稿超时/失败虽仍有证据摘要可读，却不能冒充完整研究报告;
    # 标签按真实原因区分 timeout / failed,不互相冒充。
    if writer_timed_out or writer_failed:
        blocker = "writer_timeout" if writer_timed_out else "writer_failed"
        fallback_warn = ("writer_timeout_fallback" if writer_timed_out
                         else "writer_failed_fallback")
        state.completion_blockers = list(dict.fromkeys(
            [*state.completion_blockers, blocker]
        ))
        state.warnings = list(dict.fromkeys(
            [*state.warnings, fallback_warn]
        ))
        state.status = "partial"
    elif writer_rewrite_timed_out or writer_rewrite_failed:
        rewrite_warn = ("writer_rewrite_timeout" if writer_rewrite_timed_out
                        else "writer_rewrite_failed")
        state.warnings = list(dict.fromkeys(
            [*state.warnings, rewrite_warn]
        ))
    events.emit(
        EventType.DONE,
        status=state.status,
        n_evidence=len(state.evidence),
        completion_blockers=state.completion_blockers,
        warnings=state.warnings,
        unresolved_node_ids=[m.id for m in unresolved],
        node_terminal_reasons=state.node_terminal_reasons,
    )
    if verbose:
        print(timing.summary(time.monotonic() - _wall_t0), flush=True)
    return state
