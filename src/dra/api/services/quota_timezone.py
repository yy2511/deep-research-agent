"""Quota-day timezone changes: owner-only, versioned, at the old zone's next midnight."""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text

from dra.api.services.db_errors import database_errors
from dra.api.services.errors import Conflict, Forbidden, InvalidRequest, Unavailable
from dra.db import Database
from dra.db_tables import PlatformGovernance, User, utcnow


def lock_governance(session):
    # SQLite ignores FOR UPDATE. Start its write transaction before any SELECT.
    if session.get_bind().dialect.name == "sqlite" and not session.in_transaction():
        session.execute(text("BEGIN IMMEDIATE"))
    row = session.scalar(select(PlatformGovernance).where(PlatformGovernance.id == 1).with_for_update())
    if row is None:
        raise Unavailable("平台治理配置尚未初始化，请先迁移数据库")
    return row


def effective_timezone_in_session(session, *, now: datetime | None = None) -> str:
    row = lock_governance(session)
    now = now or utcnow()
    if row.quota_timezone is None:
        from dra import runtime_config
        row.quota_timezone = runtime_config.current().trial.timezone
    if row.pending_timezone is not None and row.timezone_effective_at <= now:
        row.quota_timezone = row.pending_timezone
        row.pending_timezone = None
        row.timezone_effective_at = None
        row.version += 1
    return row.quota_timezone


def _view(row):
    return {"timezone": row.quota_timezone, "pending_timezone": row.pending_timezone,
            "effective_at": row.timezone_effective_at, "version": row.version}


def status(db: Database, actor_id: int) -> dict:
    with database_errors(), db.session() as session:
        effective_timezone_in_session(session)
        actor = session.get(User, actor_id)
        if actor is None or actor.status != "active" or actor.role not in ("owner", "admin"):
            raise Forbidden("需要管理员权限")
        return _view(session.get(PlatformGovernance, 1))


def schedule(db: Database, actor_id: int, timezone: str, expected_version: int) -> dict:
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise InvalidRequest("请选择有效的 IANA 时区") from None
    now = utcnow()
    with database_errors(), db.session() as session:
        current = effective_timezone_in_session(session, now=now)
        row = session.get(PlatformGovernance, 1)
        actor = session.scalar(select(User).where(User.id == actor_id).with_for_update())
        if actor is None or actor.status != "active" or actor.role != "owner" or row.owner_user_id != actor_id:
            raise Forbidden("只有平台负责人可以变更额度时区")
        if row.version != expected_version:
            raise Conflict("配置已变化，请刷新后重新确认")
        before = _view(row).copy()
        if timezone == current:
            row.pending_timezone = row.timezone_effective_at = None
        else:
            row.pending_timezone = timezone
            local = now.astimezone(ZoneInfo(current))
            row.timezone_effective_at = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        row.version += 1
        # The management service owns serialization of the audit before/after record.
        from dra.api.services import user_management
        user_management.audit_timezone(session, actor_id, before, _view(row))
        return _view(row)
