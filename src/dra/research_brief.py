"""研究开始前的两段：先判断缺不缺口，再按需生成计划。

对应 EXPERIMENT_PLAN §7 第 1 项。与 ``initial_planner`` 的分工差别：

- 只产出**扁平路线**，不产出 DAG 节点（没有 ``dependency_ids`` / ``acceptance_criteria``）、
  不构建计划状态、不参与调度。主线的"计划"是路线不是门禁，所以这里没有
  ``InitialPlan.confirm()`` 那一层。
- ``route[i]`` 就是主 Agent 之后用运行期自报路线工具全量提交的 ``items[i]`` 的**初始值**，
  两处共用一个形状，避免"确认前的计划"和"运行中的计划"长成两套。

**为什么要分两段**：一次调用同时吐"缺口 + 计划"时，模型为了把路线写完
只能先替用户把缺口假想掉，于是 objective 里出现"结合当前服务的实际负载…"这种兜底措辞——
生成了却不该给用户看，白花一次输出还误导人。所以：

1. :func:`assess_research_gaps` 只判断"信息够不够"，产出若干条问题，**不生成计划**；
2. :func:`build_research_brief` 才生成计划，输入是原问题 +（用户对问题的回答 / 跳过时的假设）。

服务端**只透传**模型的判断：要不要澄清是它判的，要不要确认计划也是它判的，服务端不加第二道
裁决。服务端负责的只有结构校验与身份契约。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from dra import llm, timing
from dra.prompt_time import current_research_time, time_hint
from dra.models import NonEmptyText, PlanId
from dra.simple_research import Requirement
from dra.artifacts import OutputFormat


class BriefRequirement(BaseModel):
    """模型给出的目标 / 硬限制。``kind`` 限死在 goal / constraint。

    ``delivery`` 不作为规划输出：交付形态由运行时决定，不该在这里变成用户的硬要求。

   """

    # extra="ignore"：模型偶尔会给条目补一个无害字段（例如给 route 补 kind="route"）。这份产出是"给用户看 + 给 Lead 当背景"，不是要落进调度状态的
    # 强契约——为一个多余字段把整份可用的规划作废、给用户返回 502，代价远大于收益。
    # 但取值本身仍受约束：kind 写成 delivery 之类不认识的值照旧拒绝（Literal 不受此影响）。
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: PlanId
    text: NonEmptyText
    kind: Literal["goal", "constraint"] = "goal"
    output_formats: tuple[OutputFormat, ...] = Field(default=(), max_length=10)


class BriefRouteItem(BaseModel):
    """初步路线的一条；形状与运行期自报路线的 ``items[i]`` 一致。"""

    model_config = ConfigDict(extra="ignore", frozen=True)  # 同上：多余字段不致命

    id: PlanId
    text: NonEmptyText


class BriefGapOption(BaseModel):
    """一个候选取值。``description`` 用一句话说清"选它会怎样"，不是复述标签。

    推荐项**不设字段**：把它放第一个、在标签后面加
    "(Recommended)" 就够了，UI 不用为它做特殊处理。
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    label: NonEmptyText
    description: str = ""


class BriefGap(BaseModel):
    """一条会改变路线的缺口。字段形状参考常见 Agent 产品的 ask_user_question。"""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: PlanId
    question: NonEmptyText
    header: str | None = None
    options: tuple[BriefGapOption, ...] = ()
    multi_select: bool = False


class GapAssessment(BaseModel):
    """阶段 A 的产出：只有问题，没有计划。"""

    model_config = ConfigDict(extra="ignore", frozen=True)

    original_query: NonEmptyText
    gaps: tuple[BriefGap, ...] = ()


class ResearchBrief(BaseModel):
    """开跑前的目标 / 硬要求 + 初步路线，以及"要不要先让用户看一眼"。"""

    # 三层（顶层 / requirements / route）一律 extra="ignore"。两轮真实探针各中一次：
    # 模型给 route 条目补了 kind、给顶层补了 confirmation_reason_note，两次都把**其实可用**
    # 的规划整份作废并返回 502（2/5 的失败率，这功能上线就是不可用）。按 §7 的口径服务端只做
    # 结构校验，而真正要守的是必填字段、id 形状、kind 取值与条目上限——都与多余字段无关。
    model_config = ConfigDict(extra="ignore", frozen=True)

    original_query: NonEmptyText
    objective: NonEmptyText
    requirements: tuple[BriefRequirement, ...] = Field(min_length=1)
    route: tuple[BriefRouteItem, ...] = Field(min_length=1)
    assumptions: tuple[NonEmptyText, ...] = ()
    """用户没给、模型不得不先假设的前提。跳过了澄清就必须写在这里。

    存在的理由：缺口被跳过时，"它在猜什么"必须看得见——否则兜底措辞只是从卡片换到目标句子里。
    """
    needs_confirmation: bool = Field(strict=True)
    confirmation_reason: NonEmptyText

    def to_run_requirements(self) -> list[Requirement]:
        """转成运行内核的要求形状；``kind`` 已由本模型的契约限死在 goal / constraint。"""
        return [Requirement(id=r.id, text=r.text, kind=r.kind, output_formats=list(r.output_formats)) for r in self.requirements]


