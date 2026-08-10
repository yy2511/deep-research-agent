"""write_report 节点测试（V1-1a 起：write_report 返回 Report，render 单独渲染）。

本地（默认跑）：
- 空 evidence → write_report 返回空 sections 的 Report，渲染后含 fallback
- render_report_markdown：Report → markdown（含 # / ## / [n] / References）

live（--run-live 才跑）：
- 真实 evidence + LLM 调用，端到端生成 Report 并渲染，断言关键元素
"""

from unittest.mock import MagicMock

import pytest

from dra.models import (
    Conflict,
    DecisionOutput,
    ReportPlan,
    ReportPlanSection,
    EvidenceCard,
    NodeKind,
    NodeAssessment,
    NodeStatus,
    Report,
    ReportSection,
    PlanNode,
)
from dra.nodes import (
    _WRITE_SYSTEM,
    build_report_plan,
    build_markdown_report,
    fallback_report_plan,
    format_gate,
    render_report_markdown,
    write_report,
)


def _card(
    claim: str,
    url: str = "https://example.com/a",
) -> EvidenceCard:
    return EvidenceCard(
        claim=claim,
        support_quote=f"quote for: {claim}",
        source_url=url,
    )


def _report_plan() -> ReportPlan:
    """所有 Writer 调用都必须显式带同一份报告蓝图。"""
    return ReportPlan(sections=[
        ReportPlanSection(id="test-plan", heading="研究发现", covers="综合现有证据"),
    ])


# ---------------------------------------------------------------------------
# 本地测试：build + render（不调 LLM）
# ---------------------------------------------------------------------------


def test_report_and_report_plan_section_coverage_fields_have_stable_defaults():
    """Report Plan 自动获得稳定 ID；报告 coverage 初始为空。"""

    report_plan = ReportPlanSection.model_validate({"heading": "主题", "covers": "覆盖主题"})
    report = ReportSection.model_validate({"heading": "正文", "markdown": "内容"})

    assert report_plan.id
    assert report.coverage_ids == []


def test_build_markdown_report_filters_unknown_and_duplicate_coverage_ids():
    """Writer 只能回传当前大纲里的 ID；非法类型/未知值/重复值全部过滤。"""
    report = build_markdown_report(
        {
            "title": "T",
            "sections": [{
                "heading": "综合",
                "markdown": "正文",
                "coverage_ids": ["d1", "unknown", "d1", 3, "d2"],
            }],
        },
        "fallback",
        allowed_coverage_ids={"d1", "d2"},
    )

    assert report.sections[0].coverage_ids == ["d1", "d2"]


def test_empty_evidence_returns_fallback_report():
    """write_report 在空证据时返回空 sections 的 Report；渲染后是 fallback 提示。"""
    report = write_report("什么是 RAG", [], report_plan=_report_plan())
    print(f"\n[空证据] report={report!r}")
    assert isinstance(report, Report)
    assert report.title == "什么是 RAG"
    assert report.sections == []

    md = render_report_markdown(report, [])
    print(f"[空证据·渲染]\n{md}")
    assert md.startswith("# 什么是 RAG")
    assert "无证据" in md


def test_render_no_sections_returns_fallback_only():
    """Report.sections 为空 → 渲染输出 fallback，不出 References。"""
    evidence = [_card("E1")]
    report = Report(title="T")
    md = render_report_markdown(report, evidence)
    print(f"\n[空 sections]\n{md}")
    assert "## References" not in md
    assert "无证据" in md
    assert md.startswith("# T")


