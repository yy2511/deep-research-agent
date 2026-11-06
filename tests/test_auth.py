"""账号与登录（#29）：注册、登录、登出、会话、Cookie、Origin 检查、全部接口要求登录、管理命令。

设计 docs/design/阶段1-数据库与用户.md §2 与“验收场景”；决定 ADR 0005。
这些测试不用 conftest 的默认测试管理员，走真实的注册、登录与 Cookie。
"""
from __future__ import annotations

import io
import threading
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from dra import admin
from dra.api.app import app
from dra.api.deps import SESSION_COOKIE, CurrentUserDep, current_user
from dra.api.services import auth
from dra.api.services.errors import InvalidRequest
from dra.db_tables import Invite, LoginSession, User, utcnow

pytestmark = pytest.mark.real_auth

PASSWORD = "correct horse battery"
PUBLIC_API = {"/api/auth/register", "/api/auth/login"}


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def invite(database):
    code, _ = auth.create_invite(database)
    return code


def _register(client, code, email="Alice@Example.com", password=PASSWORD):
    return client.post("/api/auth/register",
                       json={"invite_code": code, "email": email, "password": password})


def _shift_clock(monkeypatch, delta: timedelta):
    shifted = utcnow() + delta
    monkeypatch.setattr(auth, "utcnow", lambda: shifted)


# ---- 注册 -----------------------------------------------------------------------

def test_register_with_invite_logs_in(client, invite, database):
    response = _register(client, invite)
    assert response.status_code == 201
    assert response.json()["email"] == "alice@example.com"
    assert response.json()["role"] == "user"
    assert client.get("/api/me").json() == response.json()
    with database.session() as session:
        used = session.scalars(select(Invite)).one()
        user = session.scalars(select(User)).one()
    assert used.used_by == user.id and used.used_at is not None
    assert user.password_hash.startswith("$argon2id$")
    assert PASSWORD not in user.password_hash


@pytest.mark.parametrize("make_code", ["unknown", "used", "expired"])
def test_register_rejects_bad_invites_with_one_message(client, database, monkeypatch, make_code):
    code, _ = auth.create_invite(database, days=1)
    if make_code == "unknown":
        code = "no-such-code"
    elif make_code == "used":
        assert _register(TestClient(app), code, email="first@example.com").status_code == 201
    else:
        _shift_clock(monkeypatch, timedelta(days=2))
    response = _register(client, code)
    assert response.status_code == 400
    assert response.json()["detail"] == auth.INVITE_INVALID


def test_register_rejects_duplicate_email_ignoring_case(client, database):
    first, _ = auth.create_invite(database)
    second, _ = auth.create_invite(database)
    assert _register(client, first, email="bob@example.com").status_code == 201
    response = _register(TestClient(app), second, email="  BOB@example.com ")
    assert response.status_code == 409
    assert response.json()["detail"] == auth.EMAIL_TAKEN
    with database.session() as session:
        # 失败的注册不消耗邀请码。
        assert session.get(Invite, 2).used_at is None


@pytest.mark.parametrize("password", ["short", "x" * 257])
def test_register_enforces_password_length(client, invite, password):
    response = _register(client, invite, password=password)
    assert response.status_code == 400
    assert "8–256" in response.json()["detail"]


@pytest.mark.parametrize("email", ["no-at-sign", "a@b", "a b@example.com", "@example.com"])
def test_register_rejects_malformed_email(client, invite, email):
    assert _register(client, invite, email=email).status_code == 400


def test_password_boundaries_are_inclusive():
    auth.validate_password("x" * 8)
    auth.validate_password("x" * 256)
    with pytest.raises(InvalidRequest):
        auth.validate_password("x" * 7)


@pytest.mark.postgres
def test_invite_claimed_by_someone_else_mid_registration_is_rejected(database):
    """确定性地重现竞争：本次注册读到邀请码未用之后、标记已用之前，另一人抢先用掉了它。

    只靠先查后改会让两人都注册成功；条件更新（used_at IS NULL 才改）必须让后到者失败并回滚。
    SQLite 在写事务期间会锁住整个库，无法插入另一条写入，所以只在 PostgreSQL 上跑。
    """
    from sqlalchemy import event, text
    from sqlalchemy.orm import Session

    code, _ = auth.create_invite(database)

    def someone_else_claims(session, _context):
        with database.engine.begin() as other:
            other.execute(text("UPDATE invites SET used_at = now()"))

    event.listen(Session, "after_flush", someone_else_claims, once=True)
    try:
        with pytest.raises(InvalidRequest, match=auth.INVITE_INVALID):
            auth.register(database, code, "late@example.com", PASSWORD)
    finally:
        if event.contains(Session, "after_flush", someone_else_claims):
            event.remove(Session, "after_flush", someone_else_claims)
    with database.session() as session:
        assert session.scalars(select(User)).all() == []