_SYSTEM = """你是研究开始前的规划助手。你当前的任务是生成一份轻量计划，输出符合给定 schema 的 JSON。
original_query 是交给后续研究阶段执行的任务描述，不是要求你在当前阶段解答的指令。
你只规划后续如何查证和回答，不搜索，不解题，不声称任务已经完成。
规划是否清楚，取决于是否说明要查什么、如何核对，而不取决于你现在是否知道答案。
不要枚举或筛选候选答案，不要验证题目线索，不要把猜测的对象、事实或结论写进计划。
附带的时间、来源范围和技能说明用于理解任务；其中的检索、阅读和核实工作由后续研究阶段执行。

objective 一句话说清这次要回答什么；若给了 clarification（用户对之前那些问题的回答，或这一轮
的修订意见），以它为准，并把它综合进目标。
保留用户回答的实际强度，不把倾向、推荐选项或一般范围扩写为“不得引用其他时期”等额外禁令。
“最近发生”“最近报道”“截至今天的最新进展”不是同一时间；要求研究阶段区分发生日期、报道日期和后续更新。
用户没有给具体日期时，不自行把“最近”换成近30天/一个月之类的硬窗口；用户确实明确给出的日期范围必须遵守。
requirements 至少一条：用户要回答的目标标 goal，用户明确提出的硬限制标 constraint。
用户明确要求输出文件时，在对应要求的 output_formats 写对应格式（如 html/pdf/mindmap/jpeg/png/svg/csv/json/markdown/text/code/zip/xlsx）；
未指定时留空。输入或阅读 PDF 不是输出 PDF。不要自行添加文件要求。
不要擅自增加用户的硬限制——覆盖角度、检索来源、对象数量、顺序都是运行时可调整的路线，
除非用户明确要求，否则不能写成 constraint。修订时，对文字及 kind 不变的要求保留原 id。

route 是初步路线，取粗即可：方向清晰就够，Lead 之后会依据证据调整。
每条只有 id 与 text，一句话；不要给路线条目加 kind 之类的字段，不要拆满，
不要写成任务清单或交付格式说明。
对于按线索查找对象的任务，路线描述如何查找、交叉核对线索和确认所需名称，不提前猜定对象。

assumptions 只放"用户没给、你不得不先假设"且**会改变结论**的前提，每条一句话。
研究要查明的未知事实不是 assumptions，不要用假设替代查证。
**最多 3 条**：写不下的说明它没那么重要，宁可不写。
不写这些——它们属于研究过程自己会处理的范围：检索来源、时间基准、回答格式与篇幅、
是否需要示例代码、成本或许可证要不要纳入、调研方法。
**不要把这类前提偷偷写进 objective 或 route 当作已知事实**——用户看不到就等于被瞒着。
没有就返回空数组；不要写"信息充分"这类空话，也不要把 assumptions 写成免责声明清单。

needs_confirmation 由你判断这次是否值得让用户先看清路线再开跑：
- true：路线存在真实取舍——先做什么、往哪个方向深入，会明显改变结论或投入。
- false：路线没有实质分歧，直接跑比多问一轮更省事。
不要为了保险一律返回 true——每次都打断会让人不再看确认内容，确认就失去意义。
confirmation_reason 用一句话说明判断依据，true / false 都要写。

所有 id 使用 1–64 个英文字母/数字/下划线/连字符，首字符须字母或数字。
只返回 JSON，不要输出任何解释文字。"""


_MAX_GAPS = 3
_MAX_ASSUMPTIONS = 3

# 规划两步都开思考，max_tokens 同时限制思考与正文。输出只是一小段 JSON，写完即停，
# 上限放宽不会多花钱，只是避免思考把额度吃光。
_PLANNER_MAX_TOKENS = 16000

