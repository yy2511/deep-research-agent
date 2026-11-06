"""数据库接入（#28）：迁移、表与迁移一致、短会话、时间存 UTC、启动检查、健康检查、migrate 命令。

默认在临时 SQLite 上跑；加 --run-postgres 时同一批测试改连 PostgreSQL（ADR 0006）。
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from dra import admin
from dra.api.app import app
from dra.db import (
    DATABASE_URL_ENV,
    Database,
    DatabaseConfigError,
    DatabaseNotReady,
    head_revision,
    parse_database_url,
)
from dra.db_tables import Base, LoginSession, User, utcnow


def _user(email: str = "a@example.com", **fields) -> User:
    return User(email=email, password_hash="x", **fields)


# ---- 迁移与表结构 ---------------------------------------------------------------

def test_migrated_database_has_phase1_step1_tables(database):
    tables = set(inspect(database.engine).get_table_names())
    assert {"users", "invites", "sessions", "alembic_version"} <= tables
    assert database.current_revision() == head_revision()


def test_models_match_migrations(database):
    """表定义与迁移脚本一致：改了 db_tables.py 却忘了写迁移，这里会失败。"""
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    with database.engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_upgrade_is_idempotent(database):
    assert database.upgrade() == head_revision()
    assert database.upgrade() == head_revision()


def test_upgrade_from_empty_sqlite(tmp_path):
    fresh = Database.from_url(f"sqlite:///{tmp_path / 'empty.sqlite'}", allow_sqlite=True)
    try:
        assert fresh.current_revision() is None
        assert fresh.upgrade() == head_revision()
    finally:
        fresh.dispose()


# ---- 短会话与约束 -----------------------------------------------------------------

def test_session_commits_on_success(database):
    with database.session() as session:
        session.add(_user())
    with database.session() as session:
        stored = session.scalars(select(User)).one()
    assert (stored.email, stored.role, stored.status) == ("a@example.com", "user", "active")


def test_session_rolls_back_on_error(database):
    with pytest.raises(RuntimeError), database.session() as session:
        session.add(_user())
        session.flush()
        raise RuntimeError("boom")
    with database.session() as session:
        assert session.scalars(select(User)).all() == []


def test_email_is_unique(database):
    with database.session() as session:
        session.add(_user())
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(_user())


def test_role_and_status_are_constrained(database):
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(_user(role="root"))
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(_user(status="deleted"))


def test_foreign_keys_are_enforced(database):
    with pytest.raises(IntegrityError), database.session() as session:
        session.add(LoginSession(user_id=999, token_hash="h", expires_at=utcnow()))


def test_session_returns_connection_to_pool(database):
    with database.session() as session:
        session.add(_user())
    with database.session() as session:
        session.scalars(select(User)).all()
    assert database.engine.pool.checkedout() == 0


# ---- 时间一律存 UTC ---------------------------------------------------------------

def test_aware_datetimes_round_trip_as_utc(database):
    shanghai = timezone(timedelta(hours=8))
    created = datetime(2026, 11, 6, 20, 0, tzinfo=shanghai)
    with database.session() as session:
        session.add(_user(created_at=created))
    with database.session() as session:
        stored = session.scalars(select(User)).one().created_at
    assert stored.tzinfo is not None
    assert stored.utcoffset() == timedelta(0)
    assert stored == created
    assert stored == datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def test_naive_datetimes_are_rejected(database):
    with pytest.raises(Exception, match="必须带时区"), database.session() as session:
        session.add(_user(created_at=datetime(2026, 11, 6, 12, 0)))


# ---- 连接地址 --------------------------------------------------------------------

def test_postgres_urls_use_psycopg3():
    assert parse_database_url("postgresql://u:p@h/db").drivername == "postgresql+psycopg"
    assert parse_database_url("postgresql+psycopg://u:p@h/db").drivername == "postgresql+psycopg"


@pytest.mark.parametrize("raw", [
    "sqlite:///x.db",
    "mysql+pymysql://u:p@h/db",
    "postgresql+asyncpg://u:p@h/db",
    "postgresql+psycopg2://u:p@h/db",
])
def test_only_postgres_is_accepted_for_running(raw):
    with pytest.raises(DatabaseConfigError, match="只支持 PostgreSQL"):
        Database.from_url(raw)


def test_sqlite_only_when_explicitly_allowed(tmp_path):
    url = f"sqlite:///{tmp_path / 'x.sqlite'}"
    assert parse_database_url(url, allow_sqlite=True).get_backend_name() == "sqlite"


def test_invalid_url_is_reported():
    with pytest.raises(DatabaseConfigError, match="不是有效的数据库地址"):
        parse_database_url("not a url")


def test_missing_url_is_reported(monkeypatch):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    with pytest.raises(DatabaseConfigError, match=DATABASE_URL_ENV):
        Database.from_env()


def test_password_is_hidden_in_messages():
    database = Database.from_url("postgresql://user:s3cret@127.0.0.1:1/dra_test")
    try:
        assert "s3cret" not in database.safe_url
        with pytest.raises(DatabaseNotReady) as raised:
            database.check_ready()
        assert "s3cret" not in str(raised.value)
        assert "连不上数据库" in str(raised.value)
    finally:
        database.dispose()


# ---- 启动检查 --------------------------------------------------------------------

def test_check_ready_requires_latest_migration(tmp_path):
    fresh = Database.from_url(f"sqlite:///{tmp_path / 'fresh.sqlite'}", allow_sqlite=True)
    try:
        with pytest.raises(DatabaseNotReady, match="dra.admin migrate"):
            fresh.check_ready()
        fresh.upgrade()
        fresh.check_ready()
    finally:
        fresh.dispose()


def test_web_starts_with_ready_database(database):
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert app.state.db is database


def test_web_refuses_to_start_without_database_url(monkeypatch, capsys):
    monkeypatch.setattr(app.state, "db", None)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    with pytest.raises(DatabaseConfigError), TestClient(app):
        pass
    assert "数据库未就绪，服务不启动" in capsys.readouterr().out
    assert app.state.db is None


def test_web_refuses_to_start_with_non_postgres_url(monkeypatch):
    monkeypatch.setattr(app.state, "db", None)
    monkeypatch.setenv(DATABASE_URL_ENV, "sqlite:///whatever.db")
    with pytest.raises(DatabaseConfigError, match="只支持 PostgreSQL"), TestClient(app):
        pass


def test_web_refuses_to_start_when_migrations_are_behind(monkeypatch, tmp_path):
    fresh = Database.from_url(f"sqlite:///{tmp_path / 'behind.sqlite'}", allow_sqlite=True)
    monkeypatch.setattr(app.state, "db", fresh)
    try:
        with pytest.raises(DatabaseNotReady, match="迁移不是最新"), TestClient(app):
            pass
    finally:
        fresh.dispose()


def test_healthz_reports_unavailable_database(database, monkeypatch):
    def down():
        raise OSError("connection refused")

    monkeypatch.setattr(database, "ping", down)
    response = TestClient(app).get("/healthz")
    assert response.status_code == 503
    assert response.json()["detail"] == "database is unavailable"


# ---- migrate 命令 ----------------------------------------------------------------

def test_migrate_command_requires_database_url(monkeypatch, capsys):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    assert admin.main(["migrate"]) == 2
    assert DATABASE_URL_ENV in capsys.readouterr().err


def test_migrate_command_rejects_sqlite(monkeypatch, capsys):
    monkeypatch.setenv(DATABASE_URL_ENV, "sqlite:///whatever.db")
    assert admin.main(["migrate"]) == 2
    assert "只支持 PostgreSQL" in capsys.readouterr().err


@pytest.mark.postgres
def test_migrate_command_on_postgres(monkeypatch, capsys):
    monkeypatch.setenv(DATABASE_URL_ENV, os.environ["DRA_TEST_DATABASE_URL"])
    assert admin.main(["migrate"]) == 0
    assert f"已是最新（{head_revision()}）" in capsys.readouterr().out


@pytest.mark.postgres
def test_web_starts_from_environment_on_postgres(monkeypatch):
    """生产路径：启动时按 DRA_DATABASE_URL 自己建库对象，退出时释放。"""
    monkeypatch.setattr(app.state, "db", None)
    monkeypatch.setenv(DATABASE_URL_ENV, os.environ["DRA_TEST_DATABASE_URL"])
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert app.state.db is not None
    assert app.state.db is None


def test_url_falls_back_to_project_env_file(monkeypatch, tmp_path):
    """本机开发：地址写在 .env 也能启动；只读这一项，不写入进程环境。"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"{DATABASE_URL_ENV}=postgresql://u:p@127.0.0.1:1/dra_dev\nOTHER_SECRET=x\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("dra.db.ENV_FILE", env_file)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv("OTHER_SECRET", raising=False)
    database = Database.from_env()
    try:
        assert database.engine.url.database == "dra_dev"
        assert DATABASE_URL_ENV not in os.environ
        assert "OTHER_SECRET" not in os.environ
    finally:
        database.dispose()


def test_environment_variable_wins_over_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(f"{DATABASE_URL_ENV}=postgresql://u:p@h/from_file\n", encoding="utf-8")
    monkeypatch.setattr("dra.db.ENV_FILE", env_file)
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://u:p@h/from_env")
    database = Database.from_env()
    try:
        assert database.engine.url.database == "from_env"
    finally:
        database.dispose()
