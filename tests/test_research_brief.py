"""研究开始前的轻量规划：目标 / 硬要求 + 初步路线 + 是否需要人工确认。

对应 EXPERIMENT_PLAN §7 第 1 项。这里锁住三件事：

1. **服务端只透传**模型给出的 `needs_confirmation`，不重新判断——"简单问题直接跑"
   是模型的判断，不是服务端偷偷加的第二道裁决。
2. 原始问题身份不被改写，修订时未改变的要求保留稳定 id（沿用 initial_planner 的既有契约）。
3. brief **不构建计划状态**：它是路线，不是节点，不参与调度。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dra.runtime_config import BriefConfig
from dra.research_brief import (
    BriefGap,
    ResearchBrief,
    assess_research_gaps,
    build_research_brief,
    format_gap_answers,
    render_brief_context,
)
from dra.simple_research import Requirement

QUERY = "对比 A 和 B 两个方案，给出选型建议"


def config():
    return BriefConfig(max_initial_tasks=4)


def payload(
    *,
    query: str = QUERY,
    needs_confirmation: bool = True,
    requirements: list[dict] | None = None,
    route: list[dict] | None = None,
    assumptions: list[str] | None = None,
) -> dict:
    return {
        "original_query": query,
        "objective": "说清 A 与 B 的取舍并给出建议",
        "requirements": (
            requirements
            if requirements is not None
            else [{"id": "goal", "text": query, "kind": "goal"}]
        ),
        "route": (
            route
            if route is not None
            else [
                {"id": "capability", "text": "先看清两者的能力边界"},
                {"id": "cost", "text": "再比成本与维护代价"},
            ]
        ),
        "needs_confirmation": needs_confirmation,
        "confirmation_reason": "目标宽泛，取舍会改变路线" if needs_confirmation else "问题具体",
        "assumptions": assumptions or [],
    }


def gaps_payload(*, query: str = QUERY, gaps: list[dict] | None = None) -> dict:
    return {
        "original_query": query,
        "gaps": (
            gaps
            if gaps is not None
            else [
                {
                    "id": "scale",
                    "header": "确认",
                    "question": "服务的规模大概是哪一档？这决定对比的重点是运维成本还是能力。",
                    "options": [
                        {"label": "日活 10 万以内 (Recommended)", "description": "重点会放在迁移成本与运维收益。"},
                        {"label": "日活百万级", "description": "重点会放在扩展性与高可用方案。"},
                    ],
                }
            ]
        ),
    }


def build(data: dict, *, query: str = QUERY, **kwargs) -> ResearchBrief:
    return build_research_brief(query, config(), model_call=lambda *a, **k: data, **kwargs)


# --- 确认判断：服务端只透传 -------------------------------------------------


@pytest.mark.parametrize("needs", [True, False])
def test_brief_passes_through_model_confirmation_decision(needs):
    """简单 / 复杂由模型判，服务端不得改写——否则"直接跑"会变成服务端自己猜。"""
    brief = build(payload(needs_confirmation=needs))
    assert brief.needs_confirmation is needs
    assert brief.confirmation_reason.strip()


def test_brief_keeps_route_and_objective_verbatim():
    brief = build(payload())
    assert brief.objective == "说清 A 与 B 的取舍并给出建议"
    assert [item.id for item in brief.route] == ["capability", "cost"]


# --- 身份与契约 -------------------------------------------------------------


def test_brief_rejects_empty_query_without_calling_model():
    def _must_not_run(*args, **kwargs):
        raise AssertionError("空问题不应触达模型")

    with pytest.raises(ValueError, match="不能为空"):
        build_research_brief("   ", config(), model_call=_must_not_run)


def test_brief_keeps_server_query_when_model_miscopies_it():
    """模型抄原问题漏字时不失败，计划里的原问题以服务端为准。"""
    assert build(payload(query="被改写的问题")).original_query == QUERY


def test_brief_rejects_previous_from_another_query():
    other = build(payload(query="另一个问题"), query="另一个问题")
    with pytest.raises(ValueError, match="身份不匹配"):
        build_research_brief(QUERY, config(), previous=other, model_call=lambda *a, **k: payload())


def test_brief_trims_route_beyond_configured_limit():
    """路线超出上限只截取前面的，不让整份计划作废。"""
    route = [{"id": f"r{i}", "text": f"方向 {i}"} for i in range(5)]  # 上限 4
    assert [r.id for r in build(payload(route=route)).route] == ["r0", "r1", "r2", "r3"]


@pytest.mark.parametrize(
    "requirements, expected",
    [
        ([], [("goal", QUERY, "goal")]),  # 没给要求：以原问题作为目标
        ([{"id": "goal", "text": "", "kind": "goal"}], [("goal", QUERY, "goal")]),  # 空文本的条目丢弃
        ([{"id": "goal", "text": QUERY, "kind": "delivery"}], [("goal", QUERY, "goal")]),  # 认不出的类型按 goal
        ([{"id": "bad id", "text": QUERY, "kind": "goal"}], [("req1", QUERY, "goal")]),  # 编号不合法由服务端重编
        ([{"id": "目标", "text": "甲", "kind": "goal"}, {"id": "目标", "text": "乙", "kind": "constraint"}],
         [("req1", "甲", "goal"), ("req2", "乙", "constraint")]),
    ],
)
def test_brief_repairs_requirement_mistakes(requirements, expected):
    """要求里的小错就地修正，不让整份计划作废。"""
    brief = build(payload(requirements=requirements))
    assert [(r.id, r.text, r.kind) for r in brief.requirements] == expected


@pytest.mark.parametrize("value, expected", [("false", False), ("True", True), ("否", False), ("随便", True), (None, True)])
def test_brief_reads_confirmation_flag_written_as_text(value, expected):
    """「要不要确认」写成字符串时按意思转换；看不懂时保守地要求确认。"""
    assert build(payload(needs_confirmation=value)).needs_confirmation is expected


def test_brief_renumbers_route_ids_the_server_cannot_use():
    route = [{"id": "查游戏", "text": "先找候选"}, {"id": "step 1", "text": "再核对年份"}]
    assert [r.id for r in build(payload(route=route)).route] == ["r1", "r2"]


def test_brief_raises_when_planner_returns_nothing():
    """call_json 穷尽重试后返回 {}：如实报错，不能静默当成"不需要确认"直接开跑。"""
    with pytest.raises(ValueError, match="没有返回可用结果"):
        build_research_brief(QUERY, config(), model_call=lambda *a, **k: {})


def test_brief_tolerates_extra_fields_from_the_model():
    """真实运行 2026-09-16 探针：1/5 次模型给 route 条目补了 kind="route"。

    这份规划本身完全可用（5 条路线都在），却因为一个多余字段被整份作废、给用户返回 502。
    brief 的产出是"给用户看 + 给 Lead 当背景"，不是要落进调度状态的强契约——按 §7 的口径
    服务端只做结构校验（id 形状、状态合法、条目上限）；模型多写一个字段既无害也不该致命。
    注意 kind 的**取值**仍然受限：写成 delivery 这类不认识的值照旧拒绝（见上面那组用例）。
    """
    brief = build(
        payload(
            requirements=[
                {"id": "goal", "text": QUERY, "kind": "goal", "note": "用户原话"},
            ],
            route=[
                {"id": "r1", "text": "先看清 WAL 与 journal 的切换条件", "kind": "route"},
                {"id": "r2", "text": "再查官方文档里的相关配置"},
            ],
        )
    )
    assert [item.id for item in brief.route] == ["r1", "r2"]
    assert brief.requirements[0].id == "goal"


def test_brief_tolerates_extra_top_level_fields():
    """同一类问题的另一层：attempt2 里模型多给了 confirmation_reason_note。

    两轮真实探针各中一次（route 条目补 kind、顶层补 reason_note），说明"模型会带冗余字段"
    是系统性的而不是偶发。brief 的契约是**只校验结构**（必填存在、id 形状、kind 取值、条目上限、
    原始问题身份不变），这些都与多余字段无关；每多一层 forbid 就多一次"整份作废 + 502"的机会。
    """
    data = payload()
    data["confirmation_reason_note"] = ""
    brief = build(data)
    assert brief.needs_confirmation is True
    assert not hasattr(brief, "confirmation_reason_note")


# --- 修订：未改变的要求保留稳定 id ------------------------------------------


def test_brief_revision_keeps_stable_requirement_ids():
    first = build(payload())
    revised = payload(
        requirements=[
            {"id": "goal", "text": QUERY, "kind": "goal"},
            {"id": "budget", "text": "预算不超过 1 万", "kind": "constraint"},
        ]
    )
    brief = build_research_brief(
        QUERY, config(), clarification="预算不超过 1 万", previous=first, model_call=lambda *a, **k: revised
    )
    assert [r.id for r in brief.requirements] == ["goal", "budget"]


def test_brief_revision_restores_id_of_unchanged_requirement():
    """修订时模型给没变的要求换了编号，服务端按内容对回原编号，不报错。"""
    first = build(payload())
    renumbered = payload(
        requirements=[{"id": "goal2", "text": QUERY, "kind": "goal"}],
    )
    brief = build_research_brief(
        QUERY,
        config(),
        clarification="换个说法但意思没变",
        previous=first,
        model_call=lambda *a, **k: renumbered,
    )
    assert [r.id for r in brief.requirements] == ["goal"]


# --- 与运行内核的接缝 -------------------------------------------------------


def test_brief_converts_to_run_requirements():
    brief = build(
        payload(
            requirements=[
                {"id": "goal", "text": QUERY, "kind": "goal"},
                {"id": "offline", "text": "必须能离线跑", "kind": "constraint"},
            ]
        )
    )
    assert brief.to_run_requirements() == [
        Requirement(id="goal", text=QUERY, kind="goal"),
        Requirement(id="offline", text="必须能离线跑", kind="constraint"),
    ]


def test_brief_does_not_build_planning_state():
    """路线不是节点：brief 不提供 confirm()，不往调度状态上长。"""
    assert not hasattr(build(payload()), "confirm")


def test_brief_module_does_not_import_planning_state():
    """与已退役的计划路径解耦：brief 内核不得依赖 planning / initial_planner。"""
    import dra.research_brief as module

    assert not hasattr(module, "planning")
    assert not hasattr(module, "initial_planner")
    assert "planning" not in getattr(module, "__dict__", {})


# --- 阶段 A：只判断缺口，不生成计划 -----------------------------------------


def assess(data: dict, *, query: str = QUERY, **kwargs):
    return assess_research_gaps(query, config(), model_call=lambda *a, **k: data, **kwargs)


def test_gap_assessment_returns_only_questions():
    """阶段 A 的产出里**没有计划**：生成了却不展示，等于白花一次输出还带上兜底措辞。"""
    gaps = assess(gaps_payload())
    assert [gap.id for gap in gaps] == ["scale"]
    gap = gaps[0]
    assert gap.header == "确认"
    assert [opt.label for opt in gap.options] == [
        "日活 10 万以内 (Recommended)",
        "日活百万级",
    ]
    # 结果类型里根本不存在 objective / route / needs_confirmation。
    assert not hasattr(gap, "objective")
    assert not hasattr(gaps[0], "needs_confirmation")


def test_gap_assessment_schema_carries_no_plan_fields():
    """把"判断阶段不生成计划"钉在 schema 上：契约里根本没有放计划的字段。"""
    from dra.research_brief import GapAssessment

    props = GapAssessment.model_json_schema()["properties"]
    assert set(props) == {"original_query", "gaps"}


def test_gap_assessment_can_return_no_gaps():
    """没有真正会改变路线的缺口时就是空数组——这是常态，不该勉强找问题。"""
    assert assess(gaps_payload(gaps=[])) == ()


def test_gap_assessment_caps_questions_at_three():
    """最多 3 条：多问一条的代价是又一轮打断，比研究里自己假设更贵。"""
    many = [
        {"id": f"g{i}", "question": f"问题 {i}？"} for i in range(4)
    ]
    assert [gap.id for gap in assess(gaps_payload(gaps=many))] == ["g0", "g1", "g2"]


def test_gap_assessment_keeps_server_query_when_model_miscopies_it():
    """模型抄原问题漏字时不失败，照常返回缺口。"""
    assert [gap.id for gap in assess(gaps_payload(query="被改写的问题"))] == ["scale"]


def test_gap_assessment_tolerates_extra_fields():
    """同 brief：模型多给字段不该作废整份判断（已吃过两次 502）。"""
    data = gaps_payload()
    data["notes"] = "随口一句"
    data["gaps"][0]["severity"] = "high"
    data["gaps"][0]["options"][0]["recommended"] = True
    gaps = assess(data)
    assert gaps[0].id == "scale"


def test_gap_option_has_no_recommended_field():
    """推荐项用"排第一 + 标签后缀"表达，不设字段——UI 不用为它做特殊处理。"""
    gap = assess(gaps_payload())[0]
    assert "recommended" not in gap.options[0].model_dump()


# --- 阶段 B：生成计划（澄清后 / 跳过后 / 无缺口时） --------------------------


def test_brief_carries_assumptions():
    """跳过澄清后，计划必须写明它按什么前提做——否则兜底措辞只是换了个地方出现。"""
    brief = build(
        payload(assumptions=["服务规模中等、痛点在 JSON 查询", "团队没有专职 DBA"])
    )
    assert brief.assumptions == ("服务规模中等、痛点在 JSON 查询", "团队没有专职 DBA")


def test_brief_defaults_to_no_assumptions():
    assert build(payload()).assumptions == ()


def test_brief_caps_assumptions_at_three():
    """实测跳过一次澄清会一口气写 5–6 条，其中大半是"假设以公开文档为准"这类免责声明。"""
    data = payload(assumptions=[f"假设 {i}" for i in range(4)])
    assert build(data).assumptions == ("假设 0", "假设 1", "假设 2")


def test_format_gap_answers_keeps_selection_custom_and_skip():
    """答案文本要同时保留：选了哪个、自己补了什么、哪些跳过了。"""
    gaps = assess(gaps_payload())
    text = format_gap_answers(
        gaps,
        [
            {"id": "scale", "selected": ["日活 10 万以内 (Recommended)"], "custom": ""},
        ],
    )
    assert "[确认]" in text
    assert "日活 10 万以内" in text
    assert "跳过" not in text


def test_format_gap_answers_marks_skipped_and_custom():
    gaps = tuple(
        BriefGap(id=f"g{i}", question=f"问题 {i}？") for i in range(3)
    )
    text = format_gap_answers(
        gaps,
        [
            {"id": "g0", "selected": ["甲"], "custom": "另外还有一点"},
            {"id": "g1", "selected": [], "custom": ""},
        ],
    )
    assert "选了「甲」" in text
    assert "自己补充：「另外还有一点」" in text
    assert "跳过" in text          # g1 答成空、g2 根本没答


def test_format_gap_answers_without_answers_means_skip_everything():
    gaps = assess(gaps_payload())
    text = format_gap_answers(gaps, [])
    assert "跳过" in text


def test_render_brief_context_carries_goal_route_and_assumptions():
    """给研究内核的任务背景：目标、初步路线、没确认的前提都要在，且标明可调整。"""
    brief = build(payload(assumptions=["服务规模中等"]))
    text = render_brief_context(brief)
    assert "【确认过的目标】" in text and brief.objective in text
    assert "【初步路线】" in text and "运行中可依据证据调整" in text
    assert "1. 先看清两者的能力边界" in text
    assert "【未经用户确认的前提】" in text and "服务规模中等" in text


def test_render_brief_context_without_assumptions_omits_that_block():
    """没有未确认前提就别放一个空标题——那会让人以为有东西被藏起来了。"""
    text = render_brief_context(build(payload()))
    assert "【未经用户确认的前提】" not in text


def test_gap_check_runs_without_thinking_but_plan_keeps_it():
    """判断缺口不开思考（开着会在思考里解题、单次可达两分钟）；生成计划仍按配置思考。"""
    seen = {}

    def gap_model(*args, **kwargs):
        seen["gaps"] = (kwargs.get("reasoning"), kwargs.get("effort"))
        return gaps_payload(gaps=[])

    def plan_model(*args, **kwargs):
        seen["plan"] = kwargs.get("reasoning")
        return payload()

    assess_research_gaps(QUERY, config(), model_call=gap_model)
    build_research_brief(QUERY, config(), model_call=plan_model)
    assert seen == {"gaps": (False, None), "plan": True}