@pytest.mark.postgres
def test_concurrent_registrations_with_one_invite_only_one_wins(database):
    code, _ = auth.create_invite(database)
    barrier = threading.Barrier(4)
    outcomes = []

    def attempt(index):
        barrier.wait()
        try:
            auth.register(database, code, f"user{index}@example.com", PASSWORD)
            outcomes.append("ok")
        except InvalidRequest:
            outcomes.append("rejected")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["ok", "rejected", "rejected", "rejected"]
    with database.session() as session:
        assert len(session.scalars(select(User)).all()) == 1


# ---- 登录与会话 -------------------------------------------------------------------

@pytest.fixture
def alice(database):
    return auth.create_user(database, "alice@example.com", PASSWORD)


def _login(client, email="alice@example.com", password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_login_sets_http_only_lax_cookie(client, alice):
    response = _login(client, email=" ALICE@example.com")
    assert response.status_code == 200
    assert response.json() == {"id": alice.id, "email": "alice@example.com", "role": "user"}
    header = response.headers["set-cookie"].lower()
    assert header.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in header and "samesite=lax" in header and "path=/" in header
    assert "secure" not in header


def test_cookie_is_secure_over_https(alice):
    response = _login(TestClient(app, base_url="https://testserver"))
    assert "secure" in response.headers["set-cookie"].lower()


def test_session_token_is_stored_hashed(client, alice, database):
    _login(client)
    token = client.cookies[SESSION_COOKIE]
    with database.session() as session:
        stored = session.scalars(select(LoginSession)).one()
    assert stored.token_hash == auth.hash_secret(token) != token


@pytest.mark.parametrize("email,password", [
    ("alice@example.com", "wrong password"),
    ("nobody@example.com", PASSWORD),
    ("not-an-email", PASSWORD),
    ("alice@example.com", "x" * 300),
])
def test_login_failures_share_one_message(client, alice, email, password):
    response = _login(client, email, password)
    assert response.status_code == 401
    assert response.json()["detail"] == auth.LOGIN_FAILED
    assert SESSION_COOKIE not in client.cookies


def test_disabled_account_cannot_log_in(client, alice, database):
    auth.set_user_status(database, alice.email, "disabled")
    response = _login(client)
    assert response.status_code == 403
    assert response.json()["detail"] == auth.ACCOUNT_DISABLED


def test_logout_revokes_the_session(client, alice):
    _login(client)
    token = client.cookies[SESSION_COOKIE]
    assert client.post("/api/auth/logout").status_code == 200
    assert SESSION_COOKIE not in client.cookies
    stale = TestClient(app, cookies={SESSION_COOKIE: token})
    assert stale.get("/api/me").status_code == 401


def test_reset_password_and_disable_revoke_existing_sessions(client, alice, database):
    _login(client)
    auth.reset_password(database, alice.email, "a brand new password")
    assert client.get("/api/me").status_code == 401
    assert _login(client).status_code == 401
    assert _login(client, password="a brand new password").status_code == 200
    auth.set_user_status(database, alice.email, "disabled")
    assert client.get("/api/me").status_code == 401


def test_session_expires_after_30_idle_days(client, alice, monkeypatch):
    _login(client)
    _shift_clock(monkeypatch, timedelta(days=30, seconds=1))
    response = client.get("/api/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "未登录或登录已过期"


def test_activity_extends_the_session(client, alice, monkeypatch, database):
    _login(client)
    _shift_clock(monkeypatch, timedelta(days=2))
    assert client.get("/api/me").status_code == 200
    _shift_clock(monkeypatch, timedelta(days=31))
    assert client.get("/api/me").status_code == 200


def test_activity_within_a_day_does_not_write(client, alice, monkeypatch, database):
    _login(client)
    with database.session() as session:
        before = session.scalars(select(LoginSession)).one().expires_at
    _shift_clock(monkeypatch, timedelta(hours=23))
    assert client.get("/api/me").status_code == 200
    with database.session() as session:
        assert session.scalars(select(LoginSession)).one().expires_at == before


def test_garbage_cookie_is_unauthorised(alice):
    for token in ("garbage", "x" * 5000):
        assert TestClient(app, cookies={SESSION_COOKIE: token}).get("/api/me").status_code == 401


# ---- 全部接口要求登录 --------------------------------------------------------------

def _dependency_calls(dependant) -> set:
    calls = set()
    for sub in dependant.dependencies:
        calls.add(sub.call)
        calls |= _dependency_calls(sub)
    return calls


def test_every_api_route_requires_login_except_register_and_login():
    """新加接口忘了要求登录，这里会失败（设计 §2）。"""
    missing = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        if route.path in PUBLIC_API:
            assert current_user not in _dependency_calls(route.dependant)
            continue
        if current_user not in _dependency_calls(route.dependant):
            missing.append(f"{sorted(route.methods)} {route.path}")
    assert missing == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/me"),
    ("GET", "/api/runs"),
    ("GET", "/api/conversations"),
    ("GET", "/api/config"),
    ("GET", "/api/library"),
    ("GET", "/api/skills"),
    ("GET", "/api/providers"),
    ("GET", "/api/demo/events"),
    ("GET", "/api/research/20990101-000000-deadbeef/stream"),
    ("POST", "/api/research"),
    ("POST", "/api/auth/logout"),
    ("PUT", "/api/config"),
])
def test_anonymous_requests_are_rejected(client, method, path):
    response = client.request(method, path, json={})
    assert response.status_code == 401


