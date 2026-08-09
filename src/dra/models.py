"""Deep Research Agent 的运行时数据契约。

设计要点（详见 DEVLOG 对应条目）：
- EvidenceCard 是贯穿全程的「证据原子」，support_quote 逐字摘录、禁止改写 → 压制幻觉。
- EvidenceCard 不直接挂 research_task_id；任务归属由 SubAgentReport / ResearchState 保留。
- typed node、Decision Resolver/Validator 与 Final Research Pass 的状态都在本文件显式建模。
- 报告小节用自由 markdown 正文（ReportSection.markdown），render 时正则扫内联 [n] 建 References。
"""

import uuid
from enum import Enum
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field


def _short_id() -> str:
    """uuid4 短码，作为各对象的可读 id。"""
    return uuid.uuid4().hex[:8]


class CurrentModel(BaseModel):
    """当前运行契约的基类：不静默吞掉已经删除的字段。"""

    model_config = ConfigDict(extra="forbid")


class ScopeResult(CurrentModel):
    """Scoping 第一阶段产物：判断原始 query 是否需要先澄清。

    P1-1a 只做候选判定，不提前构造完整 ResearchPlan。用户回答澄清问题后,
    后续步骤再生成 clarified_query / 研究任务 / 成功标准。
    """

    needs_clarification: bool
    clarification_question: str | None = None
    reason: str


class RetrievedDoc(CurrentModel):
    """一次检索召回的文档；可来自 Web provider 或本地 RAG。"""

    id: str = Field(default_factory=_short_id)
    source_url: str | None = None
    title: str
    snippet: str                       # 检索返回的摘要片段
    raw_content: str | None = None     # 整页正文（A-1 叉a，逐字门核验的 ground truth）
    condensed: str | None = None       # A-1 叉b(S)：摘要模型压出的 summary+key_excerpts，喂 LLM 的材料
    score: float = 0.0                 # 检索分（粗排）
    published_at: str | None = None    # P2-1 发布日期 ISO 字符串，从 Tavily/snippet 解析


class EvidenceCard(CurrentModel):
    """证据卡：贯穿全程的最小单元（检索/反思/写作/评测都围绕它）。

    support_quote 必须是来源原文的逐字摘录，禁止改写——这是 claim→来源
    强绑定、压制幻觉的基础。
    """

    id: str = Field(default_factory=_short_id)
    claim: str                         # 蒸馏出的一句话事实/结论
    support_quote: str                 # 原文逐字证据（禁止改写）
    source_title: str | None = None    # 来源标题；由服务端从 RetrievedDoc 复制，不让模型抄写
    source_url: str | None = None
    published_at: str | None = None    # P2-1 来源发布日期（从 RetrievedDoc 透传）


WorkerStopReason = Literal[
    "sufficient", "no_progress", "timeout", "tool_budget"
]
"""Worker 停止继续检索的原因；不等于 node 是否完成。"""


class Conflict(CurrentModel):
    """证据之间的矛盾点（P1-2）。

    由跨 Worker 审查检测，供 writer 在报告中显式呈现，
    避免不同口径/时间/定义的数字被平铺当作同一事实。
    """

    dimension: str
    """矛盾维度，如 "死亡人数"、"发布时间"、"定义"."""

    card_ids: list[int]
    """涉及的证据卡 1-based 索引."""

    description: str
    """一句话描述矛盾本质与可能原因，如 "WHO 官方统计 vs 实时统计，口径不同"."""

    severity: str = "medium"
    """矛盾严重度：high=关键数字/结论直接冲突 | medium=默认 | low=口径/表述差异。"""


class CrossWorkerAudit(CurrentModel):
    """冻结证据后的跨 Worker 覆盖与冲突审查，只影响告警与写作上下文。"""

    has_findings: bool
    reason: str
    conflicts: list[Conflict] = Field(default_factory=list)


