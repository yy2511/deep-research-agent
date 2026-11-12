"""管理员只读汇总：真实鉴权、日期口径、权限隔离和安全响应。"""
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from dra.api.app import app
from dra.api.deps import ADMIN_ONLY, SESSION_COOKIE
from dra.api.repositories import operations as repository
from dra.api.routers import operations as router
from dra.api.services import auth, operations, run_records
from dra.db_tables import Conversation, LoginSession, ResearchTask, RunAttempt, Showcase, User

pytestmark = pytest.mark.real_auth
PASSWORD = "correct horse battery"


def at(day, hour=0):
    return datetime.fromisoformat(day).replace(tzinfo=UTC, hour=hour)


@pytest.fixture
def admin(database):
    user = auth.create_user(database, "admin@example.com", PASSWORD, role="admin")
    return TestClient(app, cookies={SESSION_COOKIE: auth.login(database, user.email, PASSWORD).token})


def seed(database):
    with database.session() as s:
        a = User(email="PRIVATE_EMAIL@example.com", password_hash="PRIVATE_HASH", role="user",
                 created_at=at("2024-02-28"))
        b = User(display_name="PRIVATE_NAME", role="guest", status="disabled", created_at=at("2024-02-29"))
        s.add_all([a, b]); s.flush()
        aid, bid = a.id, b.id
        for cid, uid, day in [("20240228-000000-aaaaaaaa", aid, "2024-02-28"),
                              ("20240229-000000-bbbbbbbb", bid, "2024-02-29")]:
            s.add(Conversation(id=cid, user_id=uid, created_at=at(day)))
        s.flush()
        for cid, uid, day, hour in [
            ("20240228-000000-aaaaaaaa", aid, "2024-02-28", 23),
            ("20240228-000000-aaaaaaaa", aid, "2024-02-29", 0),
            ("20240228-000000-aaaaaaaa", aid, "2024-02-29", 12),
            ("20240229-000000-bbbbbbbb", bid, "2024-02-29", 23),
            ("20240229-000000-bbbbbbbb", bid, "2024-03-01", 0),
        ]:
            s.add(ResearchTask(user_id=uid, conversation_id=cid, query="PRIVATE_QUERY", created_at=at(day,hour)))
        s.add(Showcase(conversation_id="20240229-000000-bbbbbbbb", title="PRIVATE_SHOWCASE_TITLE"))
    return aid


def test_aggregate_counts_and_leap_day_boundaries(database, admin):
    seed(database)
    response = admin.get("/api/admin/operations?start_date=2024-02-28&end_date=2024-02-29")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["totals"] == {"users":3,"enabled_users":2,"disabled_users":1,
                              "roles":{"admin":1,"guest":1,"user":1,"owner":0},"showcases":1}
    assert data["period"] == {"new_users":2,"research_users":2,"research_tasks":4,"conversations":2}
    assert data["daily"] == [
        {"date":"2024-02-28","new_users":1,"research_users":1,"research_tasks":1,"conversations":1},
        {"date":"2024-02-29","new_users":1,"research_users":2,"research_tasks":3,"conversations":1},
    ]
    assert data["scope"] == "registered_database_records" and data["timezone"] == "UTC"
    assert not any(secret in response.text for secret in ("PRIVATE", "@", "aaaaaaaa", "bbbbbbbb"))
    assert set(data) == {"as_of","scope","timezone","range","totals","period","daily"}


def test_zero_fill_default_and_all_time(database):
    data = operations.overview(database)
    assert len(data["daily"]) == 7
    assert all(all(v == 0 for k,v in day.items() if k != "date") for day in data["daily"])
    assert data["totals"]["users"] == 0
    assert data["range"]["end_date"] == data["as_of"].date()
    assert len(operations.overview(database, all_time=True)["daily"]) == 1
    seed(database)
    all_data = operations.overview(database, all_time=True)
    assert all_data["range"]["start_date"] == date(2024,2,28)
    assert all_data["period"]["research_tasks"] == 5
    assert all_data["daily"][-1]["research_tasks"] == 0
    empty = operations.overview(database,date(2024,2,1),date(2024,2,27))
    assert empty["totals"] == all_data["totals"]
    assert all(v == 0 for v in empty["period"].values())


@pytest.mark.parametrize("query,status", [
    ("start_date=2024-01-01",400), ("end_date=2024-01-01",400),
    ("start_date=2024-02-02&end_date=2024-01-01",400),
    ("start_date=9999-12-31&end_date=9999-12-31",400),
    ("start_date=2024-01-01&end_date=2024-01-02&all_time=true",400),
    ("start_date=no&end_date=2024-01-02",422),
    ("start_date=2024-02-30&end_date=2024-03-01",422),
    ("start_date=1704067200&end_date=2024-01-02",422),
])
def test_invalid_ranges(admin,query,status):
    assert admin.get("/api/admin/operations?"+query).status_code == status


