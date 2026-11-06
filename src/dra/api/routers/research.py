"""研究接口：发起研究、追问、实时进度流、运行中发消息、取消。"""
from __future__ import annotations

import asyncio
import json
import unicodedata
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import Field, ValidationError

from dra import llm, mcp_client
from dra.api.common import (
    _PUBLIC_ID_PATTERN,
    _ROOT,
    _compact_event,
    _require_valid_run_id,
    _StrictPayload,
)
from dra.api.deps import RegistryDep, StoreDep
from dra.api.routers.skills import _skill_bundle
from dra.api.services.planning import _strip_plan_trace
from dra.api.services.runs import ActiveRunLimitError, _run_input_snapshot
from dra.events import EventType
from dra.library import LibraryError, get_library
from dra.research_brief import ResearchBrief
from dra.site_filter import MAX_BLOCKED_SITES, normalize_blocked_sites

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


def _normalise_plan_identity(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


@router.post("/api/research")
async def api_research(req: ResearchReq, *, registry: RegistryDep) -> dict:
    from dra import runtime_config

    if not req.query.strip():
        raise HTTPException(400, "query 不能为空")
    brief = None
    if req.brief is not None:
        try:
            brief = ResearchBrief.model_validate(_strip_plan_trace(req.brief))
        except (ValidationError, TypeError, ValueError) as exc:
            raise HTTPException(400, f"brief 非法：{exc}") from exc
        if _normalise_plan_identity(req.query) != _normalise_plan_identity(brief.original_query):
            raise HTTPException(400, "query 与已确认 brief 的 original_query 不一致")
    # 主、子 Agent 共用研究模型配置。
    simple_settings = runtime_config.current()
    skill_bundle = _skill_bundle(req.skill_selection)
    try:
        selection = get_library().pin(req.document_ids) if req.source_mode != "web" else {}
        external = mcp_client.pin_servers(req.mcp_server_ids)
        if external and req.source_mode == "library":
            raise mcp_client.MCPError("仅资料模式不允许外部 MCP，请切换网页或混合研究")
        blocked_sites = normalize_blocked_sites(req.blocked_sites)
    except (LibraryError, mcp_client.MCPError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        run_id = registry.start_simple_research(
            req.query,
            config=runtime_config.to_simple_config(simple_settings, source_mode=req.source_mode,
                                library_selection=selection, library_rerank=req.library_rerank,
                                mcp_servers=external, skill_bundle=skill_bundle,
                                blocked_sites=blocked_sites),
            requirements=brief.to_run_requirements() if brief is not None else None,
            brief=brief,
            full_llm_io=req.trace_full_llm_io,
        )
    except ActiveRunLimitError as exc:
        raise HTTPException(429, str(exc)) from exc
    return {"run_id": run_id, "mode": "simple"}


class FollowupReq(_StrictPayload):
    request_id: str = Field(min_length=1, max_length=64, pattern=_PUBLIC_ID_PATTERN)
    text: str = Field(min_length=1, max_length=6000)


@router.post("/api/research/{run_id}/followup", status_code=202)
async def api_followup(run_id: str, req: FollowupReq, *, store: StoreDep, registry: RegistryDep):
    from dra import runtime_config
    from dra.research_history import HistoryError
    _require_valid_run_id(run_id)
    if store.get_meta(run_id) is None:
        raise HTTPException(404,"run 不存在")
    try:
        config=runtime_config.to_simple_config(runtime_config.current())
        new_id=registry.start_followup(run_id,req.request_id,req.text,config=config)
    except ActiveRunLimitError as exc:
        raise HTTPException(429,str(exc)) from exc
    except (HistoryError, LibraryError, mcp_client.MCPError) as exc:
        raise HTTPException(409,str(exc)) from exc
    meta=store.get_meta(new_id)
    return {k:meta[k] for k in ("run_id","conversation_id","turn_index","status","parent_run_id")}


_FIXTURE = _ROOT / "fixtures" / "demo_run" / "events.jsonl"


def _sse_frame(evt: dict) -> str:
    return f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"


@router.get("/api/research/{run_id}/stream")
async def api_stream(run_id: str, compact: bool = False, *, registry: RegistryDep) -> StreamingResponse:
    """SSE：原子「订阅+快照」后先补发 backlog 再续推 live，按 seq 去重——
    刷新页面 = 重连 = 不丢现场。end 事件后关流（EventSource 侧 close 防自动重连）。"""
    _require_valid_run_id(run_id)
    h = registry.runs.get(run_id)
    if h is None:
        raise HTTPException(404, "run 不在内存（服务重启过？历史回放走 /api/runs/{id}/events）")

    async def gen():
        q: asyncio.Queue = asyncio.Queue()
        with h.lock:
            backlog = list(h.buffer)
            h.subscribers.add(q)
        try:
            last = -1
            for e in backlog:
                if e["type"] == EventType.END.value and (not h.done or e is not backlog[-1]):
                    continue
                yield _sse_frame(_compact_event(e) if compact else e)
                last = e["seq"]
            if h.done and backlog and backlog[-1]["type"] == EventType.END.value:
                return
            while True:
                e = await q.get()
                if e["seq"] <= last:
                    continue
                yield _sse_frame(_compact_event(e) if compact else e)
                if e["type"] == EventType.END.value:
                    return
        finally:
            with h.lock:
                h.subscribers.discard(q)

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
    meta = store.get_meta(run_id)
    if meta is None:
        raise HTTPException(404, "run 不存在")
    h = registry.runs.get(run_id)
    if h is not None and h.user_input is not None:
        try:
            return h.user_input.accept(req.message_id, req.text)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except llm.LLMPersistenceError as exc:
            raise HTTPException(503, "消息保存失败，未确认接收；请查看运行状态") from exc
    # 结束/重启后的网络重试可确认原消息，不能以重试为由重启研究。
    snapshot = _run_input_snapshot(run_id, store, registry)
    for message in snapshot["messages"]:
        if message["message_id"] == req.message_id:
            if message["text"] != req.text:
                raise HTTPException(409, "重复消息 ID 对应的正文不同")
            return {"accepted": True, "message": message, "state": "closed"}
    if snapshot.get("error"):
        raise HTTPException(503, "消息记录不完整，暂时无法确认是否已接收")
    raise HTTPException(409, "研究正在收尾或已结束，未接收新消息")


@router.post("/api/research/{run_id}/cancel")
def api_cancel(run_id: str, *, store: StoreDep, registry: RegistryDep) -> dict:
    """请求协作式取消，真实研究线程结束后释放名额。"""
    _require_valid_run_id(run_id)
    h = registry.runs.get(run_id)
    if h is None:
        raise HTTPException(404, "run 不在内存")
    if h.done or not h.busy:
        meta = store.get_meta(run_id) or {}
        return {"status": meta.get("status", "unknown")}
    if h.cancel_event is not None:
        h.cancel_event.set()
    if h.user_input is not None:
        h.user_input.close("cancelled")
    return {"status": "cancelling"}