def test_write_report_uses_report_plan_structure(monkeypatch):
    """Report Plan 在场 → 用 finding 结构 + 局限提示作蓝图，取代泛化骨架。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    report_plan = ReportPlan(sections=[
        ReportPlanSection(heading="成本差异主导取舍", covers="对比两方案成本",
                     limitations=["微调GPU成本区间", "RAG检索成本"]),
    ])
    write_report("q", [_card("c")], report_plan=report_plan)
    prompt = chat_mock.call_args.args[0][1]["content"]
    assert "报告结构蓝图" in prompt          # 走 report_plan 蓝图分支
    assert "成本差异主导取舍" in prompt       # report_plan heading 进 prompt
    assert "微调GPU成本区间" in prompt        # 兼容字段作为局限/未来研究提示进 prompt
    assert "局限/后续研究提示" in prompt
    assert "本节须落实" not in prompt
    assert "必须补派" not in prompt


def test_write_report_owns_a_bounded_transport_budget(monkeypatch):
    """writer 允许 180s 单请求，但不叠加 chat/SDK 的传输重试。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)

    write_report("q", [_card("c")], report_plan=_report_plan())

    assert chat_mock.call_args.kwargs["request_timeout_s"] == 180.0
    assert chat_mock.call_args.kwargs["max_retries"] == 0


def test_write_report_uses_report_plan_ids_and_allows_execution_groups_to_merge(monkeypatch):
    """语义大纲 ID 驱动章节；执行分组只是材料来源，不能变成章节清单。"""
    from dra.models import ReportPlan, ReportPlanSection

    chat_mock = MagicMock(return_value='{"title":"T","sections":[]}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    report_plan = ReportPlan(sections=[
        ReportPlanSection(id="period", heading="时期比较", covers="比较三个时期"),
    ])

    write_report(
        "q",
        [_card("c")],
        report_plan=report_plan,
        evidence_groups=[("查学者甲", [1]), ("补查学者乙", [1])],
    )

    prompt = chat_mock.call_args.args[0][1]["content"]
    assert "period" in prompt
    assert "检索来源分组" in prompt
    assert "不是报告章节清单" in prompt
    assert "coverage_ids" in prompt
    assert "只能填写用户消息提供的 section_id" in prompt


def test_write_report_uses_report_plan_not_completed_node_ledger(monkeypatch):
    """完成节点的选择关系由 Report Plan 传递，Writer 不再重复读取扁平节点账本。"""
    chat_mock = MagicMock(return_value='{"title":"T","sections":[]}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    report_plan = ReportPlan(sections=[ReportPlanSection(
        id="selected",
        heading="候选比较",
        covers="比较 Milvus 与 Qdrant",
    )])
    write_report(
        "选择两个项目并比较",
        [_card("Milvus 与 Qdrant 是候选")],
        report_plan=report_plan,
        unresolved_plan_nodes=[PlanNode(
            id="compare",
            objective="完成统一维度比较",
            kind=NodeKind.RESEARCH,
            dependency_ids=["select"],
            acceptance_criteria="有引用的对比结论",
        )],
        node_assessments=[NodeAssessment(
            node_id="compare",
            status=NodeStatus.PARTIAL,
            summary="已有性能证据，但价格口径尚不一致（证据2、4、5）。",
            gaps=["补查 Milvus 与 Qdrant 2026 年官方托管价格"],
        )],
    )

    prompt = chat_mock.call_args.args[0][1]["content"]
    assert "计划节点裁决结果" not in prompt
    assert "Milvus" in prompt and "Qdrant" in prompt
    assert "Pinecone" not in prompt
    assert "现有证据尚未完成的研究目标" in prompt
    assert "完成统一维度比较" in prompt
    assert "完成要求：有引用的对比结论" in prompt
    assert "最新验收说明：已有性能证据，但价格口径尚不一致。" in prompt
    assert "此刻具体缺口：补查 Milvus 与 Qdrant 2026 年官方托管价格" in prompt
    assert "acceptance_criteria" not in prompt
    assert "不得宣称已完成" in prompt


def test_report_plan_resolves_claims_without_exposing_global_evidence_ids(monkeypatch):
    """Report Planner 使用代码解析后的 claim，不需要看到 EvidenceCard.id。"""
    call = MagicMock(return_value={
        "sections": [{"heading": "候选比较", "covers": "比较候选方案"}],
    })
    monkeypatch.setattr("dra.nodes.call_json", call)
    evidence = [EvidenceCard(
        id="deadbeef",
        claim="Milvus 与 Qdrant 均进入候选",
        support_quote="Milvus and Qdrant are shortlisted.",
        source_url="https://example.com/source",
    )]

    build_report_plan(
        "比较候选方案",
        evidence=evidence,
        node_assessments=[NodeAssessment(
            node_id="select",
            status=NodeStatus.COMPLETE,
            summary="候选已选定",
            evidence_ids=["deadbeef"],
            downstream_bindings={"selected": ["Milvus", "Qdrant"]},
        )],
    )

    prompt = call.call_args.args[0][1]["content"]
    assert "Milvus 与 Qdrant 均进入候选" in prompt
    assert "deadbeef" not in prompt


