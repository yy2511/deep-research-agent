"""运营统计的只读数据库快照；只查询聚合字段，不读取私有内容。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Iterator

from sqlalchemy import Date, cast, func, select
from sqlalchemy.engine import Connection

from dra.db import Database
from dra.db_tables import Conversation, ResearchTask, Showcase, User


class Snapshot:
    def __init__(self, connection: Connection):
        self.connection = connection
        # 首条 SELECT 固定快照；数据库时间也固定在同一个只读事务内。
        self.as_of = connection.scalar(select(func.current_timestamp()))
        if self.as_of.tzinfo is None:
            self.as_of = self.as_of.replace(tzinfo=UTC)
        self.as_of = self.as_of.astimezone(UTC)

    def totals(self) -> dict:
        rows = self.connection.execute(
            select(User.role, User.status, func.count()).group_by(User.role, User.status)
        )
        result = {"users": 0, "enabled_users": 0, "disabled_users": 0,
                  "roles": {"guest": 0, "user": 0, "admin": 0}}
        for role, status, count in rows:
            result["users"] += count
            result["roles"][role] += count
            result["enabled_users" if status == "active" else "disabled_users"] += count
        result["showcases"] = self.connection.scalar(select(func.count()).select_from(Showcase))
        return result

    def earliest_date(self) -> date:
        values = [self.connection.scalar(select(func.min(model.created_at)))
                  for model in (User, Conversation, ResearchTask)]
        return min([v.astimezone(UTC).date() for v in values if v is not None]
                   + [self.as_of.date()])

    def activity(self, start: datetime, end: datetime) -> tuple[dict, dict]:
        """返回期间计数和稀疏的每日计数；空日期由业务层补齐。"""
        period = {}
        daily: dict[str, dict[str, int]] = {}
        for model, fields in (
            (User, {"new_users": func.count()}),
            (ResearchTask, {"research_tasks": func.count(),
                            "research_users": func.count(func.distinct(ResearchTask.user_id))}),
            (Conversation, {"conversations": func.count()}),
        ):
            created = model.created_at
            bounds = (created >= start, created < end)
            row = self.connection.execute(select(*fields.values()).select_from(model).where(*bounds)).one()
            period.update(zip(fields, row))
            day = (cast(func.timezone("UTC", created), Date)
                   if self.connection.dialect.name == "postgresql" else func.date(created))
            for row in self.connection.execute(
                select(day, *fields.values()).select_from(model).where(*bounds).group_by(day)
            ):
                key = row[0].isoformat() if isinstance(row[0], date) else row[0]
                daily.setdefault(key, {}).update(zip(fields, row[1:]))
        return period, daily


@contextmanager
def snapshot(db: Database) -> Iterator[Snapshot]:
    with db.engine.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection = connection.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True,
            )
        with connection.begin():
            if connection.dialect.name == "sqlite":
                # Python sqlite3 的 legacy transaction 模式不为 SELECT 自动发 BEGIN。
                connection.exec_driver_sql("BEGIN")
            yield Snapshot(connection)
