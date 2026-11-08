"""示例列表（阶段 2 第 2 步 #47；设计 docs/design/阶段2-公开试用.md §4）。不要求登录。

示例里各轮研究的内容走原来的查看接口（runs、conversations 的读取组），那里按“属于示例”放行。
"""
from __future__ import annotations

from fastapi import APIRouter

from dra.api.common import service_errors
from dra.api.deps import ReadyDatabaseDep, StoreDep
from dra.api.services import showcases

router = APIRouter()


@router.get("/api/showcases")
def api_showcases(*, db: ReadyDatabaseDep, store: StoreDep) -> list[dict]:
    with service_errors():
        return showcases.list_public(db, store)
