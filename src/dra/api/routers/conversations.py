"""对话接口：按对话串查看多轮研究。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from dra.api.common import _require_valid_run_id
from dra.api.deps import StoreDep

router = APIRouter()


@router.get("/api/conversations")
def api_conversations(*, store: StoreDep):
    return store.conversations()


@router.get("/api/conversations/{conversation_id}")
def api_conversation(conversation_id: str, cursor: int = 0, *, store: StoreDep):
    _require_valid_run_id(conversation_id)
    if cursor < 0:
        raise HTTPException(400,"cursor 须为非负整数")
    turns=store.conversation_turns(conversation_id)
    if not turns:
        raise HTTPException(404,"研究会话不存在")
    return {"conversation_id":conversation_id,"latest_run_id":turns[-1]["run_id"],"total":len(turns),
            "turns":turns[cursor:cursor+20],"next_cursor":cursor+20 if cursor+20<len(turns) else None}
