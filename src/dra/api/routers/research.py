"""研究接口：发起研究、追问、实时进度流、运行中发消息、取消。

接口只做参数校验、调用 ``dra.api.services.runs``、组装响应；业务规则在业务层。
"""
from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import Field

from dra.api.common import (
    _ROOT,
    _compact_event,
    _require_valid_run_id,
    _StrictPayload,
    service_errors,
)
from dra.api.deps import CurrentUserDep, ReadyDatabaseDep, RegistryDep, StoreDep
from dra.api.services import runs as run_service
from dra.api.services.ids import PUBLIC_ID_PATTERN
from dra.site_filter import MAX_BLOCKED_SITES

router = APIRouter()


class ResearchReq(_StrictPayload):
    """现役研究请求；旧 plan 参数不再接受。"""
    query: str
    skill_selection: str | None = None
    mode: Literal["simple"]
    brief: dict | None = None
    trace_full_llm_io: bool = False
    source_mode: Literal["web", "library", "mixed"] = "web"
    document_ids: list[str] = Field(default_factory=list, max_length=50)
    library_rerank: bool = False
    mcp_server_ids: list[str] = Field(default_factory=list, max_length=4)
    blocked_sites: list[str] = Field(default_factory=list, max_length=MAX_BLOCKED_SITES)


@router.post("/api/research")
async def api_research(req: ResearchReq, user: CurrentUserDep, *, registry: RegistryDep,
                       db: ReadyDatabaseDep) -> dict:
    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    with service_errors():
        run_id = await run_service.start_owned_research(
            db, registry, user.id, req.query, brief=req.brief, skill_selection=req.skill_selection,
            source_mode=req.source_mode, document_ids=req.document_ids,
            library_rerank=req.library_rerank, mcp_server_ids=req.mcp_server_ids,
            blocked_sites=req.blocked_sites, full_llm_io=req.trace_full_llm_io,
        )
    return {"run_id": run_id, "mode": "simple"}


class FollowupReq(_StrictPayload):
    request_id: str = Field(min_length=1, max_length=64, pattern=PUBLIC_ID_PATTERN)
    text: str = Field(min_length=1, max_length=6000)


@router.post("/api/research/{run_id}/followup", status_code=202)
async def api_followup(run_id: str, req: FollowupReq, user: CurrentUserDep, *, store: StoreDep,
                       registry: RegistryDep, db: ReadyDatabaseDep):
    _require_valid_run_id(run_id)
    with service_errors():
        return await run_service.start_owned_followup(
            db, store, registry, user.id, run_id, req.request_id, req.text)


_FIXTURE = _ROOT / "fixtures" / "demo_run" / "events.jsonl"


def _sse_frame(evt: dict) -> str:
    return f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"


@router.get("/api/research/{run_id}/stream")
async def api_stream(run_id: str, compact: bool = False, *, registry: RegistryDep) -> StreamingResponse:
    """SSE：原子「订阅+快照」后先补发 backlog 再续推 live，按 seq 去重——
    刷新页面 = 重连 = 不丢现场。end 事件后关流（EventSource 侧 close 防自动重连）。"""
    _require_valid_run_id(run_id)
    with service_errors():
        events = run_service.follow_events(registry, run_id)

    async def gen():
        try:
            async for e in events:
                yield _sse_frame(_compact_event(e) if compact else e)
        finally:
            await events.aclose()  # 浏览器断开时立即退订，不等垃圾回收

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@router.get("/api/demo/events", response_class=PlainTextResponse)
def api_demo_events() -> str:
    if not _FIXTURE.exists():
        raise HTTPException(404, "demo fixture 缺失：uv run python scripts/gen_dev_fixture.py")
    return _FIXTURE.read_text(encoding="utf-8")


class ResearchMessageReq(_StrictPayload):
    message_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=6000)


@router.post("/api/research/{run_id}/messages", status_code=202)
def api_research_message(run_id: str, req: ResearchMessageReq, *, store: StoreDep,
                         registry: RegistryDep) -> dict:
    _require_valid_run_id(run_id)
    if not req.text.strip():
        raise HTTPException(422, "消息不能为空")
    with service_errors():
        return run_service.post_message(store, registry, run_id, req.message_id, req.text)


@router.post("/api/research/{run_id}/cancel")
def api_cancel(run_id: str, *, store: StoreDep, registry: RegistryDep) -> dict:
    """请求协作式取消，真实研究线程结束后释放名额。"""
    _require_valid_run_id(run_id)
    with service_errors():
        return {"status": run_service.cancel(store, registry, run_id)}
