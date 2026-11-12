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
    UniqueConstraint,
    text,
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
    """账号。邮箱密码注册的有 email、password_hash；GitHub 登录的访客两者都为空（阶段 2，#45）。"""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('guest', 'user', 'admin', 'owner')", name="role"),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        CheckConstraint("role != 'owner' OR status = 'active'", name="owner_active"),
        CheckConstraint("management_version >= 0", name="management_version"),
        Index("uq_users_one_owner", "role", unique=True,
              sqlite_where=text("role = 'owner'"), postgresql_where=text("role = 'owner'")),
    )

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    # 存去掉首尾空格、转小写后的值（由业务层规范化）。唯一约束允许多个空值。
    email: Mapped[str | None] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # 显示用：GitHub 登录名与头像，每次 GitHub 登录时更新。
    display_name: Mapped[str | None] = mapped_column(String(100))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    role: Mapped[str] = mapped_column(String(16), default="user")
    status: Mapped[str] = mapped_column(String(16), default="active")
    management_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class UserIdentity(Base):
    """外部登录身份（阶段 2，#45；设计 docs/design/阶段2-公开试用.md §2）。

    ``subject`` 是对方系统里不变的编号（GitHub 的数字 id），不用会改的登录名。
    """

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("provider", "subject", name="uq_user_identities_provider_subject"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(64))
    # 对方系统里的登录名，供管理命令按名字找人；以 subject 为准。
    login: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


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
    # 访客试用：这次扣在哪一天的次数（YYYY-MM-DD，按试用时区）；撤销占位时据此退回。非访客为空（#49）。
    quota_day: Mapped[str | None] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---- 示例（阶段 2 第 2 步 #47；设计 docs/design/阶段2-公开试用.md §4） ----------------------

class Showcase(Base):
    """管理员设为示例的对话：所有人（含未登录）只读可见。不复制研究文件。

    作为示例期间对话冻结，原主人也不能追问（``run_records.reserve_followup``）。
    """

    __tablename__ = "showcases"

    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---- 试用次数（阶段 2 第 3 步 #49；设计 docs/design/阶段2-公开试用.md §3） ------------------

class DailyCounter(Base):
    """按天计数：``scope`` 是 ``user:<id>`` 或 ``guests``（全部访客合计），``kind`` 是 ``run`` 或 ``brief``。

    扣减用一条带条件的 upsert（未到上限才加 1），并发时不会超（services/trial.py）。
    """

    __tablename__ = "daily_counters"

    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    count: Mapped[int] = mapped_column(BigInteger, default=0)


# ---- 平台治理与逐用户额度（#64） ----------------------------------------------------

class PlatformGovernance(Base):
    __tablename__ = "platform_governance"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer, default=0)
    quota_timezone: Mapped[str | None] = mapped_column(String(64))
    pending_timezone: Mapped[str | None] = mapped_column(String(64))
    timezone_effective_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    tracking_started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class UserQuotaPolicy(Base):
    __tablename__ = "user_quota_policies"
    __table_args__ = (
        CheckConstraint("mode IN ('limited', 'unlimited')", name="mode"),
        CheckConstraint("(mode = 'unlimited' AND daily_runs_limit IS NULL AND daily_briefs_limit IS NULL) OR "
                        "(mode = 'limited' AND daily_runs_limit IS NOT NULL AND daily_briefs_limit IS NOT NULL "
                        "AND daily_runs_limit >= 0 AND daily_briefs_limit >= 0)", name="limits"),
        CheckConstraint("version >= 0", name="version"),
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    daily_runs_limit: Mapped[int | None] = mapped_column(BigInteger)
    daily_briefs_limit: Mapped[int | None] = mapped_column(BigInteger)
    version: Mapped[int] = mapped_column(Integer, default=0)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class QuotaReservation(Base):
    __tablename__ = "quota_reservations"
    __table_args__ = (
        CheckConstraint("kind IN ('run', 'brief')", name="kind"),
        CheckConstraint("state IN ('reserved', 'started', 'refunded')", name="state"),
    )
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    quota_day: Mapped[str] = mapped_column(String(10))
    scopes_json: Mapped[str] = mapped_column(Text)
    policy_version: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="reserved")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ManagementAudit(Base):
    __tablename__ = "management_audits"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    target_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64))
    before_json: Mapped[str] = mapped_column(Text)
    after_json: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class OwnerTransferProof(Base):
    __tablename__ = "owner_transfer_proofs"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    target_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    governance_version: Mapped[int] = mapped_column(Integer)
    actor_version: Mapped[int] = mapped_column(Integer)
    target_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime())
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    provider: Mapped[str] = mapped_column(String(32))