def test_report_plan_receives_latest_unresolved_summary_and_gaps(monkeypatch):
    """未完成节点必须把最后验收结论和可执行缺口传给 Report Plan。"""
    call = MagicMock(return_value={
        "sections": [{"heading": "局限", "covers": "说明真实未完成项"}],
    })
    monkeypatch.setattr("dra.nodes.call_json", call)
    node = PlanNode(
        id="market",
        objective="核验市场可行性",
        kind=NodeKind.RESEARCH,
        acceptance_criteria="覆盖价格、周期和合规",
    )

    build_report_plan(
        "比较市场方向",
        node_assessments=[NodeAssessment(
            node_id="market",
            status=NodeStatus.PARTIAL,
            summary="价格和周期已覆盖，仍缺合规材料（证据2、4、5、6）。",
            gaps=["补查 AI 视频平台 2026 年商业使用与人物肖像规则"],
        )],
        unresolved_plan_nodes=[node],
    )

    prompt = call.call_args.args[0][1]["content"]
    assert "latest_assessment_summary=价格和周期已覆盖，仍缺合规材料。" in prompt
    assert "补查 AI 视频平台 2026 年商业使用与人物肖像规则" in prompt
    assert "（、4、5、6）" not in prompt


def test_fallback_report_plan_preserves_latest_unresolved_gaps():
    """Report Plan 模型失败时，确定性蓝图也不能退回泛化 objective。"""
    node = PlanNode(
        id="market",
        objective="核验市场可行性",
        kind=NodeKind.RESEARCH,
        acceptance_criteria="覆盖价格与合规",
    )

    plan = fallback_report_plan(
        "比较市场方向",
        unresolved_plan_nodes=[node],
        node_assessments=[NodeAssessment(
            node_id="market",
            status=NodeStatus.PARTIAL,
            summary="已有价格证据（证据2、4），仍缺合规。",
            gaps=["补查 2026 年平台商业使用规则"],
        )],
    )

    limitation = plan.sections[-1].limitations[0]
    assert "最新验收说明：已有价格证据，仍缺合规。" in limitation
    assert "具体缺口：补查 2026 年平台商业使用规则" in limitation
    assert "证据2" not in limitation


def test_write_system_allows_citations_in_fact_cells():
    """价格等承重事实应在表格单元格就近引用，不再强制正文机械复述。"""
    assert "【表格就近引用】" in _WRITE_SYSTEM
    assert "表格不挂引用" not in _WRITE_SYSTEM


def test_report_plan_preserves_completed_terminal_decision_without_bindings(monkeypatch):
    """终端 Decision 即使没有下游 binding，报告规划也必须沿用其排序而非重新决策。"""
    call = MagicMock(return_value={
        "sections": [{"heading": "推荐排序", "covers": "沿用已确认排序"}],
    })
    monkeypatch.setattr("dra.nodes.call_json", call)
    evidence = [EvidenceCard(
        id="e1",
        claim="Alpha 与 Beta 均有付费需求",
        support_quote="Alpha and Beta both have paid demand.",
        source_url="https://example.com/source",
    )]

    build_report_plan(
        "比较并推荐两个方向",
        evidence=evidence,
        node_assessments=[NodeAssessment(
            node_id="rank",
            status=NodeStatus.COMPLETE,
            summary="第一优先级 Alpha，第二优先级 Beta。",
            evidence_ids=["e1"],
        )],
        decision_outputs=[DecisionOutput(
            node_id="rank",
            decision_summary="第一优先级 Alpha，第二优先级 Beta【1】。",
            evidence_ids=["e1"],
            downstream_bindings={},
        )],
    )

    system_prompt = call.call_args.args[0][0]["content"]
    user_prompt = call.call_args.args[0][1]["content"]
    assert "不得绕过它重新决策或自行改序" in system_prompt
    assert "verified_decision_summary=第一优先级 Alpha，第二优先级 Beta。" in user_prompt
    assert "【1】" not in user_prompt


