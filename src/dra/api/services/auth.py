"""账号与登录（业务层）。设计：docs/design/阶段1-数据库与用户.md §2；决定：ADR 0005。

- 密码用 argon2id 哈希；登录令牌、邀请码在库里只存 SHA-256。
- 每个操作自己开短会话、只返回普通数据（ADR 0006 规则 1、2）；密码哈希这类慢操作放在事务外。
- 不依赖 FastAPI：Cookie 的读写在接口层，管理命令（dra.admin）也直接调用这里。
"""
from __future__ import annotations

import hashlib
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError

from dra.api.services.errors import (
    Conflict,
    Forbidden,
    InvalidRequest,
    NotFound,
    Unauthorized,
    Unavailable,
)
from dra.db import Database
from dra.db_tables import Invite, LoginSession, User, utcnow

# 新增限制（设计“新增的限制”）：密码长度、会话无活动有效期、邀请码默认有效期。
PASSWORD_MIN = 8
PASSWORD_MAX = 256
SESSION_IDLE = timedelta(days=30)
# 有活动时延长会话：距上次记录超过这么久才写库，避免每个请求都写。
SESSION_TOUCH_INTERVAL = timedelta(days=1)
INVITE_DEFAULT_DAYS = 14
EMAIL_MAX = 320

INVITE_INVALID = "邀请码无效或已使用"
EMAIL_TAKEN = "该邮箱已注册"
LOGIN_FAILED = "邮箱或密码错误"
ACCOUNT_DISABLED = "账号已停用"
DB_UNAVAILABLE = "数据库暂时不可用，请稍后再试"

_hasher = PasswordHasher()


@dataclass(frozen=True)
class UserInfo:
    """交给接口层和研究流程的普通数据，不带数据库会话。"""

    id: int
    email: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass(frozen=True)
class LoginResult:
    user: UserInfo
    token: str


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    local, sep, domain = email.partition("@")
    if (not sep or not local or "." not in domain or "@" in domain or len(email) > EMAIL_MAX
            or any(c.isspace() for c in email)):
        raise InvalidRequest("邮箱格式不正确")
    return email


def validate_password(password: str) -> None:
    if not PASSWORD_MIN <= len(password or "") <= PASSWORD_MAX:
        raise InvalidRequest(f"密码须为 {PASSWORD_MIN}–{PASSWORD_MAX} 个字符")


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@cache
def _dummy_hash() -> str:
    # 邮箱不存在时也做一次同等耗时的校验，让“邮箱不存在”和“密码错误”难以区分。
    return _hasher.hash(secrets.token_urlsafe(16))


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def _info(user: User) -> UserInfo:
    return UserInfo(id=user.id, email=user.email, role=user.role)


@contextmanager
def _database_errors() -> Iterator[None]:
    """连接类故障翻译成“暂时不可用”；约束冲突等交给调用方各自处理。"""
    try:
        yield
    except (OperationalError, InterfaceError) as exc:
        raise Unavailable(DB_UNAVAILABLE) from exc