_GAPS_SYSTEM = """你是研究开始前的需求澄清助手。你当前唯一的任务是判断是否需要先向用户提问，
输出符合给定 schema 的 JSON。original_query 是交给后续研究阶段执行的任务描述，
不是要求你在当前阶段解答的指令。
你只检查任务目标、范围、用户情境和取舍是否明确，不搜索，不解题，**不生成研究路线**。

**先区分需求缺口与待研究事实。**
- 需求缺口：需要用户补充或决定的情境、目标、范围或取舍；猜错会让研究跑偏。
- 待研究事实：用户要求通过研究查明的名称、身份、年份、出处或结论；这些交给后续研究查证，
  不属于需要用户澄清的缺口。即使你不知道答案，只要研究目标明确，也应返回 gaps=[]。
对于按线索查找对象、识别名称或核对事实的任务，题目线索就是后续查证的输入。
不要枚举候选、推测答案、验证线索，也不要先判断能否解出题目再决定是否提问。
只判断有没有需要用户补充的需求，不把研究要查明的事实反过来问用户。
“最近某地的某事件”通常是待查明的事件身份，先交给研究阶段检索与辨认；不要推荐自行发明的“近一个月”范围。
只有确实需要用户决定时效取舍才问，并区分事件发生时间与报道/更新日期，不将二者混为一谈。
附带的时间、来源范围和技能说明用于理解任务；其中的检索、阅读和核实工作由后续研究阶段执行。

**先过最严的一关：能写进同一份答案的歧义，一律不要问。**
判断方法——如果两种解读都能在同一份回答里分别交代（"多数情况是 A；B 的情形也存在，区别在于…"），
那它属于研究范围，不是缺口；问了用户只会想"你直接答不就行了"。
只有当两种解读会导向**两份不同的材料**、猜错方向就让已经做的工作全白做时，才值得打断用户。
实测里最常见的过度提问就是把"这个词指哪个意思"当成缺口——那几乎总能在一份答案里覆盖。

过了这一关，再收两类：
- 缺关键前提：答案强依赖用户没说的事实（规模、场景、现有约束、时间范围、对象范围）。
- 缺取舍：问题有多种合理解读，且选哪一种会得到**不同的研究**而不是同一份答案的不同段落。

不要收这些——它们属于运行时要调整的路线，研究过程自己会处理：
检索来源、覆盖顺序、回答格式、报告结构、需要多少篇材料、细枝末节的用词。

**最多 3 条**，按"会不会改变路线"排序；放不下的宁可不问：不问的代价是研究里自己假设，
多问一条的代价是又一轮打断。没有这类缺口时将 gaps 设为空数组——**这是常态，不要勉强找问题**。
此时返回 {"original_query":"用户原问题","gaps":[]}，不要直接返回 []。

每条 gap：
- id：稳定的短 id。
- question：直接问用户的一句话，说清为什么这个前提会改变路线。
- header：可选短标题，如"确认"或"选择方向"。
- options：可选。给 2–4 个你最可能见到的取值。如果你推荐其中一个，**放在第一个，并在标签
  后面加 "(Recommended)"**；不要另加 recommended 字段。
- 每个 option 的 description 用一句话说清"选它会怎样"（取舍或影响），不要复述标签。
- multi_select：确实可以多选时才设 true。

只返回 JSON 对象，顶层包含 original_query 和 gaps；gaps 是数组。不要输出任何解释文字。"""


PROGRESS: ContextVar[Callable[[dict], None] | None] = ContextVar("planner_progress", default=None)
"""规划进度的接收方（网页流式接口设置）；没有接收方时规划照常运行、不产生任何进度消息。"""


def _progress_kwargs(stage: str) -> dict:
    """报告进入了哪一步，并让模型的思考片段实时送给接收方。"""
    sink = PROGRESS.get()
    if sink is None:
        return {}
    sink({"type": "stage", "stage": stage})
    return {"on_reasoning": lambda text: sink({"type": "reasoning", "stage": stage, "text": text})}


_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TRUE_WORDS = {"true", "yes", "1", "是", "需要"}
_FALSE_WORDS = {"false", "no", "0", "否", "不需要"}


def _clean_text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _with_ids(items: list[dict], prefix: str) -> list[dict]:
    """模型给的 id 合法且不重复就沿用，否则由服务端按顺序编号（中文、空格都不再让整份作废）。"""
    used: set[str] = set()
    out = []
    for item in items:
        item_id = _clean_text(item.get("id"))
        if not _ID_PATTERN.match(item_id) or item_id in used:
            n = len(out) + 1
            while f"{prefix}{n}" in used:
                n += 1
            item_id = f"{prefix}{n}"
        used.add(item_id)
        out.append({**item, "id": item_id})
    return out