class ReportSection(CurrentModel):
    """一个报告小节：heading + 自由 markdown 正文。

    writer 直接产 markdown 串（可含表格/H3/列表），存进 `markdown`；
    render 时正则扫内联 [n] 建 References。
    """

    heading: str
    markdown: str = ""                                     # 自由 markdown 正文
    coverage_ids: list[str] = Field(default_factory=list)    # 本节处理的 ReportPlanSection.id


class Report(CurrentModel):
    """结构化报告。markdown 由 nodes.render_report_markdown 单独渲染。"""

    title: str
    sections: list[ReportSection] = Field(default_factory=list)


class ReportPlanSection(CurrentModel):
    """post-research 报告蓝图的一节：依据已取回证据规划结构与局限提示。

    关键 grounding 约束（见 nodes.build_report_plan）：蓝图只放结构、已引用证据的概括，
    以及不能由冻结证据写实的局限/后续研究提示；不承载未经证实的结论。
    """

    id: str = Field(default_factory=_short_id)          # 语义覆盖契约的稳定 ID
    heading: str                                       # finding 导向的小节标题（规划期占位，终稿可改）
    covers: str = ""                                   # 一句话：该节要覆盖什么
    limitations: list[str] = Field(default_factory=list)
    """冻结证据尚不能写实的局限或后续研究方向；只供 Writer 如实披露。"""


class ReportPlan(CurrentModel):
    """post-research：计划 Research Round 后按真实研究结果规划报告骨架与局限。

    作用：给 writer 一个 finding 导向的叙事结构与如实披露的局限提示。
    不承载事实——事实仍 100% 来自证据，grounding 不破。
    """

    sections: list[ReportPlanSection] = Field(default_factory=list)


class NodeKind(str, Enum):
    """语义计划节点类型；写作综合不进入执行计划。"""

    RESEARCH = "research"
    DECISION = "decision"


class NodeStatus(str, Enum):
    """计划节点业务完成度；与 worker 的传输/结束状态分离。"""

    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"


class PlanNode(CurrentModel):
    """用户确认的语义计划节点，而非单条搜索 query。"""

    id: str = Field(default_factory=_short_id)
    objective: str
    kind: NodeKind = NodeKind.RESEARCH
    dependency_ids: list[str] = Field(default_factory=list)
    acceptance_criteria: str


class DecisionOutput(CurrentModel):
    """Decision Resolver 的执行产物，由代码校验是否可被下游消费。

    ``decision_summary`` 记录基于证据得出的决策；``downstream_bindings`` 只承载
    下游查询必须精确继承的实体、阈值或参数，且每个值必须在 ``decision_summary``
    中逐字出现过（控制面与正文对账）。上游 Research Assessor 负责证据充分性；
    Decision 的确定性 Validator 只保证引用授权、结构完整和控制值一致，不声称
    证明选择客观最优。

    ``contract_error`` 记录首次生成及一次原地修复后仍存在的产物契约错误。
    Decision 不再经过第二次 LLM Assessor，也不再做节点级重复激活。
    """

    node_id: str
    decision_summary: str
    evidence_ids: list[str] = Field(default_factory=list)
    downstream_bindings: dict[str, list[str]] = Field(default_factory=dict)
    contract_error: str | None = None


class NodeAssessment(CurrentModel):
    """节点验收产物；COMPLETE 或有可消费产物的 closed PARTIAL 可解锁下游。

    ``downstream_bindings`` 只承载下游查询必须精确继承的实体/阈值/参数（与
    decision_summary 逐字对账）；
    research 的 ``summary`` 只记录验收理由，不参与 ReadySet 编译；decision 的
    ``summary`` 保存真实 decision_summary，供 Compiler 恢复 binding 之间的语义关联。
    Research Assessor 的 ``node_digest`` 是面向下游 Decision Resolver、Ready Task
    Compiler 与后续 Worker 的证据结论蒸馏；
    Decision 节点不重复生成，真实决策内容同时保存在 ``DecisionOutput.decision_summary``
    与该节点 ``summary``，供后续决策和运行回放使用。
    ``gaps`` 是 partial 时列出的具体证据缺口，会传入 ReadySet 编译器指导补查。
    """

    node_id: str
    status: NodeStatus
    summary: str = ""
    node_digest: str = ""
    gaps: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    downstream_bindings: dict[str, list[str]] = Field(default_factory=dict)
    assessment_contract_error: str | None = None
    """Assessor 回执的结构/路由契约错误；不是业务证据不足。"""