def _open_session(session, user_id: int, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    session.add(LoginSession(user_id=user_id, token_hash=hash_secret(token), created_at=now,
                             last_seen_at=now, expires_at=now + SESSION_IDLE))
    return token


def _revoke_user_sessions(session, user_id: int, now: datetime) -> None:
    session.execute(
        update(LoginSession)
        .where(LoginSession.user_id == user_id, LoginSession.revoked_at.is_(None))
        .values(revoked_at=now)
        .execution_options(synchronize_session=False)
    )


def register(db: Database, invite_code: str, email: str, password: str) -> LoginResult:
    """凭邀请码注册普通用户并直接登录。邀请码与新用户在同一事务内生效。"""
    email = normalize_email(email)
    validate_password(password)
    code = (invite_code or "").strip()
    if not code:
        raise InvalidRequest(INVITE_INVALID)
    password_hash = _hasher.hash(password)
    now = utcnow()
    try:
        with _database_errors(), db.session() as session:
            invite = session.scalars(select(Invite).where(
                Invite.code_hash == hash_secret(code),
                Invite.used_at.is_(None),
                Invite.expires_at > now,
            )).first()
            if invite is None:
                raise InvalidRequest(INVITE_INVALID)
            if session.scalars(select(User.id).where(User.email == email)).first() is not None:
                raise Conflict(EMAIL_TAKEN)
            user = User(email=email, password_hash=password_hash, role="user", status="active",
                        created_at=now, updated_at=now)
            session.add(user)
            session.flush()
            # 条件更新：两人同时用同一个码，只有一个能改到这一行（设计“出错时怎么办”）。
            claimed = session.execute(
                update(Invite)
                .where(Invite.id == invite.id, Invite.used_at.is_(None))
                .values(used_at=now, used_by=user.id)
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                raise InvalidRequest(INVITE_INVALID)
            token = _open_session(session, user.id, now)
            info = _info(user)
    except IntegrityError as exc:
        # 同一邮箱并发注册：唯一约束只让一个成功。
        raise Conflict(EMAIL_TAKEN) from exc
    return LoginResult(info, token)


def login(db: Database, email: str, password: str) -> LoginResult:
    try:
        email = normalize_email(email)
    except InvalidRequest:
        email = ""
    password = password or ""
    if len(password) > PASSWORD_MAX:
        raise Unauthorized(LOGIN_FAILED)
    with _database_errors(), db.session() as session:
        user = session.scalars(select(User).where(User.email == email)).first() if email else None
        found = (user.id, user.password_hash, user.status) if user else None
    if found is None:
        _verify(_dummy_hash(), password)
        raise Unauthorized(LOGIN_FAILED)
    user_id, password_hash, status = found
    if not _verify(password_hash, password):
        raise Unauthorized(LOGIN_FAILED)
    if status != "active":
        raise Forbidden(ACCOUNT_DISABLED)
    new_hash = _hasher.hash(password) if _hasher.check_needs_rehash(password_hash) else None
    now = utcnow()
    with _database_errors(), db.session() as session:
        # 慢的密码校验在事务外做完；签发会话前，在这一行的锁下复核密码和状态没有在这期间被改。
        # 重设密码、停用账号先改这一行再吊销会话（同一事务），两边按行锁排队：
        # - 重设先提交：这里读到新哈希，拒绝；
        # - 这里先提交：新会话已存在，随后的重设会把它一并吊销。
        # （#34 审查发现：没有复核时，校验后、签发前完成的重设挡不住旧密码登录。）
        user = session.scalars(select(User).where(User.id == user_id).with_for_update()).first()
        if user is None or user.password_hash != password_hash:
            raise Unauthorized(LOGIN_FAILED)
        if user.status != "active":
            raise Forbidden(ACCOUNT_DISABLED)
        if new_hash is not None:
            user.password_hash = new_hash
            user.updated_at = now
        token = _open_session(session, user_id, now)
        info = _info(user)
    return LoginResult(info, token)


def resolve_session(db: Database, token: str | None) -> UserInfo | None:
    """按 Cookie 里的令牌找到当前用户；无效、过期、已吊销或账号停用都返回 None。"""
    if not token or len(token) > 256:
        return None
    now = utcnow()
    with _database_errors(), db.session() as session:
        row = session.execute(
            select(LoginSession, User)
            .join(User, User.id == LoginSession.user_id)
            .where(
                LoginSession.token_hash == hash_secret(token),
                LoginSession.revoked_at.is_(None),
                LoginSession.expires_at > now,
                User.status == "active",
            )
        ).first()
        if row is None:
            return None
        login_session, user = row
        if now - login_session.last_seen_at >= SESSION_TOUCH_INTERVAL:
            login_session.last_seen_at = now
            login_session.expires_at = now + SESSION_IDLE
        return _info(user)


def logout(db: Database, token: str | None) -> None:
    if not token:
        return
    with _database_errors(), db.session() as session:
        session.execute(
            update(LoginSession)
            .where(LoginSession.token_hash == hash_secret(token), LoginSession.revoked_at.is_(None))
            .values(revoked_at=utcnow())
            .execution_options(synchronize_session=False)
        )


# ---- 管理操作（dra.admin 命令调用） -------------------------------------------------

def create_user(db: Database, email: str, password: str, *, role: str = "user") -> UserInfo:
    email = normalize_email(email)
    validate_password(password)
    if role not in ("user", "admin"):
        raise InvalidRequest("角色只能是 user 或 admin")
    password_hash = _hasher.hash(password)
    now = utcnow()
    try:
        with _database_errors(), db.session() as session:
            if session.scalars(select(User.id).where(User.email == email)).first() is not None:
                raise Conflict(EMAIL_TAKEN)
            user = User(email=email, password_hash=password_hash, role=role, status="active",
                        created_at=now, updated_at=now)
            session.add(user)
            session.flush()
            return _info(user)
    except IntegrityError as exc:
        raise Conflict(EMAIL_TAKEN) from exc


def create_invite(db: Database, *, days: int = INVITE_DEFAULT_DAYS, note: str | None = None,
                  created_by: int | None = None) -> tuple[str, datetime]:
    """生成一个邀请码；明文只在这里返回一次，库里只存哈希。"""
    if days < 1:
        raise InvalidRequest("邀请码有效期至少 1 天")
    code = secrets.token_urlsafe(12)
    now = utcnow()
    expires_at = now + timedelta(days=days)
    with _database_errors(), db.session() as session:
        session.add(Invite(code_hash=hash_secret(code), created_by=created_by, note=note,
                           expires_at=expires_at, created_at=now))
    return code, expires_at


def _user_id_by_email(session, email: str) -> int:
    user_id = session.scalars(select(User.id).where(User.email == normalize_email(email))).first()
    if user_id is None:
        raise NotFound("用户不存在")
    return user_id


def reset_password(db: Database, email: str, new_password: str) -> None:
    """重设密码并吊销该用户全部会话。"""
    validate_password(new_password)
    password_hash = _hasher.hash(new_password)
    now = utcnow()
    with _database_errors(), db.session() as session:
        user_id = _user_id_by_email(session, email)
        session.execute(update(User).where(User.id == user_id)
                        .values(password_hash=password_hash, updated_at=now)
                        .execution_options(synchronize_session=False))
        _revoke_user_sessions(session, user_id, now)


def set_user_status(db: Database, email: str, status: str) -> None:
    """停用（同时吊销会话）或恢复账号。"""
    if status not in ("active", "disabled"):
        raise InvalidRequest("状态只能是 active 或 disabled")
    now = utcnow()
    with _database_errors(), db.session() as session:
        user_id = _user_id_by_email(session, email)
        session.execute(update(User).where(User.id == user_id)
                        .values(status=status, updated_at=now)
                        .execution_options(synchronize_session=False))
        if status == "disabled":
            _revoke_user_sessions(session, user_id, now)