def test_writer_hides_node_global_evidence_ids_but_keeps_local_citations(monkeypatch):
    """Writer 只使用证据清单 [n] 和 section_id，不接收全局卡片或计划节点账本 ID。"""
    chat_mock = MagicMock(return_value='{"title":"T","sections":[]}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    evidence = [EvidenceCard(
        id="deadbeef",
        claim="Milvus 与 Qdrant 均进入候选",
        support_quote="Milvus and Qdrant are shortlisted.",
        source_url="https://example.com/source",
    )]

    write_report(
        "比较候选方案",
        evidence,
        report_plan=_report_plan(),
    )

    prompt = chat_mock.call_args.args[0][1]["content"]
    assert "deadbeef" not in prompt
    assert "[1] date=" in prompt
    assert "section_id=test-plan" in prompt
    assert "downstream_bindings" not in prompt


def test_write_system_no_hardcoded_year_anchor():
    """writer prompt 不再把 2024 当"最新"硬写进规则/范文（会把时效标注带回过去年份）。"""
    from dra.nodes import _WRITE_SYSTEM

    assert "2024" not in _WRITE_SYSTEM


def test_write_system_output_example_includes_coverage_ids():
    """示例不能和正式 JSON schema 打架，否则模型会照旧例漏掉 coverage IDs。"""
    example = _WRITE_SYSTEM.split("【输出示例】", 1)[1]
    assert example.count('"coverage_ids"') == 3


def test_format_gate_rejects_letter_citation_but_preserves_normal_bracket_terms():
    plan = _report_plan()
    report = Report(title="T", sections=[ReportSection(
        heading="执行摘要",
        markdown="事实成立[1][E]，术语 [RFC] 保持原样。",
        coverage_ids=[plan.sections[0].id],
    )])

    missing = format_gate(report, report_plan=plan)

    assert len(missing) == 1
    assert "非法引用标记" in missing[0]
    assert "[E]" in missing[0]
    assert "[RFC]" not in missing[0]


def test_write_report_injects_current_date(monkeypatch):
    """write_report 必须把当前日期注入 prompt，writer 才会按真实"今天"标时效。"""
    from datetime import datetime

    from dra.nodes import _today_str

    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    write_report("最新数据调研", [_card("某事实")], report_plan=_report_plan())
    user_prompt = chat_mock.call_args.args[0][1]["content"]
    assert _today_str() in user_prompt
    assert str(datetime.now().year) in user_prompt


# ---------------------------------------------------------------------------
# S1+S3 Stage A：自由 markdown 报告（新路径）
# ---------------------------------------------------------------------------


def test_build_markdown_report_drops_empty_sections():
    """{title, sections:[{heading, markdown}]} → Report；空 markdown 节丢弃；不填 claims。"""
    data = {
        "title": "RAG 综述",
        "sections": [
            {"heading": "执行摘要", "markdown": "核心结论：RAG 减少幻觉[1]。"},
            {"heading": "空节", "markdown": "   "},  # 空 → 丢
            {"heading": "对比", "markdown": "| 方法 | 召回 |\n|---|---|\n| A | 高 |"},
        ],
    }
    report = build_markdown_report(data, fallback_title="兜底")
    print(f"\n[markdown build] headings={[s.heading for s in report.sections]}")
    assert report.title == "RAG 综述"
    assert [s.heading for s in report.sections] == ["执行摘要", "对比"]
    assert report.sections[0].markdown == "核心结论：RAG 减少幻觉[1]。"


def test_build_markdown_report_empty_title_uses_fallback():
    data = {"title": "  ", "sections": [{"heading": "S", "markdown": "正文[1]"}]}
    report = build_markdown_report(data, fallback_title="兜底标题")
    assert report.title == "兜底标题"


