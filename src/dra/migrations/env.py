"""Alembic 运行环境。

两种入口：
- ``python -m dra.admin migrate`` / 测试：``dra.db.Database.upgrade`` 把已打开的连接放进
  ``config.attributes["connection"]``；
- 开发时生成新迁移：仓库根目录 ``uv run alembic revision --autogenerate -m "说明"``，
  连接地址取 ``DRA_DATABASE_URL``。
"""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from dra.db import DATABASE_URL_ENV, DatabaseConfigError, parse_database_url
from dra.db_tables import Base, UTCDateTime

config = context.config
target_metadata = Base.metadata


def _render_item(type_, obj, _autogen_context):
    """生成迁移时把自定义时间类型写成普通的带时区时间，迁移脚本不依赖应用代码。"""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite 的 ALTER TABLE 能力有限，改表用批量模式（ADR 0006）。
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _configure(connection)
        return
    import os

    raw = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not raw:
        raise DatabaseConfigError(f"生成或执行迁移需要 {DATABASE_URL_ENV}")
    engine = create_engine(parse_database_url(raw))
    try:
        with engine.begin() as connection:
            _configure(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("不支持离线（--sql）模式迁移")
run_migrations_online()