def _tidy_gaps(data: dict) -> dict:
    """缺口判断的小错就地修正：编号重排、空问题丢弃、超出上限只留前几条。"""
    gaps = []
    for gap in data.get("gaps") or []:
        if not isinstance(gap, dict) or not _clean_text(gap.get("question")):
            continue
        options = [o for o in gap.get("options") or [] if isinstance(o, dict) and _clean_text(o.get("label"))]
        gaps.append({**gap, "options": options, "multi_select": gap.get("multi_select") is True})
    return {**data, "gaps": _with_ids(gaps, "g")[:_MAX_GAPS]}


def _tidy_brief(data: dict, query: str, max_route: int, previous: "ResearchBrief | None") -> dict:
    """计划的小错就地修正，只有修不了的条目才丢；不再为一处小错让整份计划作废。

    - 原问题以服务端为准；目标缺失时用原问题；
    - 要求类型认不出的按 goal；文字与类型没变的要求沿用上一版 id；
    - 编号不合法或重复的由服务端重编；
    - 路线与前提超出上限只截取前面的；
    - 「要不要确认」写成字符串时按字面意思转换，看不懂时保守地要求确认。
    """
    allowed_formats = set(get_args(OutputFormat))
    known = {(r.kind, r.text): r.id for r in previous.requirements} if previous is not None else {}
    requirements = []
    for req in data.get("requirements") or []:
        if not isinstance(req, dict) or not _clean_text(req.get("text")):
            continue
        kind = _clean_text(req.get("kind")).lower()
        kind = kind if kind in ("goal", "constraint") else "goal"
        text = _clean_text(req.get("text"))
        formats = [f for f in req.get("output_formats") or [] if f in allowed_formats]
        requirements.append({**req, "text": text, "kind": kind, "output_formats": formats,
                             "id": known.get((kind, text), req.get("id"))})
    if not requirements:
        requirements = [{"id": "goal", "text": query, "kind": "goal"}]
    route = [item for item in data.get("route") or [] if isinstance(item, dict) and _clean_text(item.get("text"))]
    assumptions = [a.strip() for a in data.get("assumptions") or [] if isinstance(a, str) and a.strip()]
    confirm = data.get("needs_confirmation")
    if isinstance(confirm, str):
        word = confirm.strip().lower()
        confirm = True if word in _TRUE_WORDS else False if word in _FALSE_WORDS else None
    if not isinstance(confirm, bool):
        confirm = True
    return {
        **data,
        "original_query": query,
        "objective": _clean_text(data.get("objective")) or query,
        "requirements": _with_ids(requirements, "req"),
        "route": _with_ids(route, "r")[:max_route],
        "assumptions": assumptions[:_MAX_ASSUMPTIONS],
        "needs_confirmation": confirm,
        "confirmation_reason": _clean_text(data.get("confirmation_reason")) or "模型未说明理由",
    }