def test_build_markdown_report_strips_internal_placeholders_only():
    """内部流程标记不得泄漏进成稿；合法数字引用和普通 Markdown 保留。"""
    report = build_markdown_report(
        {
            "title": "比较报告【待补充】",
            "sections": [{
                "heading": "权衡[冲突项]",
                "markdown": "推理成本可能较高 [矛盾项]，但已有案例支持[1]。保留 [RFC]。",
                "coverage_ids": [],
            }],
        },
        fallback_title="兜底",
        allowed_citation_ids={1},
    )

    assert report.title == "比较报告"
    assert report.sections[0].heading == "权衡"
    assert report.sections[0].markdown == "推理成本可能较高，但已有案例支持[1]。保留 [RFC]。"


def test_render_markdown_section_with_table_and_inline_citations():
    """自由 markdown 节：表格原样透传 + 正文内联 [n] + 正则扫出 References。"""
    evidence = [_card("E1", "https://a.example/1"), _card("E2", "https://b.example/2")]
    report = Report(
        title="咖啡报告",
        sections=[
            ReportSection(heading="执行摘要", markdown="核心结论：适量咖啡可能护心[1]。"),
            ReportSection(
                heading="证据对比",
                markdown="| 维度 | 结论 |\n|---|---|\n| 短期 | 提神 |\n\n"
                         "短期作用以提神为主[1]，长期则与心血管获益相关[2]。",
            ),
        ],
    )
    md = render_report_markdown(report, evidence)
    print(f"\n[markdown render]\n{md}")
    assert "## 执行摘要" in md
    assert "核心结论：适量咖啡可能护心[1]。" in md
    assert "| 维度 | 结论 |" in md          # 表格透传
    assert "## References" in md
    assert "[1] https://a.example/1" in md
    assert "[2] https://b.example/2" in md


def test_render_markdown_out_of_range_citation_ignored_in_references():
    """正文里的越界 [n] 不进 References（只收 1..len(evidence)）。"""
    evidence = [_card("E1", "https://a/1")]
    report = Report(title="T", sections=[
        ReportSection(heading="S", markdown="一句话[1]，另一句越界引用[9]。"),
    ])
    md = render_report_markdown(report, evidence)
    assert "[1] https://a/1" in md
    assert "[9] " not in md  # References 里不出现越界源


def test_write_report_returns_markdown_sections(monkeypatch):
    """write_report 走新解析：LLM 返回 {heading, markdown} → ReportSection.markdown 填好。"""
    monkeypatch.setattr("dra.llm.chat", MagicMock(return_value=(
        '{"title":"T","sections":[{"heading":"执行摘要","markdown":"结论 X[1]。"},'
        '{"heading":"正文","markdown":"| a | b |\\n|---|---|"}]}'
    )))
    evidence = [_card("E1")]
    report = write_report("Q", evidence, report_plan=_report_plan())
    print(f"\n[write→markdown] {[(s.heading, s.markdown[:20]) for s in report.sections]}")
    assert [s.heading for s in report.sections] == ["执行摘要", "正文"]
    assert report.sections[0].markdown == "结论 X[1]。"


# ---------------------------------------------------------------------------
# live 测试：端到端 LLM 调用
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_write_report_end_to_end_with_real_llm():
    """真实 evidence → LLM 写 → Report → markdown。"""
    evidence = [
        EvidenceCard(
            claim="RAG 通过检索外部知识减少幻觉",
            support_quote="检索增强生成（RAG）通过语义相似性从外部知识库检索相关文档块，从而增强 LLM",
            source_url="https://cloud.google.com/use-cases/retrieval-augmented-generation",
        ),
        EvidenceCard(
            claim="RAG 让 LLM 能用上实时信息",
            support_quote="RAG 技术通过为 LLM 提供实时更新的信息，有效克服了这一局限性",
            source_url="https://aws.amazon.com/what-is/retrieval-augmented-generation/",
        ),
    ]
    q = "什么是 RAG，它解决了 LLM 的什么问题？"
    report = write_report(q, evidence, report_plan=_report_plan())

    assert isinstance(report, Report)
    assert report.sections, "应至少出 1 个 section"
    # S1+S3：每节是自由 markdown 串
    assert all(s.markdown for s in report.sections), "每节应有 markdown 正文"

    md = render_report_markdown(report, evidence)
    print(f"\n{'=' * 60}\n{md}{'=' * 60}")

    assert md.startswith("# ")
    assert "## " in md
    assert "## References" in md
    import re
    cited = {int(m) for m in re.findall(r"\[(\d+)\]", md)}
    assert cited and all(1 <= i <= len(evidence) for i in cited), "正文应有合法内联 [n] 引用"


