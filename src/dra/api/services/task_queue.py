"""任务队列与平台执行状态机（业务层，阶段 3 第 1 步 #84）。

设计见 docs/design/阶段3-Worker与进度推送.md §1–§4。
不依赖 FastAPI：统一抛 ``dra.api.services.errors`` 异常，由接口层翻译为 HTTP 状态码。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select, update

from dra import runtime_config
from dra.api.services import quotas
from dra.api.services import trial as trial_service
from dra.api.services.db_errors import database_errors
from dra.api.services.errors import CapacityFull, Conflict, InvalidRequest, NotFound
from dra.db import Database
from dra.db_tables import (
    ACTIVE_TASK_STATUSES,
    DELIVERY_STATUSES,
    TERMINAL_TASK_STATUSES,
    Conversation,
    PlatformGovernance,
    QuotaReservation,
    ResearchTask,
    RunAttempt,
    RunMessage,
    Showcase,
    User,
)

log = logging.getLogger(__name__)

SHOWCASE_FROZEN = "该对话正作为示例展示，取消示例后才能追问"
LEGACY_NOT_IMPORTED = (
    "这条研究还没有登记到数据库，暂时不能追问。"
    "请先执行 python -m dra.admin import-legacy --owner <管理员邮箱>"
)
GUEST_ACTIVE_LIMIT_MSG = "你已经有一个研究在排队或进行中，请等它结束后再问。"


def request_fingerprint(payload: dict) -> str:
    """将影响研究语义的请求参数规范化为稳定 SHA-256，用于检验幂等键是否被篡改复用。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EnqueueResult:
    task_id: int
    attempt_id: int
    run_id: str
    conversation_id: str
    turn_index: int
    status: str
    access: quotas.Access
    created: bool


@dataclass(frozen=True)
class FollowupEnqueueResult:
    task_id: int
    attempt_id: int
    run_id: str
    conversation_id: str
    turn_index: int
    status: str
    parent_run_id: str
    counted: set[str]
    access: quotas.Access
    created: bool


@dataclass(frozen=True)
class ClaimedTask:
    task_id: int
    attempt_id: int
    run_id: str
    user_id: int
    conversation_id: str
    turn_index: int
    query: str
    config_snapshot: str | None
    attempt_no: int
    worker_id: str
    started_at: datetime


@dataclass(frozen=True)
class CancelResult:
    run_id: str
    status: str
    refunded: bool


@dataclass(frozen=True)
class HeartbeatResult:
    run_id: str
    status: str
    input_state: str
    input_revision: int
    alive: bool


@dataclass(frozen=True)
class RetryResult:
    task_id: int
    attempt_id: int
    run_id: str
    retry_of_run_id: str
    attempt_no: int
    status: str
    quota_charged: bool
    created: bool


def _conversation_run_ids(session, conversation_id: str) -> set[str]:
    return set(
        session.scalars(
            select(RunAttempt.run_id)
            .join(ResearchTask, ResearchTask.id == RunAttempt.task_id)
            .where(ResearchTask.conversation_id == conversation_id)
        )
    )


def _ensure_guest_has_no_active_task(session, user_id: int) -> None:
    active_count = session.scalar(
        select(func.count())
        .select_from(ResearchTask)
        .where(ResearchTask.user_id == user_id, ResearchTask.status.in_(ACTIVE_TASK_STATUSES))
    )
    if active_count:
        raise CapacityFull(GUEST_ACTIVE_LIMIT_MSG)


def _close_queued_messages(session, attempt_id: int, reason: str) -> None:
    session.execute(
        update(RunMessage)
        .where(RunMessage.attempt_id == attempt_id, RunMessage.status == "queued")
        .values(status="undelivered", reason=reason)
        .execution_options(synchronize_session=False)
    )


def _now(explicit: datetime | None) -> datetime:
    return explicit if explicit is not None else trial_service.utcnow()