def assess_research_gaps(
    query: str,
    config,
    *,
    model_call=None,
    source_context: str = "",
) -> tuple[BriefGap, ...]:
    """阶段 A：只判断缺不缺口。**不生成计划**——生成了却没机会给你看，就是白花一次输出。

    没有真正会改变路线的缺口时返回空元组（这是常态），调用方据此直接进阶段 B。
    """
    if not query.strip():
        raise ValueError("研究问题不能为空")
    payload = {
        "original_query": query,
        "max_gaps": _MAX_GAPS,
        "schema": GapAssessment.model_json_schema(),
    }
    token = timing.set_ctx(usage_role="research_brief", step="research_brief_gaps")
    try:
        data = (model_call or llm.call_json)(
            [
                {"role": "system", "content": _GAPS_SYSTEM + time_hint(current_research_time()) + source_context},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            expect_keys=("gaps",),
            **_progress_kwargs("gaps"),
            model=config.planner_model,
            provider=config.planner_provider,
            # 这一步不开思考：开着时模型会在思考里去解题（谜题式问题实测单次可达 14000 token、140 秒），
            # 提示词也约束不住；关掉后 8 类问题最长 6 秒，判断质量相当，只是复杂问题上偶尔多问一两条。
            # 设置里「规划」的思考强度只作用于生成计划那一步。
            reasoning=False,
            effort=None,
            # 上限含思考：谜题式问题会让模型在思考里试着解题，4000 曾整段用在思考上、正文为空。
            max_tokens=_PLANNER_MAX_TOKENS,
            json_retries=1,
            response_format={"type": "json_object"},
        )
    finally:
        timing.reset_ctx(token)
    if not data:
        raise ValueError("缺口判断没有返回可用结果")
    # 原问题以服务端为准：模型抄写时常漏字（如“中文名”抄成“中文”），不能因此让整步失败。
    assessment = GapAssessment.model_validate({**_tidy_gaps(data), "original_query": query})
    return assessment.gaps


def format_gap_answers(gaps, answers) -> str:
    """把用户对缺口问题的回答拼成交给规划器的一段话。

    **选了什么、自己补了什么、哪些跳过**都要留下：跳过不是"没这回事"，模型得据此把前提写进
    ``assumptions``，而不是假装知道。
    """
    by_id: dict[str, dict[str, Any]] = {}
    for answer in answers or []:
        if isinstance(answer, dict) and isinstance(answer.get("id"), str):
            by_id[answer["id"]] = answer
    lines = ["用户对之前问题的回答："]
    for gap in gaps:
        answer = by_id.get(gap.id) or {}
        selected = [
            str(item).strip() for item in (answer.get("selected") or []) if str(item).strip()
        ]
        custom = str(answer.get("custom") or "").strip()
        label = gap.header or gap.question
        if selected and custom:
            lines.append(f"- [{label}] 选了「{'、'.join(selected)}」；自己补充：「{custom}」")
        elif selected:
            lines.append(f"- [{label}] 选了「{'、'.join(selected)}」")
        elif custom:
            lines.append(f"- [{label}] 自己补充：「{custom}」")
        else:
            lines.append(f"- [{label}] 跳过（用户没回答，按合理假设处理并写进 assumptions）")
    return "\n".join(lines)


def render_brief_context(brief: ResearchBrief) -> str:
    """把确认过的目标 / 初步路线 / 未确认前提渲染成给研究内核看的一段文本。

    只进**任务上下文**，不进 ``requirements``：要求清单会被核查逐条判定，把路线或假设塞进去
    会变成"必须逐条交差"的门槛。这些是给模型的背景。
    """
    # needs_confirmation=false 时前端直接开跑，用户没看过这份目标，不能写成“确认过的”。
    label = "【用户确认的研究目标】" if brief.needs_confirmation else "【规划阶段理解的研究目标（未经用户确认）】"
    lines = [f"{label}{brief.objective}",
             "研究目标是对原问题的澄清；两者表述不同时按目标执行，但不得丢掉原问题里的对象、范围和限定。"]
    lines.append("【初步路线】（运行中可依据证据调整，不必逐条交差）")
    lines += [f"{index}. {item.text}" for index, item in enumerate(brief.route, 1)]
    if brief.assumptions:
        lines.append(
            "【未经用户确认的前提】以下是开跑前的假设；证据与此冲突时以证据为准，"
            "并在答案里如实说明："
        )
        lines += [f"- {text}" for text in brief.assumptions]
    return "\n".join(lines)


def build_research_brief(
    query: str,
    config,
    *,
    clarification: str | None = None,
    previous: ResearchBrief | None = None,
    model_call=None,
    source_context: str = "",
) -> ResearchBrief:
    """一次模型调用产出 brief；失败如实抛错，绝不降级成"不需要确认直接开跑"。

    ``model_call`` 只用于离线测试替换外部边界。
    """
    if not query.strip():
        raise ValueError("研究问题不能为空")
    if previous is not None and previous.original_query != query:
        raise ValueError("初步 brief 的原始问题身份不匹配")
    payload = {
        "original_query": query,
        "clarification": clarification,
        "previous": previous.model_dump(mode="json") if previous is not None else None,
        "max_route_items": config.max_initial_tasks,
        "max_assumptions": _MAX_ASSUMPTIONS,
        "schema": ResearchBrief.model_json_schema(),
    }
    token = timing.set_ctx(usage_role="research_brief", step="research_brief")
    try:
        data = (model_call or llm.call_json)(
            [
                {"role": "system", "content": _SYSTEM + time_hint(current_research_time()) + source_context},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            expect_keys=("objective", "route"),
            **_progress_kwargs("plan"),
            model=config.planner_model,
            provider=config.planner_provider,
            reasoning=True,
            effort=config.planner_effort,
            max_tokens=_PLANNER_MAX_TOKENS,
            json_retries=1,
            response_format={"type": "json_object"},
        )
    finally:
        timing.reset_ctx(token)
    if not data:
        # call_json 穷尽重试后返回 {}。这里必须响亮地失败：把"规划没跑出来"当成
        # "不需要确认"会静默改变用户看到的流程。
        raise ValueError("规划没有返回可用结果，未开始研究")
    return ResearchBrief.model_validate(_tidy_brief(data, query, config.max_initial_tasks, previous))
