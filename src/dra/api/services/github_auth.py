"""GitHub 登录（OAuth App）。设计：docs/design/阶段2-公开试用.md §2；决定：ADR 0007。

只申请公开资料（``scope`` 为空），不要邮箱、不要仓库权限。GitHub 的访问令牌只在
``fetch_profile`` 里用一次取资料，不保存、不返回。按 GitHub 的数字 id 认人。

不依赖 FastAPI：``state`` Cookie 与跳转在接口层（``routers/auth.py``）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlencode

import requests

from dra.api.services import auth as auth_service
from dra.api.services.auth import ExternalProfile, LoginResult
from dra.db import Database

CLIENT_ID_ENV = "DRA_GITHUB_CLIENT_ID"
CLIENT_SECRET_ENV = "DRA_GITHUB_CLIENT_SECRET"

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
# 外部请求的超时：GitHub 正常响应在 1 秒内；超时按 GitHub 不可用处理。
REQUEST_TIMEOUT_S = 10
_USER_AGENT = "deep-research-agent"


class GitHubLoginError(RuntimeError):
    """GitHub 没有确认用户身份：换令牌失败、取资料失败或返回内容不对。消息不含令牌和密钥。"""


@dataclass(frozen=True)
class GitHubConfig:
    client_id: str
    client_secret: str

    def __repr__(self) -> str:  # 不让密钥出现在日志和报错里
        return f"GitHubConfig(client_id={self.client_id!r}, client_secret='***')"


def config_from_env() -> GitHubConfig | None:
    """两个都配置了才启用 GitHub 登录。读环境变量，没有时读项目 .env（与数据库地址相同）。"""
    from dra.db import _env_file_value

    def read(name: str) -> str:
        return os.environ.get(name, "").strip() or _env_file_value(name)

    client_id, client_secret = read(CLIENT_ID_ENV), read(CLIENT_SECRET_ENV)
    if not client_id or not client_secret:
        return None
    return GitHubConfig(client_id, client_secret)


def authorize_url(config: GitHubConfig, *, state: str, redirect_uri: str) -> str:
    query = {"client_id": config.client_id, "redirect_uri": redirect_uri, "state": state,
             "scope": "", "allow_signup": "true"}
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


def fetch_profile(config: GitHubConfig, *, code: str, redirect_uri: str) -> ExternalProfile:
    """用一次性的 code 换访问令牌，再取用户公开资料。令牌只在这个函数里存在。"""
    try:
        token_response = requests.post(
            TOKEN_URL,
            data={"client_id": config.client_id, "client_secret": config.client_secret,
                  "code": code, "redirect_uri": redirect_uri},
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
            timeout=REQUEST_TIMEOUT_S,
        )
        token = _json(token_response).get("access_token")
        if token_response.status_code != 200 or not isinstance(token, str) or not token:
            # GitHub 拒绝时返回 200 + {"error": "bad_verification_code", ...}；只记错误类别。
            raise GitHubLoginError(f"GitHub 未发放令牌（{_json(token_response).get('error') or token_response.status_code}）")
        user_response = requests.get(
            USER_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "User-Agent": _USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"},
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.RequestException as exc:
        raise GitHubLoginError(f"连接 GitHub 失败（{type(exc).__name__}）") from None
    if user_response.status_code != 200:
        raise GitHubLoginError(f"GitHub 未返回用户资料（{user_response.status_code}）")
    data = _json(user_response)
    github_id, login = data.get("id"), data.get("login")
    if not isinstance(github_id, int) or isinstance(github_id, bool) or not isinstance(login, str) or not login:
        raise GitHubLoginError("GitHub 返回的用户资料不完整")
    name = data.get("name") if isinstance(data.get("name"), str) and data.get("name").strip() else None
    avatar = data.get("avatar_url") if isinstance(data.get("avatar_url"), str) else None
    return ExternalProfile(provider="github", subject=str(github_id), login=login[:100],
                           display_name=(name or login).strip()[:100],
                           avatar_url=avatar if avatar and avatar.startswith("https://") and len(avatar) <= 500 else None)


def login(db: Database, profile: ExternalProfile) -> LoginResult:
    return auth_service.login_external(db, profile)


def _json(response) -> dict:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
