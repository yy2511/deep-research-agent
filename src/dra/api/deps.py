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


# ---- 归属检查（阶段 1 第 4 步 #31；设计 §3.1） -----------------------------------------
# 带研究编号 / 对话编号的接口放进单独的路由分组，在分组上统一挂下面的检查；
# 新接口放进分组就自动受保护。不属于当前用户与不存在返回同一个 404，不透露是否存在。

_RUN_NOT_FOUND = "run 不存在"
_CONVERSATION_NOT_FOUND = "研究会话不存在"


def owned_run(run_id: str, request: Request, user: CurrentUserDep) -> str:
    """路径里的 run_id 必须属于当前用户。普通 def：在线程里开短会话，查完即关（ADR 0006 规则 5）。"""
    from dra.api.services import run_records
    from dra.api.services.ids import RUN_ID_RE

    if not RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(404, _RUN_NOT_FOUND)
    with service_errors():
        owner = run_records.run_owner(require_db(request), run_id)
    if owner != user.id:
        raise HTTPException(404, _RUN_NOT_FOUND)
    return run_id


def owned_conversation(conversation_id: str, request: Request, user: CurrentUserDep) -> str:
    from dra.api.services import run_records
    from dra.api.services.ids import RUN_ID_RE

    if not RUN_ID_RE.fullmatch(conversation_id):
        raise HTTPException(404, _CONVERSATION_NOT_FOUND)
    with service_errors():
        owner = run_records.conversation_owner(require_db(request), conversation_id)
    if owner != user.id:
        raise HTTPException(404, _CONVERSATION_NOT_FOUND)
    return conversation_id


def visible_runs(request: Request, user: CurrentUserDep):
    """列表与对话汇总用：判断某个 run_id 是否登记给当前用户的函数。

    对话也按它过滤轮次：没登记的轮次（如启动失败后残留的目录）不算进对话（#39 审查）。
    """
    from dra.api.services import run_records

    with service_errors():
        ids = run_records.owned_run_ids(require_db(request), user.id)
    return ids.__contains__


VisibleRunsDep = Annotated[object, Depends(visible_runs)]
OWNED_RUN = [Depends(owned_run)]
OWNED_CONVERSATION = [Depends(owned_conversation)]


# ---- 读取组：属于自己，或属于示例（阶段 2 第 2 步 #47；设计 docs/design/阶段2-公开试用.md §4） --
# 带编号的“查看”接口放进各 router 文件的 read_router / conversation_router 分组，挂下面的检查；
# “修改”接口（追问、发消息、取消）仍在 run_router，挂 owned_run。这两组不要求先登录：
# - 属于示例：任何人都能读，包括未登录；
# - 没带登录 Cookie：按不存在处理（404），不透露是否存在；
# - 带了 Cookie 但已失效：401，让前端回到登录页（原来的行为，不让自己的研究突然显示“不存在”）；
# - 已登录：属于自己才能读，否则 404。
# tests/test_showcases.py 遍历这些接口，检查没有遗漏。

def _reader(request: Request) -> UserInfo | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    with service_errors():
        user = auth_service.resolve_session(require_db(request), token)
    if user is None:
        raise HTTPException(401, "未登录或登录已过期")
    return user


def readable_run(run_id: str, request: Request) -> str:
    from dra.api.services import showcases
    from dra.api.services.ids import RUN_ID_RE

    if not RUN_ID_RE.fullmatch(run_id):
        raise HTTPException(404, _RUN_NOT_FOUND)
    with service_errors():
        owner, public = showcases.run_access(require_db(request), run_id)
    if public:
        return run_id
    user = _reader(request)
    if user is None or owner != user.id:
        raise HTTPException(404, _RUN_NOT_FOUND)
    return run_id


def readable_conversation(conversation_id: str, request: Request) -> str:
    from dra.api.services import showcases
    from dra.api.services.ids import RUN_ID_RE

    if not RUN_ID_RE.fullmatch(conversation_id):
        raise HTTPException(404, _CONVERSATION_NOT_FOUND)
    with service_errors():
        owner, public = showcases.conversation_access(require_db(request), conversation_id)
    if public:
        return conversation_id
    user = _reader(request)
    if user is None or owner != user.id:
        raise HTTPException(404, _CONVERSATION_NOT_FOUND)
    return conversation_id


def conversation_runs(conversation_id: str, request: Request):
    """对话汇总用：判断某个 run_id 是否登记在这个对话里的函数（读取检查已先通过）。"""
    from dra.api.services import run_records

    with service_errors():
        ids = run_records.registered_conversation_run_ids(require_db(request), conversation_id)
    return ids.__contains__


ConversationRunsDep = Annotated[object, Depends(conversation_runs)]
READABLE_RUN = [Depends(readable_run)]
READABLE_CONVERSATION = [Depends(readable_conversation)]


# ---- 管理员权限（阶段 1 第 5 步 #32；设计 §4） -------------------------------------------
# 改全站设置（模型、服务商、外部资料服务、技能）的接口放进各 router 文件的 admin_router 分组，
# 在分组上统一挂这个检查；tests/test_admin_permissions.py 遍历这类接口，检查没有遗漏。

def require_admin(user: CurrentUserDep) -> UserInfo:
    """先要求登录（未登录仍是 401），再要求管理员身份。"""
    if not user.is_admin:
        raise HTTPException(403, "需要管理员权限")
    return user


ADMIN_ONLY = [Depends(require_admin)]
