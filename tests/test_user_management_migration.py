"""已有 SQLite 数据升级保留身份、会话、计数；负责人只能显式初始化。"""
import pytest
from alembic import command
from sqlalchemy import text, select
from sqlalchemy.exc import IntegrityError

from dra.db import Database, alembic_config
from dra.db_tables import PlatformGovernance, User, UserQuotaPolicy

pytestmark = pytest.mark.real_auth


def test_upgrade_preserves_existing_users_sessions_and_daily_counts(tmp_path):
    db = Database.from_url(f"sqlite:///{tmp_path / 'upgrade.sqlite'}", allow_sqlite=True)
    try:
        with db.engine.begin() as connection:
            command.upgrade(alembic_config(connection), '0005')
        with db.engine.begin() as connection:
            for i, role in enumerate(('guest', 'user', 'admin'), 1):
                connection.execute(text("INSERT INTO users (id,email,password_hash,role,status,created_at,updated_at) "
                    "VALUES (:id,:email,'test-hash',:role,'active',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
                    {"id": i, "email": f"{role}@example.com", "role": role})
            connection.execute(text("INSERT INTO sessions (user_id,token_hash,expires_at,created_at,last_seen_at) "
                                    "VALUES (2,'test-token','2099-01-01',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
            connection.execute(text("INSERT INTO daily_counters (scope,day,kind,count) VALUES ('user:1','2026-11-12','run',2)"))
        db.upgrade()
        with db.session() as s:
            assert [(u.id, u.role, u.management_version) for u in s.scalars(select(User).order_by(User.id))] == [
                (1, 'guest', 0), (2, 'user', 0), (3, 'admin', 0)]
            assert s.get(UserQuotaPolicy, 2).mode == 'unlimited'
            assert s.get(UserQuotaPolicy, 1) is None
            assert s.get(UserQuotaPolicy, 3) is None
            assert s.get(PlatformGovernance, 1).owner_user_id is None
            assert s.execute(text("SELECT count FROM daily_counters WHERE scope='user:1'")).scalar_one() == 2
            assert s.execute(text("SELECT user_id FROM sessions WHERE token_hash='test-token'")).scalar_one() == 2
            assert s.execute(text('PRAGMA foreign_key_check')).all() == []
    finally:
        db.dispose()


def test_database_guards_owner_uniqueness_and_active_status(database):
    with database.session() as s:
        s.add(User(email='owner@example.com', role='owner', status='active'))
    with pytest.raises(IntegrityError), database.session() as s:
        s.add(User(email='owner2@example.com', role='owner', status='active'))
    with pytest.raises(IntegrityError), database.session() as s:
        owner = s.scalar(select(User).where(User.role == 'owner'))
        owner.status = 'disabled'