def enqueue_new_task(
    db: Database,
    user_id: int,
    run_id: str,
    query: str,
    *,
    idempotency_key: str | None = None,
    fingerprint: str | None = None,
    config_snapshot: str | None = None,
    check_guest_active: bool = True,
    now: datetime | None = None,
) -> EnqueueResult:
    """原子创建首问任务与首次执行尝试（状态 queued），并在同一事务内校验幂等与扣减每日额度。"""
    ts = _now(now)
    fp = fingerprint or request_fingerprint({"query": query})
    scoped_key = f"new:{idempotency_key}" if idempotency_key else None

    with database_errors(), db.session() as session:
        quotas.begin_write(session)
        access = quotas.access_in_session(session, user_id, now=now)
        if scoped_key is not None:
            existing = session.scalars(
                select(ResearchTask).where(
                    ResearchTask.user_id == user_id,
                    ResearchTask.idempotency_key == scoped_key,
                )
            ).first()
            if existing is not None:
                if existing.request_fingerprint != fp:
                    raise Conflict("同一幂等键对应的请求内容不同，请勿修改后重试")
                attempt = session.scalars(
                    select(RunAttempt).where(RunAttempt.run_id == existing.current_run_id)
                ).first()
                return EnqueueResult(
                    task_id=existing.id,
                    attempt_id=attempt.id,
                    run_id=attempt.run_id,
                    conversation_id=existing.conversation_id,
                    turn_index=existing.turn_index,
                    status=existing.status,
                    access=access,
                    created=False,
                )

        if check_guest_active and access.role == "guest":
            _ensure_guest_has_no_active_task(session, user_id)

        access = quotas.reserve_in_session(session, user_id, quotas.RUN, f"run:{run_id}", now=now)
        session.add(Conversation(id=run_id, user_id=user_id, created_at=ts))
        task = ResearchTask(
            user_id=user_id,
            conversation_id=run_id,
            turn_index=1,
            query=query,
            status="queued",
            idempotency_key=scoped_key,
            request_fingerprint=fp,
            config_snapshot=config_snapshot,
            current_run_id=run_id,
            created_at=ts,
            updated_at=ts,
        )
        session.add(task)
        session.flush()
        attempt = RunAttempt(
            task_id=task.id,
            attempt_no=1,
            run_id=run_id,
            idempotency_key=scoped_key,
            status="queued",
            input_state="open",
            quota_day=access.day,
            created_at=ts,
        )
        session.add(attempt)
        session.flush()
        return EnqueueResult(
            task_id=task.id,
            attempt_id=attempt.id,
            run_id=run_id,
            conversation_id=run_id,
            turn_index=1,
            status="queued",
            access=access,
            created=True,
        )


