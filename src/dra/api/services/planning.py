"""研究计划生成：缺口判断、生成与修订计划，并把规划过程记入计划记录（从 web.py 原样迁入）。"""
from __future__ import annotations

from pydantic import ValidationError

from dra import events, llm
from dra.research_brief import (
    BriefGap,
    ResearchBrief,
    assess_research_gaps,
    build_research_brief,
    format_gap_answers,
)
from dra.runstore import PlanAttemptStore


def _strip_plan_trace(payload):
    """确认计划回传时丢掉调试 trace，避免 typed DTO extra=forbid 拒收。"""
    if not isinstance(payload, dict) or "trace" not in payload:
        return payload
    return {k: v for k, v in payload.items() if k != "trace"}


def _planner_config_summary(cfg, *, full_llm_io: bool = False) -> dict:
    return {
        "planner": f"{cfg.planner_model}@{cfg.planner_provider}",
        "effort": cfg.planner_effort,
        "full_llm_io": full_llm_io,
    }


def _capture_planner_step(
    *,
    kind: str,
    query: str,
    cfg,
    step_name: str,
    fn,
    plan_store: PlanAttemptStore,
    full_llm_io: bool = False,
) -> tuple[object, dict]:
    """跑一次规划/修订：timing.step 触发 llm_call 插桩 → 订阅落盘 → 返回 (结果, trace)。

    失败时仍返回已捕获的 events（挂在异常属性 ``plan_trace`` 上），供 422 响应带回 UI。
    """
    from dra import timing

    plan_id = plan_store.create(
        query=query,
        kind=kind,
        config_summary=_planner_config_summary(cfg, full_llm_io=full_llm_io),
    )
    captured: list[dict] = []

    def sink(evt: dict) -> None:
        e = dict(evt)
        e.setdefault("seq", len(captured))
        captured.append(e)
        try:
            plan_store.append_event(plan_id, e)
        except Exception:  # noqa: BLE001 — 落盘旁路
            pass

    events.set_run_id(plan_id)
    handle = events.subscribe(sink, run_id=plan_id)
    # 规划是同步短调用：只临时关掉 timing 实时打印，避免污染 web stdout；
    # 不调用 timing.reset()——那会清掉可能并行研究 run 的计时记录。
    prev_verbose = timing._verbose
    timing._verbose = False
    trace_token = llm.set_trace_full_io(full_llm_io)
    try:
        with timing.step(step_name):
            result = fn()
        payload = result if isinstance(result, dict) else None
        plan_store.finalize(plan_id, "ok", plan=payload)
        return result, {"plan_id": plan_id, "events": list(captured)}
    except Exception as exc:
        plan_store.finalize(plan_id, "failed", error=str(exc))
        setattr(exc, "plan_trace", {"plan_id": plan_id, "events": list(captured)})
        raise
    finally:
        llm.reset_trace_full_io(trace_token)
        timing._verbose = prev_verbose
        events.unsubscribe(handle)
        events.set_run_id(None)
        timing.clear_ctx()


def _brief_payload(q_text: str, *, plan_store: PlanAttemptStore, full_llm_io: bool = False,
                   source_context: str = "") -> tuple[dict, dict]:
    """POST /api/brief 主体：先判断缺口；**没有缺口才继续生成计划**。

    有缺口时只回问题、不生成计划——生成了却不给用户看，等于白花一次输出，而且那份计划必然
    带兜底措辞（模型为了把路线写完只能先替用户把缺口假想掉）。返回 ``(payload, trace)``：

    - ``{"kind": "clarify", "gaps": [...]}``：需要先问用户；
    - ``{"kind": "plan", "brief": {...}}``：没有缺口，计划已经生成。
    """
    from dra import runtime_config

    cfg = runtime_config.to_brief_config(runtime_config.current())

    def _run() -> dict:
        gaps = assess_research_gaps(q_text, cfg, **({"source_context": source_context} if source_context else {}))
        if gaps:
            return {"kind": "clarify", "gaps": [gap.model_dump(mode="json") for gap in gaps]}
        brief = build_research_brief(q_text, cfg, **({"source_context": source_context} if source_context else {}))
        return {"kind": "plan", "brief": brief.model_dump(mode="json")}

    return _capture_planner_step(
        kind="brief",
        query=q_text,
        cfg=cfg,
        step_name="research_brief",
        fn=_run,
        plan_store=plan_store,
        full_llm_io=full_llm_io,
    )


def _plan_after_clarification_payload(
    q_text: str,
    gaps: list,
    answers: list,
    *,
    plan_store: PlanAttemptStore,
    full_llm_io: bool = False,
    source_context: str = "",
) -> tuple[dict, dict]:
    """POST /api/brief/plan 主体：带着用户对缺口的回答生成计划。

    `answers` 为空表示用户跳过了全部问题——这时答案文本里会逐条标出"跳过"，模型必须把
    由此产生的前提写进 ``assumptions``，而不是假装知道。
    """
    from dra import runtime_config

    cfg = runtime_config.to_brief_config(runtime_config.current())
    typed = tuple(BriefGap.model_validate(gap) for gap in gaps)
    clarification = format_gap_answers(typed, answers)
    return _capture_planner_step(
        kind="brief_plan",
        query=q_text,
        cfg=cfg,
        step_name="research_brief",
        fn=lambda: {
            "kind": "plan",
            "brief": build_research_brief(q_text, cfg, clarification=clarification, **({"source_context": source_context} if source_context else {})).model_dump(
                mode="json"
            ),
        },
        plan_store=plan_store,
        full_llm_io=full_llm_io,
    )


def _revise_brief_payload(
    brief_payload: dict,
    feedback: str,
    *,
    plan_store: PlanAttemptStore,
    full_llm_io: bool = False,
    source_context: str = "",
) -> tuple[dict | None, dict | None]:
    """POST /api/brief/revise 主体：还原 brief → 带 clarification 重规划。

    传入的 brief 形状非法返回 (None, None)（由路由层报 400）；规划契约失败抛带
    ``plan_trace`` 的异常（由路由层报 502）。
    """
    from dra import runtime_config

    try:
        previous = ResearchBrief.model_validate(_strip_plan_trace(brief_payload))
    except (ValidationError, TypeError, ValueError):
        return None, None
    cfg = runtime_config.to_brief_config(runtime_config.current())
    return _capture_planner_step(
        kind="brief_revise",
        query=previous.original_query,
        cfg=cfg,
        step_name="research_brief",
        fn=lambda: {
            "kind": "plan",
            "brief": build_research_brief(
                previous.original_query, cfg, clarification=feedback, previous=previous, **({"source_context": source_context} if source_context else {})
            ).model_dump(mode="json"),
        },
        plan_store=plan_store,
        full_llm_io=full_llm_io,
    )