class WorkerAttempt(CurrentModel):
    """一次真实 worker 派发审计；生命周期结束不等于业务成功。"""

    task_id: str
    node_id: str | None = None
    round_index: int = 0
    status: Literal["ok", "empty", "timeout", "failed", "hard_error"]
    error: str | None = None


class ResearchState(CurrentModel):
    """跨 worker、裁决、Final Research Pass、写作与恢复边界流转的全局状态。

    当前多代理路径的运行状态；字段显式记录节点验收、
    付费 worker 尝试、待恢复阶段和最终交付状态。
    """

    query: str
    evidence: list[EvidenceCard] = Field(default_factory=list)
    cross_worker_audit: CrossWorkerAudit | None = None
    cross_worker_audit_evidence_count: int | None = None
    """跨 Worker 审查实际看到的 evidence 数；resume 用它识别陈旧审计。"""
    report: Report | None = None       # 结构化 Report（writer 产出）
    status: str = "running"            # running | done | failed | partial
    # research_plan 供 CLI/调试追溯，orchestrator 路径下由 build_research_plan 填入。
    research_plan: "ResearchPlan | None" = None
    report_plan: ReportPlan | None = None   # post-research：计划 Research Round 后的报告蓝图 + 局限提示
    sub_reports: "list[SubAgentReport]" = Field(default_factory=list)
    executed_tasks: "list[ResearchTask]" = Field(default_factory=list)
    """真实派发过的执行任务；用于 post-research Report Plan、审计与恢复。"""
    raw_evidence_count: int = 0          # 去重前证据总数（运行审计用）
    citation_audit: dict | None = None  # Task 6：candidate/used 引用台账（写报告后统计）
    research_rounds_completed: int = 0             # 已完成的计划内 Research Round 数；普通并行题为 1
    final_research_passes_completed: int = 0
    """计划内 ReadySet 后是否已执行过一次并行 Final Research Pass（0 或 1）。

    不计入 research_rounds_completed；Final Research Pass batch 开始后置为 1。它也是 resume 门，防止
    中断后再次编译或重新派发同一批补查任务。
    """
    final_research_pass_unactionable_ids: list[str] = Field(default_factory=list)
    """Final Research Pass 中 PARTIAL/BLOCKED 但无法构造任务的 node ID。"""
    node_activation_counts: dict[str, int] = Field(default_factory=dict)
    """每个 node 的激活次数；research 记实际派发批次（含 Final Research Pass），decision
    记 Resolver 执行次数。用于节点重试预算与公平调度，防止某个节点垄断下游。

    superseded（已关闭重试）的 research 节点视同足以解锁下游依赖的
    “已满足”状态；decision 只有通过确定性契约校验才会解锁下游。
    """
    node_assessments: list[NodeAssessment] = Field(default_factory=list)
    decision_outputs: list[DecisionOutput] = Field(default_factory=list)
    """Decision Resolver 的最新产物；按 node ID 覆盖，供确定性校验与恢复。"""
    pending_assessment_task_ids: list[str] = Field(default_factory=list)
    """已完成付费 worker、但 node assessor 尚未成功落账的批次。

    checkpoint/resume 只重跑这个幂等裁决阶段，不重复派发对应 worker。
    """
    worker_attempts: list[WorkerAttempt] = Field(default_factory=list)
    """真实派发审计；硬异常即使没有 SubAgentReport 也不会从评测分母消失。"""
    completion_blockers: list[str] = Field(default_factory=list)
    """真正阻断业务交付的原因（如未完成计划节点、空报告）。"""
    warnings: list[str] = Field(default_factory=list)
    """运行瑕疵/质量告警；不单独把 status 打成 partial。"""
    node_terminal_reasons: dict[str, str] = Field(default_factory=dict)
    """未完成计划节点的确定性终止原因；供事件回放与 Web 解释。"""


