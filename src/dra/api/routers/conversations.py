"""对话接口：按对话串查看多轮研究。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from dra.api.common import _require_valid_run_id
from dra.api.deps import READABLE_CONVERSATION, ConversationRunsDep, StoreDep, VisibleRunsDep

router = APIRouter()
# 带对话编号的查看接口：属于自己或属于示例（#31、#47），示例不要求登录。
conversation_router = APIRouter(dependencies=READABLE_CONVERSATION)


@router.get("/api/conversations")
def api_conversations(visible: VisibleRunsDep, *, store: StoreDep):
    # 只用数据库里登记给当前用户的轮次汇总：没登记的轮次（如启动失败后残留的目录）不算。
    return store.conversations(counted=visible)


@conversation_router.get("/api/conversations/{conversation_id}")
def api_conversation(conversation_id: str, registered: ConversationRunsDep, cursor: int = 0, *,
                     store: StoreDep):
    _require_valid_run_id(conversation_id)
    if cursor < 0:
        raise HTTPException(400,"cursor 须为非负整数")
    # 只算数据库里登记在这个对话的轮次（#39）；读取检查已确认属于自己或属于示例。
    turns=store.conversation_turns(conversation_id, counted=registered)
    if not turns:
        raise HTTPException(404,"研究会话不存在")
    return {"conversation_id":conversation_id,"latest_run_id":turns[-1]["run_id"],"total":len(turns),
            "turns":turns[cursor:cursor+20],"next_cursor":cursor+20 if cursor+20<len(turns) else None}
