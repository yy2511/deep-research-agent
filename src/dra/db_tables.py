"""数据库表定义。设计见 docs/design/阶段1-数据库与用户.md §1。

字段是设计意图的落地；表结构变更一律写迁移脚本（dra/migrations/versions/），
已合并的迁移不改只增（开发规范 §3）。``tests/test_database.py`` 检查本文件与迁移一致。
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

# 约束统一命名：Alembic 在 SQLite 批量改表时要靠名字找到约束。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# PostgreSQL 用 BIGINT 自增；SQLite 只有 INTEGER PRIMARY KEY 才自增。
BigId = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """带时区时间，库里一律存 UTC（ADR 0006）。

    SQLite 不保存时区，读出时补上 UTC；写入不带时区的时间直接报错，避免把本地时间当成 UTC。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("数据库时间必须带时区")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'admin')", name="role"),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
    )

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    # 存去掉首尾空格、转小写后的值（由业务层规范化）。
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user")
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Invite(Base):
    """邀请码只在生成时显示一次，库里只存哈希；created_by 为空表示命令行生成。"""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class LoginSession(Base):
    """登录会话（ADR 0005）。库里只存令牌的 SHA-256。"""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


# ---- 研究记录的归属（阶段 1 第 3 步，#30；设计 §1） --------------------------------
# 第 1 阶段运行状态仍以 runs/<run_id>/meta.json 为准，这里只记“属于谁、在哪个对话里”。

class Conversation(Base):
    """一串来回追问。id 沿用文件里的 conversation_id（即首轮的 run_id）。"""

    __tablename__ = "conversations"
    __table_args__ = (Index("ix_conversations_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ResearchTask(Base):
    """用户提的一个问题（首问或追问）。"""

    __tablename__ = "research_tasks"
    __table_args__ = (Index("ix_research_tasks_user_id_created_at", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    query: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class RunAttempt(Base):
    """一次实际执行，对应 runs/<run_id>/。第 1 阶段每个任务恰好一条。"""

    __tablename__ = "run_attempts"

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