# ---------------------------------------------------------------------------
# Scoping → Orchestrator → worker 的计划与回传数据结构
# 放在文件末尾，依赖 EvidenceCard / Conflict 等前面已定义的类型。
# ---------------------------------------------------------------------------


class ResearchTask(CurrentModel):
    """一次可派发的研究任务。

    任务由 ReadySet / Final Research Pass 编译后交给 worker，独立跑 tool loop。
    objective 给子代理 prompt 用。
    """

    id: str = Field(default_factory=_short_id)
    node_id: str                   # 所属语义计划节点
    objective: str                    # 研究任务的研究目标（一句话）
    search_query: str                 # 给 web_search 用的检索 query
    round_index: int = 0                       # 计划内 Research Round 编号；初始 ReadySet=0
    prerequisite_context: str | None = None  # 动态推进时由上游证据解析出的前置结论
    prerequisite_evidence_ids: list[str] = Field(default_factory=list)


def render_worker_objective(task: ResearchTask) -> str:
    """给 worker 的完整研究指令；展示层继续使用短 objective。

    前置结论必须同时带 evidence IDs 才算 grounded。ID 只作为代码侧血缘门槛，
    不暴露给只认识本轮 doc_id 的 worker；任一缺失就退回短目标，避免手工/旧
    payload 把无引用文本伪装成“已经确认”的研究事实。
    """
    if not task.prerequisite_context or not task.prerequisite_evidence_ids:
        return task.objective
    return (
        f"{task.objective}\n\n"
        f"【已验证的前置范围与结论】{task.prerequisite_context}\n"
        "后续检索必须沿用其中明确的对象、参数和口径；若新证据与前置结论冲突，显式记录冲突，"
        "不得静默换定义或筛选标准。"
    )


class TaskCompilation(CurrentModel):
    """Typed node scheduler 的一次 research task 编译结果。"""

    reason: str = ""
    tasks: list[ResearchTask] = Field(default_factory=list)


class ResearchPlan(CurrentModel):
    """Scoping 的最终产物，喂给 Orchestrator 做并行 dispatch。

    clarified_query 是用户澄清后的明确 query；initial_tasks 由 LLM 拆解，
    prompt 约束为当前可执行的高层证据目标。依赖上游结果的目标保留在 node DAG，
    等 ReadySet 在证据到位后编译。max_initial_tasks（默认 8）只是代码层安全硬上限。
    """

    clarified_query: str
    plan_nodes: list[PlanNode]
    initial_tasks: list[ResearchTask]


class SubAgentReport(CurrentModel):
    """单个子代理跑完后回传给 Orchestrator 的压缩结果。

    不回传完整 ResearchState（上下文隔离原则）：只回传证据卡 +
    本次反思总结，让主上下文保持精简。
    """

    research_task_id: str
    objective: str
    evidence: list[EvidenceCard] = Field(default_factory=list)
    tool_calls: int = 0
    summary: str = ""                 # 子代理对自己工作的一句话总结
    conflicts: list[Conflict] = Field(default_factory=list)
    stop_reason: WorkerStopReason | None = None
    """Worker 停止检索的最终原因；failed/hard_error 等非正常退出可为空。"""
    status: str = "ok"
    """子代理结束方式：ok=正常收敛/熔断且有证据 | empty=正常收敛但零证据（覆盖窟窿，
    触发整轮 partial，见 M7 DEVLOG 2026-07-09）| timeout=墙钟到点带部分结果 |
    failed=异常带部分结果。"""


_TRACKING_PARAM_KEYS = frozenset(
    {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "igshid"}
)
"""公认纯追踪查询参数（精确匹配，不含 utm_* 前缀——那个用 startswith 单独判）。"""