def test_healthz_and_frontend_stay_public(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/").status_code == 200


# ---- 跨站请求 --------------------------------------------------------------------

def test_cross_site_writes_are_rejected(client, alice):
    _login(client)
    for origin in ("https://evil.example", "null"):
        response = client.post("/api/auth/logout", headers={"Origin": origin})
        assert response.status_code == 403
        assert response.json()["detail"] == "跨站请求被拒绝"
    # 跨站读取不受影响；同站写入正常。
    assert client.get("/api/me", headers={"Origin": "https://evil.example"}).status_code == 200
    assert client.post("/api/auth/logout", headers={"Origin": "http://testserver"}).status_code == 200


def test_cross_site_login_is_rejected(client, alice):
    response = client.post("/api/auth/login", headers={"Origin": "https://evil.example"},
                           json={"email": alice.email, "password": PASSWORD})
    assert response.status_code == 403
    assert SESSION_COOKIE not in client.cookies


# ---- 长连接不占数据库连接 ------------------------------------------------------------

def test_current_user_returns_connection_before_streaming(alice, database):
    """ADR 0006 规则 1、5：识别用户后、推送期间，连接池里没有借出的连接。"""
    probe = FastAPI()
    probe.state.db = database
    seen = []

    @probe.get("/stream")
    async def stream(user: CurrentUserDep):
        async def body():
            seen.append(database.engine.pool.checkedout())
            yield f"{user.email}\n"
        return StreamingResponse(body())

    probe_client = TestClient(probe)
    token = auth.login(database, alice.email, PASSWORD).token
    probe_client.cookies.set(SESSION_COOKIE, token)
    with probe_client.stream("GET", "/stream") as response:
        assert response.read() == b"alice@example.com\n"
    assert seen == [0]


# ---- 管理命令 --------------------------------------------------------------------

@pytest.fixture
def cli_database(database, monkeypatch):
    monkeypatch.setattr(admin, "_ready_database", lambda: database)
    return database


def _run(monkeypatch, *argv, stdin=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return admin.main(list(argv))


def test_create_admin_then_log_in(client, cli_database, monkeypatch, capsys):
    assert _run(monkeypatch, "create-admin", "--email", "Boss@Example.com", "--password-stdin",
                stdin=PASSWORD + "\n") == 0
    assert "已创建管理员 boss@example.com" in capsys.readouterr().out
    response = _login(client, "boss@example.com", PASSWORD)
    assert response.status_code == 200 and response.json()["role"] == "admin"


def test_create_admin_prompts_twice(cli_database, monkeypatch, capsys):
    answers = iter([PASSWORD, "different"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert _run(monkeypatch, "create-admin", "--email", "boss@example.com") == 2
    assert "两次输入的密码不一致" in capsys.readouterr().err


def test_create_admin_reports_duplicate_and_weak_password(cli_database, monkeypatch, capsys):
    auth.create_user(cli_database, "boss@example.com", PASSWORD)
    assert _run(monkeypatch, "create-admin", "--email", "boss@example.com", "--password-stdin",
                stdin=PASSWORD + "\n") == 2
    assert auth.EMAIL_TAKEN in capsys.readouterr().err
    assert _run(monkeypatch, "create-admin", "--email", "new@example.com", "--password-stdin",
                stdin="short\n") == 2
    assert "8–256" in capsys.readouterr().err


def test_create_invite_prints_a_usable_code_once(client, cli_database, monkeypatch, capsys):
    assert _run(monkeypatch, "create-invite", "--days", "3", "--note", "给小王") == 0
    out = capsys.readouterr().out
    code = out.splitlines()[0].removeprefix("邀请码：")
    assert "唯一一次显示" in out
    with cli_database.session() as session:
        stored = session.scalars(select(Invite)).one()
    assert stored.note == "给小王" and stored.code_hash != code
    assert stored.expires_at - stored.created_at == timedelta(days=3)
    assert _register(client, code).status_code == 201


def test_create_invite_rejects_non_positive_days(cli_database, monkeypatch, capsys):
    assert _run(monkeypatch, "create-invite", "--days", "0") == 2
    assert "至少 1 天" in capsys.readouterr().err


def test_reset_password_and_disable_commands(client, cli_database, alice, monkeypatch, capsys):
    _login(client)
    assert _run(monkeypatch, "reset-password", "--email", alice.email, "--password-stdin",
                stdin="another long password\n") == 0
    assert client.get("/api/me").status_code == 401
    assert _run(monkeypatch, "disable-user", "--email", alice.email) == 0
    assert _login(client, password="another long password").status_code == 403
    assert _run(monkeypatch, "enable-user", "--email", alice.email) == 0
    assert _login(client, password="another long password").status_code == 200
    assert _run(monkeypatch, "disable-user", "--email", "ghost@example.com") == 2
    assert "用户不存在" in capsys.readouterr().err
