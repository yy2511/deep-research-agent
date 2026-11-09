"""本地资料库接口：上传、列表、移出、全文、原件、检索。

每个用户只操作自己的资料库（#37，``library/users/<user_id>/``）：别人的资料编号在自己的目录里
找不到，按不存在处理。
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from dra.api.common import _ROOT, _original_response
from dra.api.deps import MEMBERS_ONLY, CurrentUserDep
from dra.library import MAX_BYTES, LibraryError, get_library

# 试用访客不能用资料库（#49）。
router = APIRouter(dependencies=MEMBERS_ONLY)


@router.get("/api/library")
def api_library(user: CurrentUserDep):
    return get_library(user.id).list_documents()


@router.post("/api/library/demo", status_code=202)
def api_library_demo(user: CurrentUserDep):
    directory = _ROOT / "fixtures" / "library_demo"
    rows = json.loads((directory / "manifest.json").read_text())
    library = get_library(user.id)
    imported = []
    try:
        for row in rows:
            data = (directory / row["file_name"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise LibraryError("示例资料校验失败")
            imported.append(library.import_bytes(row["file_name"], data))
    except LibraryError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"versions": imported}


@router.post("/api/library/documents", status_code=202)
async def api_library_upload(request: Request, user: CurrentUserDep, name: str,
                             replace: str | None = None):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "文件不能超过 10 MiB")
        data.extend(chunk)
    try:
        return get_library(user.id).import_bytes(name, bytes(data), replace=replace)
    except LibraryError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/api/library/documents/{document_id}")
def api_library_remove(document_id: str, user: CurrentUserDep):
    try:
        get_library(user.id).remove(document_id)
        return {"removed": True}
    except LibraryError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/api/library/versions/{version_id}/text", response_class=PlainTextResponse)
def api_library_text(version_id: str, user: CurrentUserDep):
    try:
        version = get_library(user.id).read_version(version_id)
        if version["status"] == "original":
            raise HTTPException(409, "原件尚无文字索引，请读取原件")
        return version["text"]
    except (LibraryError, OSError) as exc:
        raise HTTPException(404, "资料版本不存在、未就绪或校验失败") from exc


@router.get("/api/library/versions/{version_id}/original")
def api_library_original(version_id: str, user: CurrentUserDep):
    try:
        library = get_library(user.id)
        return _original_response(library.original(version_id), library.read_version(version_id))
    except (LibraryError, OSError) as exc:
        raise HTTPException(404, "资料原件不存在或校验失败") from exc


class LibrarySearchReq(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    document_ids: list[str] = Field(min_length=1, max_length=50)
    rerank: bool = False


@router.post("/api/library/search")
def api_library_search(req: LibrarySearchReq, user: CurrentUserDep):
    try:
        library = get_library(user.id)
        selection = library.pin(req.document_ids)
        return {"selection": selection, "results": [d.model_dump() for d in library.search(req.query, selection, rerank=req.rerank)]}
    except LibraryError as exc:
        raise HTTPException(400, str(exc)) from exc
