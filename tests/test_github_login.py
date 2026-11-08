"""GitHub 登录（#45）：OAuth 跳转、state 核对、建 guest、再次登录同一账号、出错不建账号、管理命令。

设计 docs/design/阶段2-公开试用.md §2；决定 ADR 0007。走真实的会话 Cookie（real_auth）。
GitHub 的两次请求（换令牌、取资料）用替身，不访问真实 GitHub。
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy import select

from dra import admin
from dra.api.app import app
from dra.api.deps import SESSION_COOKIE
from dra.api.services import auth, github_auth
from dra.db_tables import LoginSession, User, UserIdentity

pytestmark = pytest.mark.real_auth

CALLBACK = "http://testserver/api/auth/github/callback"
ACCESS_TOKEN = "gho_access_token_never_stored"
SECRET = "client-secret-value"


class FakeResponse:
    def __init__(self, status: int, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeGitHub:
    """替身：记录收到的请求，按设定返回。"""

    def __init__(self):
        self.profile = {"id": 4242, "login": "octo", "name": "Octo Cat",
                        "avatar_url": "https://avatars.githubusercontent.com/u/4242"}
        self.token_response = FakeResponse(200, {"access_token": ACCESS_TOKEN, "token_type": "bearer"})
        self.user_status = 200
        self.calls: list[tuple[str, dict]] = []
        self.raise_on_post: Exception | None = None

    def post(self, url, *, data, headers, timeout):
        self.calls.append((url, data))
        if self.raise_on_post:
            raise self.raise_on_post
        return self.token_response

    def get(self, url, *, headers, timeout):
        self.calls.append((url, headers))
        assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        return FakeResponse(self.user_status, dict(self.profile))


@pytest.fixture
def github(monkeypatch):
    monkeypatch.setenv(github_auth.CLIENT_ID_ENV, "client-id-123")
    monkeypatch.setenv(github_auth.CLIENT_SECRET_ENV, SECRET)
    fake = FakeGitHub()
    monkeypatch.setattr(github_auth.requests, "post", fake.post)
    monkeypatch.setattr(github_auth.requests, "get", fake.get)
    return fake


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


def _start(client) -> str:
    response = client.get("/api/auth/github/start")
    assert response.status_code == 303
    return parse_qs(urlsplit(response.headers["location"]).query)["state"][0]


def _github_login(client) -> "requests.Response":
    state = _start(client)
    return client.get("/api/auth/github/callback", params={"code": "one-time-code", "state": state})


def _users(database):
    with database.session() as session:
        return session.scalars(select(User)).all()


# ---- 跳转与配置 -------------------------------------------------------------------

def test_github_login_is_hidden_until_configured(client, monkeypatch):
    monkeypatch.delenv(github_auth.CLIENT_ID_ENV, raising=False)
    monkeypatch.delenv(github_auth.CLIENT_SECRET_ENV, raising=False)
    assert client.get("/api/auth/options").json() == {"github": False}
    assert client.get("/api/auth/github/start").status_code == 404
    assert client.get("/api/auth/github/callback", params={"code": "c", "state": "s"}).status_code == 404
    monkeypatch.setenv(github_auth.CLIENT_ID_ENV, "only-id")
    assert client.get("/api/auth/options").json() == {"github": False}  # 两个都要有


def test_start_sends_the_user_to_github_with_a_state(client, github):
    assert client.get("/api/auth/options").json() == {"github": True}
    response = client.get("/api/auth/github/start")
    target = urlsplit(response.headers["location"])
    query = parse_qs(target.query, keep_blank_values=True)
    assert (target.scheme, target.netloc, target.path) == ("https", "github.com", "/login/oauth/authorize")
    assert query["client_id"] == ["client-id-123"]
    assert query["redirect_uri"] == [CALLBACK]
    assert query["scope"] == [""]  # 只要公开资料
    assert len(query["state"][0]) >= 40
    assert SECRET not in response.headers["location"]
    cookie = response.headers["set-cookie"]
    assert "dra_oauth_state=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Path=/api/auth/github" in cookie and "Max-Age=600" in cookie
    assert github.calls == []  # 还没找 GitHub


# ---- 登录成功 -------------------------------------------------------------------

def test_first_github_login_creates_a_guest_and_logs_in(client, github, database):
    response = _github_login(client)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert SESSION_COOKIE in response.cookies
    me = client.get("/api/me").json()
    assert me["role"] == "guest" and me["email"] is None
    assert me["display_name"] == "Octo Cat"
    assert me["avatar_url"] == "https://avatars.githubusercontent.com/u/4242"
    # 换令牌时带的是 code 和回调地址；GitHub 的访问令牌不进数据库。
    token_url, form = github.calls[0]
    assert token_url == github_auth.TOKEN_URL
    assert form["code"] == "one-time-code" and form["redirect_uri"] == CALLBACK
    with database.session() as session:
        identity = session.scalars(select(UserIdentity)).one()
        assert (identity.provider, identity.subject, identity.login) == ("github", "4242", "octo")
        rows = [repr(vars(row)) for model in (User, UserIdentity, LoginSession)
                for row in session.scalars(select(model)).all()]
    assert not any(ACCESS_TOKEN in row for row in rows)


def test_logging_in_again_reuses_the_account_and_refreshes_the_profile(client, github, database):
    _github_login(client)
    first_id = client.get("/api/me").json()["id"]
    github.profile.update(login="octo-renamed", name=None, avatar_url="https://avatars.githubusercontent.com/u/9")
    other = TestClient(app, follow_redirects=False)
    _github_login(other)
    me = other.get("/api/me").json()
    assert me["id"] == first_id
    assert me["display_name"] == "octo-renamed"  # 没填名字时用登录名
    assert me["avatar_url"] == "https://avatars.githubusercontent.com/u/9"
    assert len(_users(database)) == 1
    with database.session() as session:
        assert session.scalars(select(UserIdentity.login)).one() == "octo-renamed"


def test_two_different_github_accounts_get_two_guests(client, github, database):
    _github_login(client)
    github.profile.update(id=777, login="someone")
    _github_login(TestClient(app, follow_redirects=False))
    assert sorted(u.role for u in _users(database)) == ["guest", "guest"]


def test_simultaneous_first_logins_end_up_on_one_account(github, database, monkeypatch):
    """两个请求同时第一次登录同一身份：后到的撞上唯一约束，重试时找到先建好的那个。"""
    profile = auth.ExternalProfile(provider="github", subject="4242", login="octo")
    real_once = auth._login_external_once
    calls = []

    def racing(db, p):
        calls.append(p)
        if len(calls) == 1:
            real_once(db, p)  # “另一个请求”抢先建好
            from sqlalchemy.exc import IntegrityError
            raise IntegrityError("insert", {}, Exception("duplicate"))
        return real_once(db, p)

    monkeypatch.setattr(auth, "_login_external_once", racing)
    result = auth.login_external(database, profile)
    assert len(calls) == 2
    assert [u.id for u in _users(database)] == [result.user.id]


# ---- 出错时不建账号 -----------------------------------------------------------------

@pytest.mark.parametrize("break_it, error", [
    ("wrong_state", "state"),
    ("no_state_cookie", "state"),
    ("no_code", "state"),
    ("user_cancelled", "denied"),
])
def test_bad_callbacks_never_reach_github(client, github, database, break_it, error):
    state = _start(client)
    params = {"code": "one-time-code", "state": state}
    if break_it == "wrong_state":
        params["state"] = "attacker-state"
    elif break_it == "no_state_cookie":
        client.cookies.clear()
    elif break_it == "no_code":
        params.pop("code")
    else:
        params = {"error": "access_denied", "state": state}
    response = client.get("/api/auth/github/callback", params=params)
    assert response.status_code == 303
    assert response.headers["location"] == f"/?login_error={error}"
    assert SESSION_COOKIE not in response.cookies
    assert github.calls == [] and _users(database) == []


@pytest.mark.parametrize("failure", ["token_refused", "network", "user_401", "bad_profile", "not_json"])
def test_github_failures_send_the_user_back_without_an_account(client, github, database, failure):
    if failure == "token_refused":
        github.token_response = FakeResponse(200, {"error": "bad_verification_code"})
    elif failure == "network":
        github.raise_on_post = requests.ConnectionError("down")
    elif failure == "user_401":
        github.user_status = 401
    elif failure == "bad_profile":
        github.profile = {"id": "not-a-number", "login": "octo"}
    else:
        github.token_response = FakeResponse(502, ValueError("<html>"))
    response = _github_login(client)
    assert response.headers["location"] == "/?login_error=github"
    assert SESSION_COOKIE not in response.cookies
    assert _users(database) == []


def test_error_messages_never_contain_secrets(github):
    github.token_response = FakeResponse(200, {"error": "bad_verification_code"})
    config = github_auth.config_from_env()
    with pytest.raises(github_auth.GitHubLoginError) as caught:
        github_auth.fetch_profile(config, code="c", redirect_uri=CALLBACK)
    assert SECRET not in str(caught.value) and SECRET not in repr(config)


def test_disabled_github_account_cannot_log_in(client, github, database):
    _github_login(client)
    auth.set_user_status(database, None, "disabled", github="OCTO")  # 登录名不区分大小写
    assert client.get("/api/me").status_code == 401  # 停用时已吊销会话
    response = _github_login(TestClient(app, follow_redirects=False))
    assert response.headers["location"] == "/?login_error=disabled"
    assert SESSION_COOKIE not in response.cookies


# ---- 与密码登录、管理命令的关系 ------------------------------------------------------

def test_accounts_without_a_password_cannot_use_password_login(database):
    with database.session() as session:
        session.add(User(email="nopass@example.com", password_hash=None, role="user", status="active",
                         created_at=auth.utcnow(), updated_at=auth.utcnow()))
    with pytest.raises(auth.Unauthorized):
        auth.login(database, "nopass@example.com", "anything-at-all")


def test_admin_commands_find_github_users(client, github, database, monkeypatch, capsys):
    _github_login(client)
    monkeypatch.setattr(admin, "_ready_database", lambda: database)
    assert admin.main(["set-role", "--github", "octo", "--role", "user"]) == 0
    assert "GitHub 用户 octo 的角色已改为 user" in capsys.readouterr().out
    assert client.get("/api/me").json()["role"] == "user"  # 立即生效
    assert admin.main(["disable-user", "--github", "octo"]) == 0
    assert client.get("/api/me").status_code == 401
    assert admin.main(["enable-user", "--github", "octo"]) == 0
    assert admin.main(["set-role", "--github", "nobody", "--role", "user"]) == 2
    assert "用户不存在" in capsys.readouterr().err


def test_admin_commands_need_exactly_one_way_to_name_the_user(database, monkeypatch):
    monkeypatch.setattr(admin, "_ready_database", lambda: database)
    with pytest.raises(SystemExit):
        admin.main(["set-role", "--role", "user"])
    with pytest.raises(SystemExit):
        admin.main(["disable-user", "--email", "a@b.co", "--github", "octo"])
    with pytest.raises(auth.InvalidRequest):
        auth.set_role(database, "root", email="a@b.co")
