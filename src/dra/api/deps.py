"""依赖注入：存储与运行管理器挂在 app.state，接口通过 Depends 获取。

实例在 dra.api.app 里创建一次；测试替换 app.state 上的对象即可，不改模块全局变量。
"""
from __future__ import annotations

from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request

from dra.api.common import service_errors
from dra.api.services import auth as auth_service
from dra.api.services.auth import UserInfo
from dra.api.services.runs import RunRegistry
from dra.db import Database
from dra.runstore import PlanAttemptStore, RunStore


def get_store(request: Request) -> RunStore:
    return request.app.state.store


def get_plan_store(request: Request) -> PlanAttemptStore:
    return request.app.state.plan_store


def get_registry(request: Request) -> RunRegistry:
    return request.app.state.registry


def get_db(request: Request) -> Database | None:
    """数据库对象（连接池）。会话由业务层按操作自己开关，接口不持有会话（ADR 0006）。

    启动检查通过前为 None。
    """
    return request.app.state.db


StoreDep = Annotated[RunStore, Depends(get_store)]
PlanStoreDep = Annotated[PlanAttemptStore, Depends(get_plan_store)]
RegistryDep = Annotated[RunRegistry, Depends(get_registry)]
DatabaseDep = Annotated[Database | None, Depends(get_db)]

SESSION_COOKIE = "dra_session"


def require_db(request: Request) -> Database:
    db = request.app.state.db
    if db is None:
        raise HTTPException(503, "数据库未就绪")
    return db


ReadyDatabaseDep = Annotated[Database, Depends(require_db)]


def current_user(request: Request) -> UserInfo:
    """当前登录用户（ADR 0005）。

    写成普通 ``def``：FastAPI 放到线程池执行，``async def`` 接口也能用；内部只开一个短会话，
    查完即关，返回普通数据，进度流这类长连接不会占住数据库连接（ADR 0006 规则 1、5）。
    同一请求里多处依赖它时，FastAPI 只执行一次。
    """
    db = require_db(request)
    with service_errors():
        user = auth_service.resolve_session(db, request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise HTTPException(401, "未登录或登录已过期")
    return user


CurrentUserDep = Annotated[UserInfo, Depends(current_user)]

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def same_origin(request: Request) -> None:
    """跨站请求伪造的第二道防线（第一道是 SameSite=Lax）：修改类请求带了 Origin 就必须是本站。

    沙箱化的预览页（Content-Security-Policy: sandbox）发出的请求 Origin 为 ``null``，同样被拒绝。
    没带 Origin 的请求（命令行工具、测试客户端）不受影响。
    """
    if request.method not in _UNSAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin is None:
        return
    if urlsplit(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "跨站请求被拒绝")