# ---------------------------------------------------------------------------
# P1-2：conflict 传入 writer
# ---------------------------------------------------------------------------


def test_write_report_passes_conflicts_to_prompt(monkeypatch):
    """P1-2 契约：Writer prompt 必须包含跨 Worker 审查检测到的矛盾信息。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    evidence = [
        _card("COVID 死亡 690 万"),
        _card("COVID 死亡 700 万"),
    ]
    conflicts = [
        Conflict(
            dimension="死亡人数",
            card_ids=[1, 2],
            description="WHO 官方 vs 实时统计，口径不同",
        ),
    ]

    write_report("COVID 死亡人数", evidence, conflicts=conflicts, report_plan=_report_plan())

    user_prompt = chat_mock.call_args.args[0][1]["content"]
    print(f"\n[Writer conflict 输入]\n{user_prompt}")
    assert "【证据矛盾】" in user_prompt
    assert "死亡人数" in user_prompt
    assert "WHO 官方 vs 实时统计" in user_prompt
    assert "[1, 2]" in user_prompt


def test_write_report_no_conflict_section_when_none(monkeypatch):
    """conflicts=None 时 prompt 不含冲突段落（向后兼容）。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    evidence = [_card("RAG 减少幻觉")]

    write_report("RAG", evidence, conflicts=None, report_plan=_report_plan())

    user_prompt = chat_mock.call_args.args[0][1]["content"]
    print(f"\n[Writer 无冲突]\n{user_prompt}")
    assert "【证据矛盾】" not in user_prompt


# ---------------------------------------------------------------------------
# 第0刀：evidence_groups 按研究任务分组喂 writer（恢复 _merge_evidence 拍平丢的结构）
# ---------------------------------------------------------------------------


def test_write_report_grouped_listing_in_prompt(monkeypatch):
    """给 evidence_groups → prompt 按研究任务分块（### 标题）+ 组织提示；全局编号不变。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    evidence = [
        _card("咖啡因提神", "https://a/1"),
        _card("咖啡因影响睡眠", "https://a/2"),
        _card("适量咖啡护心", "https://b/1"),
    ]
    # 研究任务 A 占全局 [1,2]、研究任务 B 占全局 [3]
    groups = [("咖啡因的短期作用", [1, 2]), ("咖啡的长期健康影响", [3])]

    write_report("咖啡对健康的影响", evidence, evidence_groups=groups, report_plan=_report_plan())

    user_prompt = chat_mock.call_args.args[0][1]["content"]
    print(f"\n[Writer 分组输入]\n{user_prompt}")
    assert "### 研究任务：咖啡因的短期作用" in user_prompt
    assert "### 研究任务：咖啡的长期健康影响" in user_prompt
    # 全局编号保留（不重排重编号）：[3] 对应第三张卡的 claim
    assert "[1] date=" in user_prompt
    assert "[3] date=" in user_prompt
    assert "claim=适量咖啡护心" in user_prompt.split("### 研究任务：咖啡的长期健康影响")[1]
    # 组织提示 + 反「物理隔离杀 insight」的关键句
    assert "【组织要求】" in user_prompt
    assert "不限本组" in user_prompt


def test_write_report_flat_listing_without_groups(monkeypatch):
    """不给 evidence_groups → 退回扁平 listing（无 ### 块、无组织提示），向后兼容。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    evidence = [_card("E1"), _card("E2")]

    write_report("Q", evidence, report_plan=_report_plan())

    user_prompt = chat_mock.call_args.args[0][1]["content"]
    assert "### 研究任务：" not in user_prompt
    assert "【组织要求】" not in user_prompt
    assert "[1] date=" in user_prompt
    assert "[2] date=" in user_prompt