def test_authentication_and_role_changes(database, admin):
    assert TestClient(app).get("/api/admin/operations").status_code == 401
    user=auth.create_user(database,"user@example.com",PASSWORD)
    c=TestClient(app,cookies={SESSION_COOKIE:auth.login(database,user.email,PASSWORD).token})
    assert c.get("/api/admin/operations").status_code == 403
    with database.session() as s:
        s.get(User,user.id).role="guest"
    assert c.get("/api/admin/operations").status_code == 403
    with database.session() as s:
        boss=s.scalar(select(User).where(User.email=="admin@example.com"));boss.role="user"
    assert admin.get("/api/admin/operations").status_code == 403
    with database.session() as s:
        boss=s.scalar(select(User).where(User.email=="admin@example.com"));boss.role="admin";boss.status="disabled"
    assert admin.get("/api/admin/operations").status_code == 401
    with database.session() as s:
        login=s.scalar(select(LoginSession).where(LoginSession.user_id==user.id));login.expires_at=at("2020-01-01")
    assert c.get("/api/admin/operations").status_code == 401


def test_private_research_stays_private(database,admin):
    owner=seed(database)
    cid="20240228-000000-aaaaaaaa"
    with database.session() as s:
        task=s.scalar(select(ResearchTask).where(ResearchTask.user_id==owner))
        s.add(RunAttempt(task_id=task.id,run_id=cid))
    assert admin.get(f"/api/runs/{cid}").status_code == 404
    assert admin.get(f"/api/conversations/{cid}").status_code == 404


def test_error_is_safe_and_no_fallback(database,admin,monkeypatch):
    def fail(*a,**k):
        raise OperationalError("PRIVATE_SQL", {"secret":"PRIVATE_CREDENTIAL"}, Exception("SECRET"))
    monkeypatch.setattr(repository,"snapshot",fail)
    r=admin.get("/api/admin/operations")
    assert r.status_code == 503
    assert r.json() == {"detail":"运营统计暂不可用，请稍后重试"}


def test_repository_uses_only_aggregates_and_constant_queries(database):
    seed(database)
    statements=[]
    def capture(conn,cursor,statement,parameters,context,executemany):statements.append(statement.lower())
    event.listen(database.engine,"before_cursor_execute",capture)
    try:
        operations.overview(database,date(2024,2,28),date(2024,3,1))
    finally:
        event.remove(database.engine,"before_cursor_execute",capture)
    assert len(statements) <= 10
    sql="\n".join(statements)
    for column in ("email","password_hash","display_name","query","sessions","run_attempts"):
        assert column not in sql
    assert all(not s.startswith(("insert","update","delete")) for s in statements)


def test_import_and_released_reservation_follow_database_timestamps(database,tmp_path):
    import json
    admin=auth.create_user(database,"history@example.com",PASSWORD,role="admin")
    rid="20240228-000000-cccccccc"
    run_records.reserve_new_run(database,admin.id,rid,"secret")
    run_records.release(database,rid)
    assert operations.overview(database,all_time=True)["period"]["research_tasks"] == 0
    directory=tmp_path/rid;directory.mkdir()
    (directory/"meta.json").write_text(json.dumps({"run_id":rid,"started_at":at("2024-02-28",23).timestamp(),"query":"secret"}))
    run_records.import_legacy(database,tmp_path,admin.email)
    result=operations.overview(database,date(2024,2,28),date(2024,2,29))
    assert result["daily"][0]["research_tasks"] == 1
    assert result["daily"][1]["research_tasks"] == 0
    assert result["period"]["research_users"] == 1


def test_route_has_admin_dependency():
    assert [d.dependency for d in router.router.dependencies] == [d.dependency for d in ADMIN_ONLY]


def test_snapshot_does_not_mix_concurrent_commits(database):
    if database.engine.dialect.name == "sqlite":
        with database.engine.connect() as c:
            c.exec_driver_sql("PRAGMA journal_mode=WAL")
    with repository.snapshot(database) as snap:
        before = snap.totals()
        with database.session() as s:
            s.add(User(role="guest"))
        assert snap.totals() == before
    assert operations.overview(database)["totals"]["users"] == before["users"] + 1


def test_offset_timestamps_are_grouped_by_utc(database):
    with database.session() as s:
        s.add(User(role="guest", created_at=datetime.fromisoformat("2024-03-01T01:00:00+08:00")))
    result = operations.overview(database,date(2024,2,29),date(2024,3,1))
    assert [d["new_users"] for d in result["daily"]] == [1,0]


def test_database_not_ready_is_not_empty_statistics(admin,monkeypatch):
    monkeypatch.setattr(app.state,"db",None)
    assert admin.get("/api/admin/operations").status_code == 503


def test_owner_count_is_included_without_private_data(database):
    from dra.api.services import operations
    from dra.db_tables import User
    with database.session() as session:
        session.add(User(email="owner@example.com", password_hash="not-real", role="owner"))
    response = operations.overview(database)
    assert response["totals"]["roles"]["owner"] == 1
    assert "owner@example.com" not in str(response)
