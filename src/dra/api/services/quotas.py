"""四种身份共享的每日计数与占位台账。

先锁治理行、用户行，再读有效角色/政策并原子扣次。角色转换不重置个人计数，
补偿只按原占位台账退款，避免身份变化后错退全站访客池。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy import select, text, update

from dra import runtime_config
from dra.api.services import trial as trial_service
from dra.api.services.db_errors import database_errors
from dra.api.services.errors import Conflict, Forbidden, InvalidRequest, Unavailable
from dra.db import Database
from dra.db_tables import DailyCounter, PlatformGovernance, QuotaReservation, User, UserQuotaPolicy

RUN, BRIEF = "run", "brief"


@dataclass(frozen=True)
class Access:
    user_id: int
    role: str
    day: str
    resets_at: datetime
    timezone: str
    run_limit: int | None
    brief_limit: int | None
    policy_version: int | None
    trial: trial_service.Trial | None


def begin_write(session) -> None:
    """SQLite的读后写使用串行写事务；PostgreSQL随后使用行锁。"""
    if session.get_bind().dialect.name == "sqlite" and not session.in_transaction():
        session.execute(text("BEGIN IMMEDIATE"))


def access_in_session(session, user_id: int, *, now=None, lock: bool = True, allow_disabled: bool = False, allow_closed_trial: bool = False) -> Access:
    from dra.api.services.quota_timezone import effective_timezone_in_session

    now = now or trial_service.utcnow()
    # 治理先于用户：管理写入和扣次遵循相同锁序。
    timezone = effective_timezone_in_session(session, now=now)
    stmt = select(User).where(User.id == user_id).execution_options(populate_existing=True)
    if lock:
        stmt = stmt.with_for_update()
    user = session.scalar(stmt)
    if user is None or (user.status != "active" and not allow_disabled):
        raise Forbidden("账号不存在或已停用，请重新登录")
    day, resets_at = trial_service._today(timezone, now)
    trial = None
    run_limit = brief_limit = version = None
    if user.role == "guest":
        settings = runtime_config.current().trial
        if not allow_closed_trial and (settings.model is None or settings.provider is None):
            raise Unavailable(trial_service.NOT_OPEN)
        trial = trial_service.Trial(user_id, settings, day, resets_at)
        run_limit, brief_limit = settings.per_user_daily_runs, settings.per_user_daily_briefs
    elif user.role == "user":
        policy = session.scalar(select(UserQuotaPolicy).where(UserQuotaPolicy.user_id == user_id)
                                .with_for_update().execution_options(populate_existing=True))
        if policy is None:
            raise Unavailable("个人额度配置缺失，请联系平台管理员")
        version = policy.version
        if policy.mode == "limited":
            run_limit, brief_limit = policy.daily_runs_limit, policy.daily_briefs_limit
            if run_limit is None or brief_limit is None:
                raise Unavailable("个人额度配置无效，请联系平台管理员")
        elif policy.mode != "unlimited":
            raise Unavailable("个人额度配置无效，请联系平台管理员")
    elif user.role not in {"admin", "owner"}:
        raise Forbidden("账号身份无效，请联系平台管理员")
    return Access(user_id, user.role, day, resets_at, timezone, run_limit, brief_limit, version, trial)


def _count(session, scope: str, day: str, kind: str) -> int:
    return session.scalar(select(DailyCounter.count).where(
        DailyCounter.scope == scope, DailyCounter.day == day, DailyCounter.kind == kind)) or 0


def _take(session, scope: str, day: str, kind: str, limit: int | None) -> bool:
    if limit is not None and limit <= 0:
        return False
    if session.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    stmt = insert(DailyCounter).values(scope=scope, day=day, kind=kind, count=1)
    stmt = stmt.on_conflict_do_update(
        index_elements=[DailyCounter.scope, DailyCounter.day, DailyCounter.kind],
        set_={"count": DailyCounter.count + 1},
        where=DailyCounter.count < limit if limit is not None else None,
    ).returning(DailyCounter.count)
    return session.execute(stmt).first() is not None


def _exhausted(access: Access, kind: str, *, personal=True) -> str:
    if access.trial is not None:
        return trial_service._exhausted(access.trial, kind, personal=personal)
    what = "研究" if kind == RUN else "规划"
    when = access.resets_at.strftime("%m 月 %d 日 %H:%M")
    return f"你今天的免费{what}次数用完了，{when}恢复。"


def reserve_in_session(session, user_id: int, kind: str, reservation_id: str, *,
                       now=None, validate: Callable[[Access], None] | None = None) -> Access:
    if kind not in {RUN, BRIEF}:
        raise InvalidRequest("未知的额度类型")
    begin_write(session)
    access = access_in_session(session, user_id, now=now)
    previous = session.get(QuotaReservation, reservation_id)
    if previous is not None:
        # 不重新调用模型；网络重试可以查看原运行，而不是再次消费。
        raise Conflict("该请求已经提交，请勿重复发起；查看原运行或发起新的规划")
    if validate is not None:
        validate(access)
    user_scope = f"user:{user_id}"
    if kind == BRIEF and access.trial is not None:
        if access.run_limit is not None and _count(session, user_scope, access.day, RUN) >= access.run_limit:
            raise trial_service.QuotaExceeded(_exhausted(access, RUN))
        if access.trial is not None and _count(session, "guests", access.day, RUN) >= access.trial.settings.global_daily_runs:
            raise trial_service.QuotaExceeded(_exhausted(access, RUN, personal=False))
    limit = access.run_limit if kind == RUN else access.brief_limit
    if not _take(session, user_scope, access.day, kind, limit):
        raise trial_service.QuotaExceeded(_exhausted(access, kind))
    scopes = [user_scope]
    if access.trial is not None:
        global_limit = access.trial.limit(kind, "guests")
        if not _take(session, "guests", access.day, kind, global_limit):
            raise trial_service.QuotaExceeded(_exhausted(access, kind, personal=False))
        scopes.append("guests")
    session.add(QuotaReservation(id=reservation_id, user_id=user_id, kind=kind,
                                quota_day=access.day, scopes_json=json.dumps(scopes),
                                policy_version=access.policy_version, state="reserved"))
    session.flush()
    return access


def take_brief(db: Database, user_id: int, request_id: str, *, validate=None) -> Access:
    with database_errors(), db.session() as session:
        access = reserve_in_session(session, user_id, BRIEF, f"brief:{user_id}:{request_id}", validate=validate)
        session.get(QuotaReservation, f"brief:{user_id}:{request_id}").state = "started"
        return access


def refund_in_session(session, reservation_id: str) -> bool:
    """只退回未启动的占位；调用方事务内完成，重复补偿无效。"""
    reservation = session.scalar(select(QuotaReservation).where(QuotaReservation.id == reservation_id).with_for_update())
    if reservation is None or reservation.state != "reserved":
        return False
    for scope in json.loads(reservation.scopes_json):
        session.execute(update(DailyCounter).where(
            DailyCounter.scope == scope, DailyCounter.day == reservation.quota_day,
            DailyCounter.kind == reservation.kind, DailyCounter.count > 0,
        ).values(count=DailyCounter.count - 1).execution_options(synchronize_session=False))
    reservation.state = "refunded"
    return True


def mark_started(db: Database, run_id: str) -> None:
    with database_errors(), db.session() as session:
        begin_write(session)
        session.execute(update(QuotaReservation).where(
            QuotaReservation.id == f"run:{run_id}", QuotaReservation.state == "reserved",
        ).values(state="started"))


def status(db: Database, user, *, allow_disabled: bool = False) -> dict:
    """本人及授权用户管理使用的额度摘要，不含其他身份或研究数据。"""
    with database_errors(), db.session() as session:
        begin_write(session)
        access = access_in_session(session, user.id, allow_disabled=allow_disabled, allow_closed_trial=True)
        opened = not (access.trial and (access.trial.settings.model is None or access.trial.settings.provider is None))
        run_used = _count(session, f"user:{user.id}", access.day, RUN)
        brief_used = _count(session, f"user:{user.id}", access.day, BRIEF)
        run_left = None if access.run_limit is None else max(0, access.run_limit - run_used)
        brief_left = None if access.brief_limit is None else max(0, access.brief_limit - brief_used)
        exhausted = False
        if access.trial:
            global_run_left = max(0, access.trial.settings.global_daily_runs - _count(session, "guests", access.day, RUN))
            global_brief_left = max(0, access.trial.settings.global_daily_briefs - _count(session, "guests", access.day, BRIEF))
            run_left = min(run_left, global_run_left)
            brief_left = min(brief_left, global_brief_left)
            exhausted = global_run_left == 0
        governance = session.get(PlatformGovernance, 1)
        result = {"source": "platform" if access.role == "guest" else "personal" if access.role == "user" else "role",
                "mode": "unlimited" if access.run_limit is None else "limited", "open": bool(opened),
                "runs_used": run_used, "runs_limit": access.run_limit, "runs_left": run_left,
                "briefs_used": brief_used, "briefs_limit": access.brief_limit, "briefs_left": brief_left,
                "global_exhausted": exhausted, "timezone": access.timezone,
                "resets_at": access.resets_at.isoformat(),
                "tracking_started_at": governance.tracking_started_at.isoformat() if governance else None}

        from dra.api.services.user_management import json_count
        for key in ("runs_used", "runs_limit", "runs_left", "briefs_used", "briefs_limit", "briefs_left"):
            result[key] = json_count(result[key])
        return result