def test_write_report_listing_includes_server_owned_source_title(monkeypatch):
    """Writer 看得到来源语境；quote 单行有界且完整 URL 不重复进入 prompt。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    evidence = [EvidenceCard(
        claim="团队复盘涉及 RAG 项目取舍",
        support_quote="前" * 140 + "不应进入本次 Writer 输入的尾部限定",
        source_title="某团队的 Agent 工程实践复盘",
        source_url="https://example.com/agent-practice",
    )]

    write_report("Q", evidence, report_plan=_report_plan())

    user_prompt = chat_mock.call_args.args[0][1]["content"]
    assert "title=某团队的 Agent 工程实践复盘" in user_prompt
    assert "不应进入本次 Writer 输入的尾部限定" in user_prompt
    assert "url=https://example.com/agent-practice" not in user_prompt


def test_writer_quote_excerpt_is_single_line_and_ends_at_boundary(monkeypatch):
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    evidence = [EvidenceCard(
        claim="长摘录中的关键事实",
        support_quote=("第一段包含主体和背景。\n" + "第二段继续给出数据与口径。" * 30),
        source_url="https://example.com/long?token=noise",
    )]

    write_report("Q", evidence, report_plan=_report_plan())

    prompt = chat_mock.call_args.args[0][1]["content"]
    evidence_line = next(line for line in prompt.splitlines() if line.startswith("[1] date="))
    assert "quote_excerpt=" in evidence_line
    assert evidence_line.endswith(" …")
    assert "\n" not in evidence_line
    assert "token=noise" not in prompt


def test_writer_rewrite_feedback_does_not_claim_unseen_previous_draft(monkeypatch):
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)

    write_report(
        "Q",
        [_card("E1")],
        report_plan=_report_plan(),
        shape_feedback="- 缺执行摘要",
    )

    prompt = chat_mock.call_args.args[0][1]["content"]
    assert "本次重写必须补齐" in prompt
    assert "请保留已有内容" not in prompt
    assert "重新生成完整报告" in prompt


@pytest.mark.live
def test_writer_presents_conflict_explicitly():
    """P1-2 live 验收：给矛盾证据 → 报告中应有冲突呈现，不平铺合并。"""
    evidence = [
        EvidenceCard(
            claim="COVID-19 全球死亡约 690 万",
            support_quote="WHO 报告 COVID-19 全球死亡约 690 万例。",
            source_url="https://www.who.int/covid-deaths",
        ),
        EvidenceCard(
            claim="COVID-19 全球死亡约 700 万",
            support_quote="实时统计 COVID-19 全球死亡约 700 万例。",
            source_url="https://www.worldometers.info/coronavirus/",
        ),
    ]
    conflicts = [
        Conflict(
            dimension="死亡人数",
            card_ids=[1, 2],
            description="WHO 官方统计 vs 实时统计，口径不同导致数字差异",
        ),
    ]

    report = write_report("COVID-19 全球死亡人数", evidence, conflicts=conflicts, report_plan=_report_plan())
    md = render_report_markdown(report, evidence)
    print(f"\n[live·冲突呈现]\n{md}")

    assert report.sections, "应至少生成一个 section"
    # 期望报告里出现"WHO"和"700"两个数字，且不并成同一个人
    assert "WHO" in md or "690" in md
    assert "700" in md


# ---------------------------------------------------------------------------
# 四方对比修复②（A2）：inline 机构+日期归因
# ---------------------------------------------------------------------------


def test_fmt_card_carries_source_domain(monkeypatch):
    """A2：证据行必须带 src=域名——inline 机构归因的信息管道（域名是机构名的原料）。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    ev = [EvidenceCard(claim="全球AI支出2.5万亿", support_quote="q",
                       source_url="https://www.iea.org/reports/x",
                       published_at="2026-03-01")]

    write_report("q", ev, report_plan=_report_plan())

    user_prompt = chat_mock.call_args.args[0][1]["content"]
    assert "src=iea.org" in user_prompt
    assert "date=2026-03-01" in user_prompt


