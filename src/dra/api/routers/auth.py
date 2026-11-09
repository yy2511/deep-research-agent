"""账号接口：注册、登录、GitHub 登录、登出、当前用户。设计：阶段1 §2、阶段2 §2；ADR 0005、0007。

注册、登录、GitHub 登录、``/api/auth/options`` 不要求已登录；登出、``/api/me`` 要求登录。Cookie 的读写只在这里做，业务规则在
``dra.api.services.auth``。查库的接口写成普通 ``def``（ADR 0006 规则 4）。
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import RedirectResponse

from dra.api.common import _StrictPayload, service_errors
from dra.api.deps import SESSION_COOKIE, CurrentUserDep, ReadyDatabaseDep
from dra.api.services import auth as auth_service
from dra.api.services import github_auth
from dra.api.services import trial as trial_service
from dra.api.services.auth import UserInfo
from dra.api.services.errors import Forbidden, Unavailable

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
    # trial：访客的试用次数，只有 /api/me 会填（#49）；其余响应与之同形，非访客恒为 null。
    return {"id": user.id, "email": user.email, "role": user.role,
            "display_name": user.display_name, "avatar_url": user.avatar_url, "trial": None}


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
def api_me(user: CurrentUserDep, *, db: ReadyDatabaseDep) -> dict:
    """当前用户。访客（#49）另带 ``trial``：今天还能发起几次研究、上限、何时恢复。"""
    with service_errors():
        trial = trial_service.status(db, user)
    return {**_me(user), "trial": trial}


@router.get("/api/auth/options")
def api_auth_options() -> dict:
    """登录页据此决定是否显示“用 GitHub 登录”。不要求登录。"""
    return {"github": github_auth.config_from_env() is not None}


# ---- GitHub 登录（阶段 2 第 1 步 #45；设计 docs/design/阶段2-公开试用.md §2） --------------
# 两个接口都是浏览器整页跳转（不是 fetch），所以出错时跳回首页并带一个错误代码，由登录页显示说明；
# 不把 GitHub 返回的原文放进页面。

_STATE_COOKIE = "dra_oauth_state"
_STATE_PATH = "/api/auth/github"
_STATE_MAX_AGE = 600  # 在 GitHub 授权页停留超过 10 分钟需重新点登录


def _github_config() -> github_auth.GitHubConfig:
    config = github_auth.config_from_env()
    if config is None:
        raise HTTPException(404, "未配置 GitHub 登录")
    return config


def _callback_url(request: Request) -> str:
    return str(request.url_for("api_github_callback"))


def _back_to_login(request: Request, error: str) -> RedirectResponse:
    response = RedirectResponse(f"/?login_error={error}", status_code=303)
    _clear_state(response, request)
    return response


def _clear_state(response: Response, request: Request) -> None:
    response.delete_cookie(_STATE_COOKIE, path=_STATE_PATH, httponly=True, samesite="lax",
                           secure=request.url.scheme == "https")


@router.get("/api/auth/github/start")
def api_github_start(request: Request) -> RedirectResponse:
    """带用户去 GitHub 授权。随机 state 存在只给回调路径的短期 Cookie 里，回来时核对。"""
    config = _github_config()
    state = secrets.token_urlsafe(32)
    response = RedirectResponse(github_auth.authorize_url(config, state=state, redirect_uri=_callback_url(request)),
                                status_code=303)
    # SameSite=Lax：从 github.com 整页跳回本站的 GET 会带上它；别的网站发起的请求带不上。
    response.set_cookie(_STATE_COOKIE, state, max_age=_STATE_MAX_AGE, path=_STATE_PATH, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https")
    return response


@router.get("/api/auth/github/callback")
def api_github_callback(request: Request, *, db: ReadyDatabaseDep, code: str = "", state: str = "",
                        error: str = "") -> RedirectResponse:
    """GitHub 授权后跳回这里。核对 state → 换令牌取资料 → 找到或新建 guest → 签发会话。

    state 不符时不去找 GitHub：防止别人把他自己的 GitHub 授权塞给你，让你登进他的账号。
    """
    config = _github_config()
    if error:  # 用户在 GitHub 上点了取消，或 GitHub 拒绝
        return _back_to_login(request, "denied")
    expected = request.cookies.get(_STATE_COOKIE, "")
    if not expected or not state or not secrets.compare_digest(expected, state) or not code:
        return _back_to_login(request, "state")
    try:
        profile = github_auth.fetch_profile(config, code=code[:200], redirect_uri=_callback_url(request))
    except github_auth.GitHubLoginError:
        return _back_to_login(request, "github")
    try:
        result = github_auth.login(db, profile)
    except Forbidden:
        return _back_to_login(request, "disabled")
    except Unavailable:
        return _back_to_login(request, "unavailable")
    response = RedirectResponse("/", status_code=303)
    _clear_state(response, request)
    _set_session_cookie(response, request, result.token)
    return response