def normalize_url(url: str | None) -> str:
    """URL 归一化（仅用于比较 key，绝不改写展示/存储的原始 URL）。

    只清洗确定不承载内容语义的部分——宁可漏合并也不误合并（对齐去重
    「误杀不可逆」哲学）：
    - 协议统一（http→https 视为同页）、host 小写、剥 host 的 "www." 前缀
    - 剥路径尾部斜杠（根路径 "/" 保留语义等价于空）
    - 剥公认纯追踪查询参数（utm_* 前缀、fbclid、gclid、msclkid、mc_cid、
      mc_eid、igshid），其余查询参数原样保留（?page=2 是真内容差异）
    - 剥 #fragment（页内锚点不改变服务端内容）
    - 空/None → ""
    """
    if not url or not url.strip():
        return ""

    parts = urlsplit(url.strip())

    scheme = "https" if parts.scheme.lower() == "http" else parts.scheme.lower()

    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[len("www."):]

    path = parts.path.rstrip("/")

    kept_query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAM_KEYS
    ]
    query = urlencode(kept_query)

    return urlunsplit((scheme, netloc, path, query, ""))


def deduplicate_evidence(cards: list[EvidenceCard]) -> list[EvidenceCard]:
    """证据去重（path A：只做归一化精确去重，**不做 bi-encoder 语义聚类**）。

    为什么删掉语义去重（实测驱动）：原方案 text2vec bi-encoder + cosine≥0.85 聚类，
    两题对抗式审计误杀率 50%/100%（咖啡/老龄化），把"同主题不同事实"（不同数字/年份/
    政策/结论，如"单次200 vs 每日400""2016试点 vs 2023规模"）误判成重复杀掉——bi-encoder
    在单句 claim 粒度分不开"话题像"与"事实同"，且误杀全在高 cos 区(0.865-0.931)，调阈值
    救不回。行业调研 11/13 项目也只做精确去重、语义重复交给 writer。详见 DEVLOG 去重审计。

    取舍：真·逐字重复（同源同句、同文被抓两次）→ 精确去重挡掉；改写型"重复"（字面重叠
    低）→ 放进 writer，由它综合时自然合并（边际成本=多占点 token，可逆，远小于误杀独立
    证据的不可逆损失：证据一旦丢，writer 永远造不回，直接砍报告深度）。

    归一化精确去重：(source_url 归一化, claim 去空白+小写) 完全相同才合并，保留置信
    最高的一条。claim 侧只触碰空白/大小写、不动数字/实体 → "单次200 vs 每日400"这类
    绝不会被并；URL 侧额外做 normalize_url（协议/host大小写/www./尾斜杠/追踪参数/
    fragment）——这些差异不改变页面内容，合并它们不会误杀"事实不同"的证据，因为
    claim 侧仍要求逐字（归一化后）相同才合并，URL 变体只是让"同页真重复"更容易被
    识别到，不放宽 claim 的比较标准。
    """
    if not cards:
        return []

    def _key(c: EvidenceCard) -> tuple[str, str]:
        # 归一化：URL 侧用 normalize_url；claim 侧去所有空白 + 小写。
        # 不动数字/实体，零误杀风险。
        return (normalize_url(c.source_url), "".join((c.claim or "").lower().split()))

    kept: list[EvidenceCard] = []
    idx_by_key: dict[tuple[str, str], int] = {}
    for c in cards:
        k = _key(c)
        if k in idx_by_key:
            # 精确重复（同 url + 归一化同 claim）：保留首条。无置信度区分先后，
            # 用后到副本升级内容会让已落账的 canonical ID 悬空（NodeAssessment /
            # prerequisite downstream_binding 可能已引用），先见副本即最终内容。
            continue
        idx_by_key[k] = len(kept)
        kept.append(c)
    return kept


# 注：_semantic_cluster_dedup / MergeCluster / over_merge_rate 已于 2026-06-30 移除
# （path A 去重审计，详见 DEVLOG）。


# ResearchState 前向引用了上面定义的 ResearchPlan / SubAgentReport、
# deduplicate_evidence，
# 全部定义齐全后 rebuild 一次。
ResearchState.model_rebuild()