def test_fmt_card_domain_missing_url(monkeypatch):
    """A2：无 source_url 的卡 src=未知，不抛错。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    ev = [EvidenceCard(claim="c", support_quote="q", source_url=None)]

    write_report("q", ev, report_plan=_report_plan())

    user_prompt = chat_mock.call_args.args[0][1]["content"]
    assert "src=未知" in user_prompt


def test_write_system_requires_inline_attribution():
    """A2 prompt 契约：inline 归因规则在场（行为由同题复评验，此测试防静默撤销）。"""
    assert "inline 归因" in _WRITE_SYSTEM


def test_write_system_forbids_invented_evidence_sample_counts():
    """Writer 不得把自己数出的证据卡/案例条数包装成正式样本量。"""
    assert "证据卡数量" in _WRITE_SYSTEM
    assert "不是来源发布的业务统计" in _WRITE_SYSTEM


# ---------------------------------------------------------------------------
# 四方对比修复③④（A3+A4）：金字塔件 + 空壳节合并
# ---------------------------------------------------------------------------


def test_write_system_has_pyramid_sections():
    """A4 prompt 契约：关键发现 + 结论与展望 + 局限收拢三件套在场。"""
    assert "关键发现" in _WRITE_SYSTEM
    assert "结论与展望" in _WRITE_SYSTEM
    assert "局限与口径说明" in _WRITE_SYSTEM


def test_write_system_forbids_hollow_sections():
    """A3 prompt 契约：空壳节合并规则在场（v1 报告「现有证据未提供」8+ 次的病根）。"""
    assert "并入最相关的邻节" in _WRITE_SYSTEM


def test_blueprint_does_not_duplicate_global_structure_rules(monkeypatch):
    """金字塔结构由 system prompt 统一规定；本次蓝图只承载语义大纲。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    report_plan = ReportPlan(sections=[
        ReportPlanSection(heading="h1", covers="c1", limitations=["r1"]),
    ])

    write_report("q", [_card("c")], report_plan=report_plan)

    user_prompt = chat_mock.call_args.args[0][1]["content"]
    assert "关键发现" not in user_prompt
    assert "结论与展望" not in user_prompt
    assert "禁止空壳独立成节" not in user_prompt
    assert "section_id=" in user_prompt
    assert "局限/后续研究提示：r1" in user_prompt
    assert "关键发现" in _WRITE_SYSTEM
    assert "结论与展望" in _WRITE_SYSTEM
    assert "并入最相关的邻节" in _WRITE_SYSTEM


def test_write_report_unanchored_conflict_renders_without_empty_ids(monkeypatch):
    """无锚矛盾（worker finish 申报，card_ids=[]）不渲染「涉及证据 []」空壳。"""
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    evidence = [_card("融资 up to $1.4B"), _card("融资另一口径")]
    conflicts = [
        Conflict(dimension="融资规模口径", card_ids=[],
                 description="来源 A 与来源 B 数字不一致", severity="high"),
    ]

    write_report("融资情况", evidence, conflicts=conflicts, report_plan=_report_plan())

    user_prompt = chat_mock.call_args.args[0][1]["content"]
    assert "【证据矛盾】" in user_prompt
    assert "来源 A 与来源 B 数字不一致" in user_prompt
    assert "涉及证据 []" not in user_prompt


def test_writer_drops_conflict_whose_anchored_evidence_was_not_listed(monkeypatch):
    chat_mock = MagicMock(return_value='{"title": "T", "sections": []}')
    monkeypatch.setattr("dra.llm.chat", chat_mock)
    evidence = [_card("已授权事实"), _card("未授权事实")]
    conflicts = [Conflict(
        dimension="未授权冲突",
        card_ids=[2],
        description="不能绕过证据过滤进入 Writer",
        severity="high",
    )]

    write_report(
        "核验",
        evidence,
        evidence_groups=[("已授权任务", [1])],
        conflicts=conflicts,
        report_plan=_report_plan(),
    )

    user_prompt = chat_mock.call_args.args[0][1]["content"]
    assert "未授权冲突" not in user_prompt
    assert "不能绕过证据过滤进入 Writer" not in user_prompt
