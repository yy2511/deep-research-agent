"""数据库（存储层）：连接、按操作开关的短会话、迁移状态检查。

决定见 ADR 0002（PostgreSQL）与 ADR 0006（SQLAlchemy 同步写法 + Alembic）：

- 会话按操作开、用完即关，不按请求持有；只把普通数据交给调用方（规则 1）。
- 同一个会话只在创建它的线程里使用（规则 2）。
- 运行只接受 PostgreSQL；SQLite 只给离线测试用（``allow_sqlite=True``）。

表定义在 ``dra.db_tables``，迁移脚本在 ``dra/migrations/``。
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from dra.paths import PROJECT_ROOT

DATABASE_URL_ENV = "DRA_DATABASE_URL"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
# 本机开发时地址可写在项目 .env；只读取这一项，不写入进程环境。测试会把它指向不存在的文件。
ENV_FILE = PROJECT_ROOT / ".env"
MIGRATE_HINT = "python -m dra.admin migrate（开发时 uv run python -m dra.admin migrate）"


class DatabaseConfigError(RuntimeError):
    """数据库地址缺失或不受支持。"""


class DatabaseNotReady(RuntimeError):
    """数据库连不上，或迁移不是最新。"""


def _safe(url: URL) -> str:
    return url.render_as_string(hide_password=True)


def parse_database_url(raw: str | URL, *, allow_sqlite: bool = False) -> URL:
    """解析并规范连接地址：PostgreSQL 统一用 psycopg 3 驱动。"""
    try:
        url = make_url(raw)
    except ArgumentError as exc:
        raise DatabaseConfigError(f"{DATABASE_URL_ENV} 不是有效的数据库地址") from exc
    if url.drivername in ("postgresql", "postgresql+psycopg"):
        return url.set(drivername="postgresql+psycopg")
    if allow_sqlite and url.get_backend_name() == "sqlite":
        return url
    raise DatabaseConfigError(
        f"只支持 PostgreSQL（postgresql+psycopg://...），当前为 {url.drivername}"
    )


def alembic_config(connection: Connection | None = None):
    """不依赖仓库根目录的 alembic.ini：镜像里只有 src/。"""
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def head_revision() -> str | None:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


class Database:
    """一个进程一份：持有连接池，按操作发放短会话。"""

    def __init__(self, engine: Engine):
        self.engine = engine
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_url(cls, raw: str | URL, *, allow_sqlite: bool = False) -> Database:
        url = parse_database_url(raw, allow_sqlite=allow_sqlite)
        if url.get_backend_name() == "sqlite":
            # 连接池会把同一条连接交给不同线程（每次只给一个线程用）。
            engine = create_engine(url, connect_args={"check_same_thread": False})
            event.listen(engine, "connect", _sqlite_foreign_keys)
        else:
            engine = create_engine(url, pool_pre_ping=True)
        return cls(engine)

    @classmethod
    def from_env(cls) -> Database:
        """地址取环境变量 DRA_DATABASE_URL；没有时读项目 .env 中的同名项。"""
        raw = os.environ.get(DATABASE_URL_ENV, "").strip() or _env_file_value(DATABASE_URL_ENV)
        if not raw:
            raise DatabaseConfigError(
                f"未配置 {DATABASE_URL_ENV}。Web 服务需要 PostgreSQL，"
                "例如 postgresql+psycopg://用户:密码@主机:5432/库名；"
                "本机开发可用 docker compose -f compose.dev-db.yaml up -d 启动"
            )
        return cls.from_url(raw)

    @property
    def safe_url(self) -> str:
        return _safe(self.engine.url)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """短会话：正常结束提交，出错回滚，最后关闭并归还连接。"""
        session = self._sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def ping(self) -> None:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))

    def current_revision(self) -> str | None:
        from alembic.runtime.migration import MigrationContext

        with self.engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()

    def check_ready(self) -> None:
        """启动检查：能连上，且迁移已是最新。不自动迁移。"""
        try:
            current = self.current_revision()
        except SQLAlchemyError as exc:
            raise DatabaseNotReady(
                f"连不上数据库 {self.safe_url}：{exc.__class__.__name__}"
            ) from exc
        head = head_revision()
        if current != head:
            raise DatabaseNotReady(
                f"数据库迁移不是最新（当前 {current or '空库'}，最新 {head}）。"
                f"请先执行 {MIGRATE_HINT}"
            )

    def upgrade(self) -> str | None:
        """升级到最新迁移，返回升级后的版本。"""
        from alembic import command

        with self.engine.begin() as connection:
            command.upgrade(alembic_config(connection), "head")
        return self.current_revision()

    def dispose(self) -> None:
        self.engine.dispose()


def _env_file_value(name: str) -> str:
    if not ENV_FILE.is_file():
        return ""
    from dotenv import dotenv_values

    return (dotenv_values(ENV_FILE).get(name) or "").strip()


def _sqlite_foreign_keys(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
