"""多个路由共用的小工具：公开 ID 校验、严格请求体基类、原件响应、事件精简投影。"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict

_ROOT = Path(__file__).resolve().parents[3]


_PUBLIC_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
_RUN_ID_RE = re.compile(_PUBLIC_ID_PATTERN)


def _require_valid_run_id(run_id: str) -> None:
    """run_id 直接拼 runs/<id> 文件路径;非法形态在触达 RunStore/文件系统前拒绝。

    此前唯一防线是 ASGI 层对编码斜杠(%2f)的路由处理——偶然安全;显式校验后
    不再依赖框架实现细节(设计安全)。合法 run 形如 20990101-000000-deadbeef。
    """
    if not _RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(404, "run 不存在")


class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _original_response(data: bytes, document: dict):
    is_pdf = document.get("suffix") == ".pdf"
    binary = document.get("status") == "original" or document.get("suffix") not in (".pdf", ".txt", ".md")
    return Response(data, media_type="application/pdf" if is_pdf else "application/octet-stream" if binary else "text/plain; charset=utf-8",
                    headers={"Content-Disposition": ("attachment" if binary and not is_pdf else "inline") + "; filename*=UTF-8''" + quote(document["file_name"]),
                             "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})


def _compact_event(event: dict) -> dict:
    """仅改变传输投影；事件身份/状态/观测保留，原始日志和内存记录不修改。"""
    fields = {
        "llm_usage": ("request_messages", "request_tools"),
        "llm_call": ("input", "output", "tools"),
        "subagent_tool_call": ("arguments_raw", "result_full"),
    }.get(event.get("type"), ())
    if not isinstance(event.get("seq"), int) or (not any(k in event for k in fields)
            and event.get("type") != "subagent_tool_call"):
        return event  # 无稳定 seq 的旧记录保持原格式。
    return {**{k: v for k, v in event.items() if k not in fields}, "detail_seq": event["seq"]}
