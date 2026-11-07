"""对话接口：按对话串查看多轮研究。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from dra.api.common import _require_valid_run_id
from dra.api.deps import OWNED_CONVERSATION, StoreDep, VisibleConversationsDep

router = APIRouter()
# 带对话编号的接口：分组上统一检查归属（#31）。
conversation_router = APIRouter(dependencies=OWNED_CONVERSATION)


@router.get("/api/conversations")
def api_conversations(visible: VisibleConversationsDep, *, store: StoreDep):
    return [c for c in store.conversations() if visible(c["conversation_id"])]


@conversation_router.get("/api/conversations/{conversation_id}")
def api_conversation(conversation_id: str, cursor: int = 0, *, store: StoreDep):
    _require_valid_run_id(conversation_id)
    if cursor < 0:
        raise HTTPException(400,"cursor 须为非负整数")
    turns=store.conversation_turns(conversation_id)
    if not turns:
        raise HTTPException(404,"研究会话不存在")
    return {"conversation_id":conversation_id,"latest_run_id":turns[-1]["run_id"],"total":len(turns),
            "turns":turns[cursor:cursor+20],"next_cursor":cursor+20 if cursor+20<len(turns) else None}
