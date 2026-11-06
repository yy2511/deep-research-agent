"""账号接口：注册、登录、登出、当前用户。设计 §2，ADR 0005。

注册、登录不要求已登录；登出、``/api/me`` 要求登录。Cookie 的读写只在这里做，业务规则在
``dra.api.services.auth``。查库的接口写成普通 ``def``（ADR 0006 规则 4）。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response

from dra.api.common import _StrictPayload, service_errors
from dra.api.deps import SESSION_COOKIE, CurrentUserDep, ReadyDatabaseDep
from dra.api.services import auth as auth_service
from dra.api.services.auth import UserInfo

router = APIRouter()

# Cookie 本身只是载体：在浏览器里保存 400 天（浏览器允许的上限），是否有效以服务端会话为准
# （30 天无活动过期，有活动时服务端延长）。
_COOKIE_MAX_AGE = 400 * 24 * 3600


class RegisterReq(_StrictPayload):
    invite_code: str
    email: str
    password: str


class LoginReq(_StrictPayload):
    email: str
    password: str


def _me(user: UserInfo) -> dict:
    return {"id": user.id, "email": user.email, "role": user.role}


def _set_session_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=_COOKIE_MAX_AGE, path="/", httponly=True, samesite="lax",
        # 经 HTTPS 访问时才加 Secure；本机 http 开发不加，否则浏览器不回传。
        secure=request.url.scheme == "https",
    )


@router.post("/api/auth/register", status_code=201)
def api_register(req: RegisterReq, request: Request, response: Response, *, db: ReadyDatabaseDep) -> dict:
    with service_errors():
        result = auth_service.register(db, req.invite_code, req.email, req.password)
    _set_session_cookie(response, request, result.token)
    return _me(result.user)


@router.post("/api/auth/login")
def api_login(req: LoginReq, request: Request, response: Response, *, db: ReadyDatabaseDep) -> dict:
    with service_errors():
        result = auth_service.login(db, req.email, req.password)
    _set_session_cookie(response, request, result.token)
    return _me(result.user)


@router.post("/api/auth/logout")
def api_logout(request: Request, response: Response, _user: CurrentUserDep, *,
               db: ReadyDatabaseDep) -> dict:
    with service_errors():
        auth_service.logout(db, request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax",
                           secure=request.url.scheme == "https")
    return {"logged_out": True}


@router.get("/api/me")
def api_me(user: CurrentUserDep) -> dict:
    return _me(user)
