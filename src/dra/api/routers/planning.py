"""研究计划接口：/api/brief、/api/brief/plan、/api/brief/revise，及规划进度流式推送。"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from dra import llm
from dra.api.common import service_errors
from dra.api.deps import CurrentUserDep, PlanStoreDep, ReadyDatabaseDep
from dra.api.services import trial as trial_service
from dra.api.services import quotas
from dra.api.services.planning import (
    PlannerJob,
    _brief_payload,
    _plan_after_clarification_payload,
    _revise_brief_payload,
    source_context,
)

router = APIRouter()


def _brief_http_error(exc: Exception) -> HTTPException:
    """规划失败转为结构化上游错误，保留 trace；连接 503、超时 504、其余 502。

    刻意不用 400：这不是"用户输入不合法"，而是"规划这次没跑出来"——两者在 UI 上
    该说不同的话，也都**不改变"必须先有 brief 才能开跑"**这条契约。
    """
    if isinstance(exc, llm.LLMPersistenceError):
        raise exc
    status = 502
    message = str(exc)
    if isinstance(exc, (RuntimeError, TimeoutError)):
        # 不把上游原始异常（可能含 URL/服务端正文）直接展示到页面。
        message = "规划模型未返回可用结果，请检查模型设置或稍后重试。"
        if isinstance(exc, llm.LLMRequestTimeout):
            status, message = 504, "规划模型请求超时，请稍后重试；你的问题仍保留在输入框中。"
        elif llm._is_conn_error(exc):
            status, message = 503, "暂时无法连接规划模型，请检查网络或代理后重试；你的问题仍保留在输入框中。"
    detail: dict = {"message": message}
    trace = getattr(exc, "plan_trace", None)
    if trace:
        detail["trace"] = trace
    return HTTPException(status, detail)


def _stream_error(exc: Exception) -> dict:
    """后台规划失败时，流里最后一条 ``error`` 事件（流已开始，不能再改 HTTP 状态码）。"""
    if isinstance(exc, HTTPException):
        return {"type": "error", "status": exc.status_code, "detail": exc.detail}
    return {"type": "error", "status": 500, "detail": {"message": "规划失败，请稍后重试。"}}


def _planner_stream(work, request: Request) -> StreamingResponse:
    """规划边算边推（NDJSON）：每行一条 ``PlannerJob`` 事件。

    浏览器断开（离开页面、刷新）时让后台规划停下，不在后台继续花钱。
    """
    job = PlannerJob(work, _stream_error)

    async def next_event():
        while True:
            event = job.poll()
            if event is not None:
                return event
            if await request.is_disconnected():
                return PlannerJob.END
            await asyncio.sleep(0.05)

    async def lines():
        try:
            event = await next_event()
            while event is not PlannerJob.END:
                event, nxt = job.merge_reasoning(event)
                yield json.dumps(event, ensure_ascii=False) + "\n"
                event = nxt if nxt is not None else await next_event()
        finally:
            job.cancel()  # 正常结束时无副作用；浏览器断开时让后台规划停下

    return StreamingResponse(lines(), media_type="application/x-ndjson")


class SourceSelectionReq(BaseModel):
    request_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    skill_selection: str | None = None
    source_mode: Literal["web", "library", "mixed"] = "web"
    document_ids: list[str] = Field(default_factory=list, max_length=50)
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=4)


def _brief_sources(req: SourceSelectionReq, user_id: int) -> dict:
    with service_errors():
        return source_context(skill_selection=req.skill_selection, source_mode=req.source_mode,
                              document_ids=req.document_ids, mcp_server_ids=req.mcp_server_ids,
                              user_id=user_id)


def _planning_access(req: SourceSelectionReq, user, db) -> tuple[dict, dict]:
    """（资料范围，规划模型覆盖）。访客（#49）：只能网页来源；在调用模型之前扣一次规划次数，
    用完 429；规划用试用模型。其他人不受影响。"""
    with service_errors():
        trial = trial_service.for_user(user)
        if trial is not None:
            trial_service.check_options(source_mode=req.source_mode, document_ids=req.document_ids,
                                        mcp_server_ids=req.mcp_server_ids)
    sources = _brief_sources(req, user.id)
    def validate(access):
        if access.trial is not None:
            trial_service.check_options(source_mode=req.source_mode, document_ids=req.document_ids,
                                        mcp_server_ids=req.mcp_server_ids)
    with service_errors():
        access = quotas.take_brief(db, user.id, req.request_id or uuid.uuid4().hex, validate=validate)
    return sources, trial_service.planner_override(access.trial)


class BriefReq(SourceSelectionReq):
    query: str
    trace_full_llm_io: bool = False


class ReviseBriefReq(SourceSelectionReq):
    brief: dict
    feedback: str
    trace_full_llm_io: bool = False


class BriefPlanReq(SourceSelectionReq):
    """带着用户对缺口问题的回答生成计划；``answers`` 为空表示全部跳过。"""

    query: str
    gaps: list = Field(default_factory=list)
    answers: list = Field(default_factory=list)
    trace_full_llm_io: bool = False


@router.post("/api/brief")
def api_brief(req: BriefReq, request: Request, stream: bool = False, *, plan_store: PlanStoreDep,
              user: CurrentUserDep, db: ReadyDatabaseDep):
    """开跑前的第一段：判断问题有没有会改变路线的缺口。

    有缺口 → 只回问题（``kind="clarify"``），**不生成计划**；没有缺口 → 直接接第二段生成
    计划（``kind="plan"``）。两种情况都不开跑，用户确认后再带回 ``POST /api/research``。
    """
    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    sources, planner = _planning_access(req, user, db)

    def work() -> dict:
        try:
            payload, trace = _brief_payload(req.query, plan_store=plan_store, user_id=user.id,
                                            full_llm_io=req.trace_full_llm_io, planner=planner, **sources)
        except (ValidationError, TypeError, ValueError, RuntimeError, TimeoutError) as exc:
            raise _brief_http_error(exc) from exc
        return {**payload, "trace": trace}

    return _planner_stream(work, request) if stream else work()


@router.post("/api/brief/plan")
def api_brief_plan(req: BriefPlanReq, request: Request, stream: bool = False, *, plan_store: PlanStoreDep,
                   user: CurrentUserDep, db: ReadyDatabaseDep):
    """开跑前的第二段：带着用户对缺口问题的回答生成计划（没答 = 跳过，按假设处理）。"""
    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    sources, planner = _planning_access(req, user, db)

    def work() -> dict:
        try:
            payload, trace = _plan_after_clarification_payload(
                req.query, req.gaps, req.answers, plan_store=plan_store, user_id=user.id,
                full_llm_io=req.trace_full_llm_io, planner=planner, **sources
            )
        except (ValidationError, TypeError, ValueError, RuntimeError, TimeoutError) as exc:
            raise _brief_http_error(exc) from exc
        return {**payload, "trace": trace}

    return _planner_stream(work, request) if stream else work()


@router.post("/api/brief/revise")
def api_brief_revise(req: ReviseBriefReq, request: Request, stream: bool = False, *, plan_store: PlanStoreDep,
                     user: CurrentUserDep, db: ReadyDatabaseDep):
    """按用户在计划卡上写的意见重新生成计划。原始问题身份必须保持不变。"""
    if not req.feedback.strip():
        raise HTTPException(400, "feedback 不能为空")
    sources, planner = _planning_access(req, user, db)

    def work() -> dict:
        try:
            payload, trace = _revise_brief_payload(req.brief, req.feedback, plan_store=plan_store, user_id=user.id,
                                                   full_llm_io=req.trace_full_llm_io, planner=planner, **sources)
        except (ValidationError, TypeError, ValueError, RuntimeError, TimeoutError) as exc:
            raise _brief_http_error(exc) from exc
        if payload is None:
            raise HTTPException(400, "brief 非法：需要完整的目标、硬要求与初步路线")
        return {**payload, "trace": trace}

    return _planner_stream(work, request) if stream else work()