def enqueue_followup_task(
    db: Database,
    user_id: int,
    parent_run_id: str,
    run_id: str,
    query: str,
    *,
    request_id: str | None = None,
    fingerprint: str | None = None,
    config_snapshot: str | None = None,
    check_parent_finished: bool = False,
    check_guest_active: bool = True,
    counted_loader=None,
    now: datetime | None = None,
) -> FollowupEnqueueResult:
    """在 Conversation 行锁内原子完成追问去重、最新轮校验、额度扣减与入队（修复 #40）。"""
    ts = _now(now)
    fp = fingerprint or request_fingerprint({"parent_run_id": parent_run_id, "text": query})
    load_counted = counted_loader or _conversation_run_ids

    with database_errors(), db.session() as session:
        # 锁序固定：治理 -> 用户 -> 对话 -> 任务 -> 计数。
        quotas.begin_write(session)
        access = quotas.access_in_session(session, user_id, now=now)
        conversation_id = session.scalar(
            select(ResearchTask.conversation_id)
            .join(RunAttempt, RunAttempt.task_id == ResearchTask.id)
            .where(RunAttempt.run_id == parent_run_id)
        )
        if conversation_id is None:
            raise Conflict(LEGACY_NOT_IMPORTED)

        owner = session.scalar(
            select(Conversation.user_id)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
        if owner != user_id:
            raise NotFound("run 不存在")
        if session.get(Showcase, conversation_id) is not None:
            raise Conflict(SHOWCASE_FROZEN)

        scoped_key = f"followup:{conversation_id}:{request_id}" if request_id else None
        if scoped_key is not None:
            existing = session.scalars(
                select(ResearchTask).where(
                    ResearchTask.user_id == user_id,
                    ResearchTask.idempotency_key == scoped_key,
                )
            ).first()
            if existing is not None:
                if existing.request_fingerprint != fp:
                    raise Conflict("同一 request_id 对应不同追问，请勿修改后重试")
                attempt = session.scalars(
                    select(RunAttempt).where(RunAttempt.run_id == existing.current_run_id)
                ).first()
                counted = load_counted(session, conversation_id)
                return FollowupEnqueueResult(
                    task_id=existing.id,
                    attempt_id=attempt.id,
                    run_id=attempt.run_id,
                    conversation_id=conversation_id,
                    turn_index=existing.turn_index,
                    status=existing.status,
                    parent_run_id=parent_run_id,
                    counted=counted,
                    access=access,
                    created=False,
                )

        latest_task = session.scalars(
            select(ResearchTask)
            .where(ResearchTask.conversation_id == conversation_id)
            .order_by(ResearchTask.turn_index.desc())
            .with_for_update()
        ).first()
        if latest_task is None or latest_task.current_run_id != parent_run_id:
            raise Conflict("研究已有新一轮，请回到最新一轮继续")
        if check_parent_finished and latest_task.status in ACTIVE_TASK_STATUSES:
            raise Conflict("上一轮仍在排队或运行中，请结束后继续追问")

        if check_guest_active and access.role == "guest":
            _ensure_guest_has_no_active_task(session, user_id)

        counted = load_counted(session, conversation_id)
        access = quotas.reserve_in_session(session, user_id, quotas.RUN, f"run:{run_id}", now=now)
        next_turn = latest_task.turn_index + 1
        task = ResearchTask(
            user_id=user_id,
            conversation_id=conversation_id,
            turn_index=next_turn,
            query=query,
            status="queued",
            idempotency_key=scoped_key,
            request_fingerprint=fp,
            config_snapshot=config_snapshot,
            current_run_id=run_id,
            created_at=ts,
            updated_at=ts,
        )
        session.add(task)
        session.flush()
        attempt = RunAttempt(
            task_id=task.id,
            attempt_no=1,
            run_id=run_id,
            idempotency_key=scoped_key,
            status="queued",
            input_state="open",
            quota_day=access.day,
            created_at=ts,
        )
        session.add(attempt)
        session.flush()
        return FollowupEnqueueResult(
            task_id=task.id,
            attempt_id=attempt.id,
            run_id=run_id,
            conversation_id=conversation_id,
            turn_index=next_turn,
            status="queued",
            parent_run_id=parent_run_id,
            counted=counted,
            access=access,
            created=True,
        )


def _select_claimable_task(session, *, non_guest_only: bool = False) -> ResearchTask | None:
    is_pg = session.get_bind().dialect.name == "postgresql"
    stmt = (
        select(ResearchTask)
        .join(User, User.id == ResearchTask.user_id)
        .where(ResearchTask.status == "queued")
    )
    if non_guest_only:
        stmt = stmt.where(User.role != "guest")
    stmt = stmt.order_by(ResearchTask.created_at.asc(), ResearchTask.id.asc()).limit(1)
    if is_pg:
        stmt = stmt.with_for_update(of=ResearchTask, skip_locked=True)
    return session.scalars(stmt).first()


def claim_next_task(
    db: Database,
    worker_id: str,
    *,
    max_concurrent_guest_runs: int | None = None,
    now: datetime | None = None,
) -> ClaimedTask | None:
    """由 Worker 原子抢单领取一条 queued 任务并置为 running。

    PostgreSQL 使用 ``FOR UPDATE OF research_tasks SKIP LOCKED``；当候选为访客任务时，
    锁住 ``platform_governance`` 单例行核对全站活跃访客并发上限，若访客并发已满则跳过访客队列，
    优先领取管理员/普通用户的排队任务，消除队头阻塞与双 Worker 并发超领。
    """
    if not worker_id or not worker_id.strip():
        raise InvalidRequest("worker_id 不能为空")
    ts = _now(now)
    guest_cap = (
        runtime_config.current().trial.max_concurrent_guest_runs
        if max_concurrent_guest_runs is None
        else max_concurrent_guest_runs
    )

    with database_errors(), db.session() as session:
        quotas.begin_write(session)
        task = _select_claimable_task(session, non_guest_only=False)
        if task is None:
            return None

        owner_role = session.scalar(select(User.role).where(User.id == task.user_id))
        if owner_role == "guest":
            # 串行化访客并发槽位判定，防止两个 Worker 同一毫秒读到相同的旧计数后双双放行。
            session.scalar(
                select(PlatformGovernance.id).where(PlatformGovernance.id == 1).with_for_update()
            )
            running_guests = session.scalar(
                select(func.count())
                .select_from(ResearchTask)
                .join(User, User.id == ResearchTask.user_id)
                .where(
                    User.role == "guest",
                    ResearchTask.status.in_(("running", "cancelling")),
                )
            ) or 0
            if running_guests >= guest_cap:
                task = _select_claimable_task(session, non_guest_only=True)
                if task is None:
                    return None

        attempt = session.scalars(
            select(RunAttempt)
            .where(RunAttempt.run_id == task.current_run_id)
            .with_for_update()
        ).first()
        if attempt is None or attempt.status != "queued":
            return None

        task.status = "running"
        task.updated_at = ts
        attempt.status = "running"
        attempt.worker_id = worker_id
        attempt.started_at = ts
        attempt.heartbeat_at = ts
        attempt.input_state = "open"

        session.execute(
            update(QuotaReservation)
            .where(
                QuotaReservation.id == f"run:{attempt.run_id}",
                QuotaReservation.state == "reserved",
            )
            .values(state="started")
            .execution_options(synchronize_session=False)
        )

        return ClaimedTask(
            task_id=task.id,
            attempt_id=attempt.id,
            run_id=attempt.run_id,
            user_id=task.user_id,
            conversation_id=task.conversation_id,
            turn_index=task.turn_index,
            query=task.query,
            config_snapshot=task.config_snapshot,
            attempt_no=attempt.attempt_no,
            worker_id=worker_id,
            started_at=ts,
        )


def mark_running_in_process(db: Database, run_id: str, *, worker_id: str = "in-process", now: datetime | None = None) -> None:
    """步骤 3-1 过渡辅助：Web 进程内直接启动研究时，将数据库中的 queued 尝试推进为 running。"""
    ts = _now(now)
    with database_errors(), db.session() as session:
        quotas.begin_write(session)
        attempt = session.scalars(
            select(RunAttempt).where(RunAttempt.run_id == run_id).with_for_update()
        ).first()
        if attempt is None or attempt.status != "queued":
            return
        task = session.scalar(
            select(ResearchTask).where(ResearchTask.id == attempt.task_id).with_for_update()
        )
        attempt.status = "running"
        attempt.worker_id = worker_id
        attempt.started_at = ts
        attempt.heartbeat_at = ts
        if task is not None and task.current_run_id == run_id:
            task.status = "running"
            task.updated_at = ts
        session.execute(
            update(QuotaReservation)
            .where(QuotaReservation.id == f"run:{run_id}", QuotaReservation.state == "reserved")
            .values(state="started")
            .execution_options(synchronize_session=False)
        )


def cancel_task(db: Database, run_id: str, *, now: datetime | None = None) -> CancelResult:
    """请求取消：queued 直接变为 cancelled 并原路退回次数；running 变为 cancelling 等待引擎安全退出。"""
    ts = _now(now)
    with database_errors(), db.session() as session:
        quotas.begin_write(session)
        attempt = session.scalars(
            select(RunAttempt).where(RunAttempt.run_id == run_id).with_for_update()
        ).first()
        if attempt is None:
            raise NotFound("run 不存在")
        task = session.scalar(
            select(ResearchTask).where(ResearchTask.id == attempt.task_id).with_for_update()
        )

        if attempt.status == "queued":
            attempt.status = "cancelled"
            attempt.finished_at = ts
            attempt.input_state = "closed"
            _close_queued_messages(session, attempt.id, "排队期间已取消")
            if task is not None and task.current_run_id == run_id:
                task.status = "cancelled"
                task.cancel_requested_at = ts
                task.updated_at = ts
            refunded = quotas.refund_in_session(session, f"run:{run_id}")
            return CancelResult(run_id=run_id, status="cancelled", refunded=refunded)

        if attempt.status == "running":
            attempt.status = "cancelling"
            attempt.input_state = "closed"
            _close_queued_messages(session, attempt.id, "研究已请求取消")
            if task is not None and task.current_run_id == run_id:
                task.status = "cancelling"
                task.cancel_requested_at = ts
                task.updated_at = ts
            return CancelResult(run_id=run_id, status="cancelling", refunded=False)

        return CancelResult(run_id=run_id, status=attempt.status, refunded=False)


def complete_attempt(
    db: Database,
    run_id: str,
    *,
    outcome: str,
    delivery_status: str | None = None,
    error: str | None = None,
    now: datetime | None = None,
) -> str:
    """Worker 在研究线程退出后提交终态。

    遵循条件状态跃迁规则：
    - 若尝试已处于终态（如已被巡检标为 ``interrupted``），绝不覆盖已有终态；
    - 若尝试处于 ``running`` 或 ``cancelling``，且完整结果已落盘（``outcome == 'succeeded'``），
      即使此前刚被置为 ``cancelling`` 仍记为 ``succeeded``，不丢弃已完成报告。
    """
    if outcome not in ("succeeded", "failed", "cancelled"):
        raise InvalidRequest(f"非法的完成终态：{outcome}")
    if delivery_status is not None and delivery_status not in DELIVERY_STATUSES:
        raise InvalidRequest(f"非法的交付状态：{delivery_status}")
    ts = _now(now)

    with database_errors(), db.session() as session:
        quotas.begin_write(session)
        attempt = session.scalars(
            select(RunAttempt).where(RunAttempt.run_id == run_id).with_for_update()
        ).first()
        if attempt is None:
            raise NotFound("run 不存在")
        if attempt.status in TERMINAL_TASK_STATUSES:
            return attempt.status

        task = session.scalar(
            select(ResearchTask).where(ResearchTask.id == attempt.task_id).with_for_update()
        )
        attempt.status = outcome
        attempt.delivery_status = delivery_status
        attempt.finished_at = ts
        attempt.error = error
        attempt.input_state = "closed"
        _close_queued_messages(session, attempt.id, f"运行已结束（{outcome}）")

        session.execute(
            update(QuotaReservation)
            .where(QuotaReservation.id == f"run:{run_id}", QuotaReservation.state == "reserved")
            .values(state="started")
            .execution_options(synchronize_session=False)
        )

        if task is not None and task.current_run_id == run_id:
            task.status = outcome
            task.updated_at = ts
        return outcome


def complete_attempt_quietly(
    db: Database | None,
    run_id: str,
    *,
    simple_status: str,
    error: str | None = None,
) -> None:
    """供现役进程内收尾调用：把 simple 状态映射回数据库终态，缺库或未登记记录时不抛错阻断收尾。"""
    if db is None:
        return
    if simple_status == "cancelled":
        outcome, delivery = "cancelled", "cancelled"
    elif simple_status == "failed":
        outcome, delivery = "failed", "failed"
    elif simple_status == "partial":
        outcome, delivery = "succeeded", "partial"
    else:
        outcome, delivery = "succeeded", "done"
    try:
        complete_attempt(db, run_id, outcome=outcome, delivery_status=delivery, error=error)
    except NotFound:
        return
    except Exception:
        log.warning("同步研究 %s 数据库终态失败", run_id, exc_info=True)


def heartbeat(
    db: Database,
    run_id: str,
    worker_id: str,
    *,
    now: datetime | None = None,
) -> HeartbeatResult:
    """刷新在跑尝试的心跳并读回最新状态与补充消息修订号；若租约已丢失则返回 alive=False。"""
    ts = _now(now)
    with database_errors(), db.session() as session:
        quotas.begin_write(session)
        attempt = session.scalars(
            select(RunAttempt).where(RunAttempt.run_id == run_id).with_for_update()
        ).first()
        if attempt is None:
            raise NotFound("run 不存在")
        if attempt.status in ("running", "cancelling") and attempt.worker_id == worker_id:
            attempt.heartbeat_at = ts
            return HeartbeatResult(
                run_id=run_id,
                status=attempt.status,
                input_state=attempt.input_state,
                input_revision=attempt.input_revision,
                alive=True,
            )
        return HeartbeatResult(
            run_id=run_id,
            status=attempt.status,
            input_state=attempt.input_state,
            input_revision=attempt.input_revision,
            alive=False,
        )


def reap_interrupted(
    db: Database,
    *,
    timeout_s: float = 60.0,
    now: datetime | None = None,
) -> list[str]:
    """扫描心跳超时未更新的 running / cancelling 尝试并原子收敛为 interrupted。"""
    if timeout_s <= 0:
        raise InvalidRequest("timeout_s 必须大于 0")
    ts = _now(now)
    cutoff = ts - timedelta(seconds=timeout_s)
    reaped: list[str] = []

    with database_errors(), db.session() as session:
        quotas.begin_write(session)
        is_pg = session.get_bind().dialect.name == "postgresql"
        stmt = (
            select(RunAttempt)
            .where(
                RunAttempt.status.in_(("running", "cancelling")),
                RunAttempt.heartbeat_at.is_not(None),
                RunAttempt.heartbeat_at < cutoff,
            )
            .order_by(RunAttempt.id.asc())
        )
        if is_pg:
            stmt = stmt.with_for_update(skip_locked=True)
        stale_attempts = list(session.scalars(stmt))
        for attempt in stale_attempts:
            task = session.scalar(
                select(ResearchTask).where(ResearchTask.id == attempt.task_id).with_for_update()
            )
            attempt.status = "interrupted"
            attempt.finished_at = ts
            attempt.input_state = "closed"
            if not attempt.error:
                attempt.error = "Worker 心跳超时，研究进程已中断"
            _close_queued_messages(session, attempt.id, "研究进程意外中断，消息未送达")
            if task is not None and task.current_run_id == attempt.run_id:
                task.status = "interrupted"
                task.updated_at = ts
            reaped.append(attempt.run_id)
    return reaped


def _validate_guest_snapshot_safety(config_snapshot: str) -> None:
    try:
        data = json.loads(config_snapshot)
    except ValueError as exc:
        raise Conflict("历史执行快照损坏，无法重新执行，请直接新建研究") from exc
    resolved = data.get("resolved_config") if isinstance(data, dict) else None
    if not isinstance(resolved, dict):
        return
    if (
        resolved.get("source_mode", "web") != "web"
        or resolved.get("library_selection")
        or resolved.get("mcp_servers")
    ):
        raise Conflict("访客不能重新执行含私域资料或外部资料服务的研究，请新建网页研究")


def retry_task(
    db: Database,
    user_id: int,
    run_id: str,
    new_run_id: str,
    *,
    idempotency_key: str,
    check_guest_active: bool = True,
    now: datetime | None = None,
) -> RetryResult:
    """对对话最新一轮、最新一次处于 interrupted 或 failed 的尝试新建执行尝试并入队。

    配额规则：
    - 平台进程中断（``interrupted``）的首次重试免费（``free_interrupted_retries_used`` 0 -> 1）；
    - 连续第 2 次及以上 ``interrupted`` 重试，或 ``failed`` 重试，按新一次研究扣减当日额度。
    """
    if not idempotency_key or not idempotency_key.strip():
        raise InvalidRequest("idempotency_key 不能为空")
    ts = _now(now)
    scoped_retry_key = f"retry:{run_id}:{idempotency_key}"

    with database_errors(), db.session() as session:
        quotas.begin_write(session)
        access = quotas.access_in_session(session, user_id, now=now)

        target_attempt = session.scalars(
            select(RunAttempt).where(RunAttempt.run_id == run_id)
        ).first()
        if target_attempt is None:
            raise NotFound("run 不存在")
        task = session.get(ResearchTask, target_attempt.task_id)
        if task is None or task.user_id != user_id:
            raise NotFound("run 不存在")

        # 锁住对话与任务，串行化同一对话上的追问、设为示例与重试。
        session.scalar(
            select(Conversation.id).where(Conversation.id == task.conversation_id).with_for_update()
        )
        if session.get(Showcase, task.conversation_id) is not None:
            raise Conflict(SHOWCASE_FROZEN)

        task = session.scalar(
            select(ResearchTask).where(ResearchTask.id == task.id).with_for_update()
        )
        target_attempt = session.scalar(
            select(RunAttempt).where(RunAttempt.id == target_attempt.id).with_for_update()
        )

        existing_retry = session.scalars(
            select(RunAttempt).where(
                RunAttempt.task_id == task.id,
                RunAttempt.idempotency_key == scoped_retry_key,
            )
        ).first()
        if existing_retry is not None:
            charged = session.get(QuotaReservation, f"run:{existing_retry.run_id}") is not None
            return RetryResult(
                task_id=task.id,
                attempt_id=existing_retry.id,
                run_id=existing_retry.run_id,
                retry_of_run_id=run_id,
                attempt_no=existing_retry.attempt_no,
                status=existing_retry.status,
                quota_charged=charged,
                created=False,
            )

        latest_conv_task = session.scalars(
            select(ResearchTask)
            .where(ResearchTask.conversation_id == task.conversation_id)
            .order_by(ResearchTask.turn_index.desc())
        ).first()
        if latest_conv_task is None or latest_conv_task.id != task.id:
            raise Conflict("只能重新执行对话的最新一轮")
        if task.current_run_id != run_id:
            raise Conflict("该尝试已有新的重试记录，请刷新查看最新状态")
        if target_attempt.status not in ("interrupted", "failed"):
            raise Conflict("只有已中断或失败的研究才能重新执行")
        if task.config_snapshot is None:
            raise Conflict("该历史记录早于后台队列版本，缺少执行快照，请直接新建研究")

        if access.role == "guest":
            _validate_guest_snapshot_safety(task.config_snapshot)
            if check_guest_active:
                _ensure_guest_has_no_active_task(session, user_id)

        if target_attempt.status == "interrupted" and task.free_interrupted_retries_used < 1:
            task.free_interrupted_retries_used += 1
            quota_charged = False
            quota_day = None
        else:
            access = quotas.reserve_in_session(session, user_id, quotas.RUN, f"run:{new_run_id}", now=now)
            quota_charged = True
            quota_day = access.day

        next_attempt_no = target_attempt.attempt_no + 1
        new_attempt = RunAttempt(
            task_id=task.id,
            attempt_no=next_attempt_no,
            run_id=new_run_id,
            retry_of_run_id=run_id,
            idempotency_key=scoped_retry_key,
            status="queued",
            input_state="open",
            quota_day=quota_day,
            created_at=ts,
        )
        session.add(new_attempt)
        session.flush()

        task.current_run_id = new_run_id
        task.status = "queued"
        task.cancel_requested_at = None
        task.updated_at = ts

        return RetryResult(
            task_id=task.id,
            attempt_id=new_attempt.id,
            run_id=new_run_id,
            retry_of_run_id=run_id,
            attempt_no=next_attempt_no,
            status="queued",
            quota_charged=quota_charged,
            created=True,
        )
