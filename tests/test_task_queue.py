"""阶段 3-1（#84、#40）：任务队列状态机、迁移 0007 存量回填、追问并发去重与重试配额回归测试。"""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from dra import runtime_config, web
from dra.api.deps import SESSION_COOKIE
from dra.api.services import auth, run_records, task_queue, trial
from dra.api.services import runs as run_service
from dra.api.services.auth import ExternalProfile
from dra.api.services.errors import CapacityFull, Conflict, NotFound
from dra.db import Database, alembic_config, head_revision
from dra.db_tables import (
    Base,
    DailyCounter,
    QuotaReservation,
    ResearchTask,
    RunAttempt,
    RunMessage,
    Showcase,
    UserQuotaPolicy,
)
from dra.runstore import RunStore
from dra.simple_research import SimpleConfig

pytestmark = pytest.mark.real_auth
PASSWORD = "correct horse battery"


def _set_trial(**fields):
    settings = runtime_config._code_defaults()
    trial_fields = {"provider": "deepseek", "model": "deepseek-chat", **fields}
    runtime_config.save(settings.model_copy(update={"trial": runtime_config.TrialSettings(**trial_fields)}))


def _create_member(db: Database, email: str = "alice@example.com", *, role: str = "user", daily_runs: int | None = None):
    user = auth.create_user(db, email, PASSWORD, role=role)
    if role == "user" and daily_runs is not None:
        with db.session() as session:
            policy = session.get(UserQuotaPolicy, user.id)
            policy.mode = "limited"
            policy.daily_runs_limit = daily_runs
            policy.daily_briefs_limit = 10
            policy.version += 1
    token = auth.login(db, email, PASSWORD).token
    return user, token


def _create_guest(db: Database, login: str = "octo"):
    res = auth.login_external(db, ExternalProfile(provider="github", subject=login, login=login))
    return res.user, res.token


def _counter_sum(db: Database, scope: str, kind: str = "run") -> int:
    with db.session() as session:
        return sum(
            session.scalars(
                select(DailyCounter.count).where(DailyCounter.scope == scope, DailyCounter.kind == kind)
            )
        )


# ---- 1. 迁移 0007 存量回填与模型一致性 -------------------------------------------------

def test_migration_0007_backfills_preexisting_rows_and_matches_models(tmp_path):
    db = Database.from_url(f"sqlite:///{tmp_path / 'legacy0006.sqlite'}", allow_sqlite=True)
    try:
        with db.engine.begin() as conn:
            command.upgrade(alembic_config(conn), "0006")

            t1 = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
            t2 = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
            t3 = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
            conn.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, role, status, management_version, created_at, updated_at) "
                    "VALUES (1, 'admin@example.com', 'h', 'admin', 'active', 0, :t1, :t1)"
                ),
                {"t1": t1},
            )
            conn.execute(
                text(
                    "INSERT INTO conversations (id, user_id, created_at) VALUES "
                    "('conv-a', 1, :t1), ('conv-b', 1, :t3)"
                ),
                {"t1": t1, "t3": t3},
            )
            conn.execute(
                text(
                    "INSERT INTO research_tasks (id, user_id, conversation_id, query, created_at) VALUES "
                    "(10, 1, 'conv-a', '首问A', :t1), "
                    "(11, 1, 'conv-a', '追问A2', :t2), "
                    "(12, 1, 'conv-b', '首问B', :t3)"
                ),
                {"t1": t1, "t2": t2, "t3": t3},
            )
            conn.execute(
                text(
                    "INSERT INTO run_attempts (id, task_id, run_id, quota_day, created_at) VALUES "
                    "(100, 10, 'run-a1', NULL, :t1), "
                    "(101, 11, 'run-a2', NULL, :t2), "
                    "(102, 12, 'run-b1', '2026-11-11', :t3)"
                ),
                {"t1": t1, "t2": t2, "t3": t3},
            )

        assert db.upgrade() == head_revision() == "0007"

        with db.engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            assert compare_metadata(ctx, Base.metadata) == []

        with db.session() as session:
            tasks = list(session.scalars(select(ResearchTask).order_by(ResearchTask.id)))
            assert [(t.id, t.conversation_id, t.turn_index, t.status, t.current_run_id) for t in tasks] == [
                (10, "conv-a", 1, "succeeded", "run-a1"),
                (11, "conv-a", 2, "succeeded", "run-a2"),
                (12, "conv-b", 1, "succeeded", "run-b1"),
            ]
            attempts = list(session.scalars(select(RunAttempt).order_by(RunAttempt.id)))
            assert [(a.run_id, a.attempt_no, a.status, a.input_state, a.last_event_seq) for a in attempts] == [
                ("run-a1", 1, "succeeded", "closed", -1),
                ("run-a2", 1, "succeeded", "closed", -1),
                ("run-b1", 1, "succeeded", "closed", -1),
            ]
    finally:
        db.dispose()


# ---- 2. 首问提交幂等键与指纹防篡改 -----------------------------------------------------

def test_research_endpoint_idempotency_key_deduplicates_and_rejects_tampering(tmp_path, monkeypatch, database):
    store = RunStore(tmp_path / "runs")
    registry = run_service.RunRegistry(store, max_active_runs=5)
    monkeypatch.setattr(web.app.state, "store", store)
    monkeypatch.setattr(web.app.state, "registry", registry)
    _set_trial()

    started_runs = []

    def fake_start(query, **kwargs):
        started_runs.append(kwargs["run_id"])
        return kwargs["run_id"]

    monkeypatch.setattr(registry, "start_simple_research", fake_start)

    alice, alice_token = _create_member(database, "alice@example.com", daily_runs=2)
    bob, bob_token = _create_member(database, "bob@example.com", daily_runs=2)
    alice_client = TestClient(web.app, cookies={SESSION_COOKIE: alice_token})
    bob_client = TestClient(web.app, cookies={SESSION_COOKIE: bob_token})

    body = {"query": "PostgreSQL SKIP LOCKED 原理", "mode": "simple", "idempotency_key": "idem-key-01"}
    r1 = alice_client.post("/api/research", json=body)
    r2 = alice_client.post("/api/research", json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["run_id"] == r2.json()["run_id"]
    assert len(started_runs) == 1
    assert _counter_sum(database, f"user:{alice.id}") == 1

    # 同一用户修改查询内容却复用同一 idempotency_key：返回 409，不扣第 2 次额度。
    tampered = alice_client.post("/api/research", json={**body, "query": "改成别的问题"})
    assert tampered.status_code == 409
    assert _counter_sum(database, f"user:{alice.id}") == 1

    # 不同用户使用相同字面值的 idempotency_key 互不冲突。
    r_bob = bob_client.post("/api/research", json=body)
    assert r_bob.status_code == 200
    assert r_bob.json()["run_id"] != r1.json()["run_id"]
    assert len(started_runs) == 2


# ---- 3. #40 追问并发去重（相同 request_id 只跑一次；同父不同追问只成一单） -------------

def test_overlapping_followup_same_request_id_starts_only_once(tmp_path, monkeypatch, database):
    store = RunStore(tmp_path / "runs")
    registry = run_service.RunRegistry(store, max_active_runs=4)
    monkeypatch.setattr(runtime_config, "current", runtime_config._code_defaults)
    monkeypatch.setattr(runtime_config, "to_subagent_config", lambda _: SimpleConfig().adapter)

    user, _ = _create_member(database, "followup@example.com", daily_runs=5)
    parent = "20261128-010000-parent01"
    run_records.reserve_new_run(database, user.id, parent, "首问")
    task_queue.complete_attempt(database, parent, outcome="succeeded", delivery_status="done")
    store.create_run("首问", None, {"mode": "simple"}, run_id=parent)
    store.save_simple_result(parent, {"draft": "结论", "cards": [], "sources": [], "calls": [], "requirements": []})
    store.finalize(parent, "done", {})

    started_ids = []
    first_committed = threading.Event()
    allow_return = threading.Event()
    real_reserve_followup = run_records.reserve_followup

    def paused_after_commit(*args, **kwargs):
        res = real_reserve_followup(*args, **kwargs)
        if res.created:
            # 精确停在“占位事务已提交、但尚未进入 start_followup 写 meta.json”的竞争窗口（#40）。
            first_committed.set()
            assert allow_return.wait(5)
        return res

    def fake_start_followup(*args, **kwargs):
        rid = kwargs["reserved_run_id"]
        started_ids.append(rid)
        store.create_run(
            "同一追问",
            None,
            {"mode": "simple"},
            run_id=rid,
            identity={
                "conversation_id": parent,
                "parent_run_id": parent,
                "turn_index": 2,
                "request_id": "req-dup",
                "followup_request": {"parent_run_id": parent, "text": "同一追问"},
            },
        )
        return {
            "run_id": rid,
            "conversation_id": parent,
            "turn_index": 2,
            "status": "running",
            "parent_run_id": parent,
        }

    monkeypatch.setattr(run_records, "reserve_followup", paused_after_commit)
    monkeypatch.setattr(run_service, "start_followup", fake_start_followup)

    async def scenario():
        t1 = asyncio.create_task(
            run_service.start_owned_followup(database, store, registry, user.id, parent, "req-dup", "同一追问")
        )
        assert await asyncio.to_thread(first_committed.wait, 5)
        t2 = asyncio.create_task(
            run_service.start_owned_followup(database, store, registry, user.id, parent, "req-dup", "同一追问")
        )
        res2 = await t2
        allow_return.set()
        res1 = await t1
        return res1, res2

    res1, res2 = asyncio.run(scenario())
    assert res1["run_id"] == res2["run_id"]
    assert len(started_ids) == 1
    assert _counter_sum(database, f"user:{user.id}") == 2  # 首问 1 次 + 追问仅扣 1 次
    with database.session() as session:
        turns = list(
            session.scalars(
                select(ResearchTask).where(ResearchTask.conversation_id == parent).order_by(ResearchTask.turn_index)
            )
        )
        assert [(t.turn_index, t.query) for t in turns] == [(1, "首问"), (2, "同一追问")]


def test_overlapping_distinct_followups_on_same_parent_allow_exactly_one(tmp_path, monkeypatch, database):
    store = RunStore(tmp_path / "runs")
    registry = run_service.RunRegistry(store, max_active_runs=4)
    user, _ = _create_member(database, "race@example.com", daily_runs=5)
    parent = "20261128-010000-parent02"
    run_records.reserve_new_run(database, user.id, parent, "首问")
    task_queue.complete_attempt(database, parent, outcome="succeeded", delivery_status="done")
    store.create_run("首问", None, {"mode": "simple"}, run_id=parent)
    store.finalize(parent, "done", {})

    first_committed = threading.Event()
    allow_return = threading.Event()
    started = []
    real_reserve_followup = run_records.reserve_followup

    def paused_after_commit(*args, **kwargs):
        res = real_reserve_followup(*args, **kwargs)
        if not first_committed.is_set():
            first_committed.set()
            assert allow_return.wait(5)
        return res

    def fake_start_followup(*args, **kwargs):
        rid = kwargs["reserved_run_id"]
        started.append(rid)
        store.create_run("追问", None, {"mode": "simple"}, run_id=rid)
        return {"run_id": rid, "conversation_id": parent, "turn_index": 2, "status": "running", "parent_run_id": parent}

    monkeypatch.setattr(run_records, "reserve_followup", paused_after_commit)
    monkeypatch.setattr(run_service, "start_followup", fake_start_followup)

    async def scenario():
        t1 = asyncio.create_task(
            run_service.start_owned_followup(database, store, registry, user.id, parent, "req-a", "追问A")
        )
        assert await asyncio.to_thread(first_committed.wait, 5)
        t2 = asyncio.create_task(
            run_service.start_owned_followup(database, store, registry, user.id, parent, "req-b", "追问B")
        )
        outcomes_t2 = await asyncio.gather(t2, return_exceptions=True)
        allow_return.set()
        res1 = await t1
        return [res1, *outcomes_t2]

    outcomes = asyncio.run(scenario())
    successes = [o for o in outcomes if isinstance(o, dict)]
    conflicts = [o for o in outcomes if isinstance(o, Conflict)]
    assert len(successes) == 1 and len(conflicts) == 1
    assert "新一轮" in str(conflicts[0])
    assert len(started) == 1
    assert _counter_sum(database, f"user:{user.id}") == 2  # 被拒的那条未多扣次数


# ---- 4. 访客单任务排队限制、Worker 抢单与访客并发槽位保护 -------------------------------

def test_guest_queue_cap_and_worker_claim_skips_guests_when_guest_slots_full(database):
    _set_trial(per_user_daily_runs=3, global_daily_runs=10, max_concurrent_guest_runs=1)
    guest1, _ = _create_guest(database, "guest-one")
    guest2, _ = _create_guest(database, "guest-two")
    member, _ = _create_member(database, "member@example.com")

    g1 = task_queue.enqueue_new_task(database, guest1.id, "20261128-020001-00000001", "访客1首问")
    assert g1.status == "queued"

    # 同一访客已有 queued 任务时不能再排第 2 个，且不扣第 2 次额度。
    with pytest.raises(CapacityFull, match="已经有一个研究在排队或进行中"):
        task_queue.enqueue_new_task(database, guest1.id, "20261128-020002-00000002", "访客1第二问")
    assert _counter_sum(database, f"user:{guest1.id}") == 1

    # 另一位访客与正式用户各自入队。
    g2 = task_queue.enqueue_new_task(database, guest2.id, "20261128-020003-00000003", "访客2首问")
    m1 = task_queue.enqueue_new_task(database, member.id, "20261128-020004-00000004", "正式用户首问")

    # Worker 1 领走最早的 g1，同时把配额台账置为 started。
    c1 = task_queue.claim_next_task(database, "worker-1", max_concurrent_guest_runs=1)
    assert c1 is not None and c1.run_id == g1.run_id
    with database.session() as session:
        assert session.get(QuotaReservation, f"run:{g1.run_id}").state == "started"

    # 此时活跃访客已满 1 个：Worker 2 跳过排在前面的 g2，直接领走正式用户的 m1（无队头阻塞）。
    c2 = task_queue.claim_next_task(database, "worker-2", max_concurrent_guest_runs=1)
    assert c2 is not None and c2.run_id == m1.run_id

    # Worker 3 再来领：只剩访客 g2，但访客并发槽仍满，返回 None。
    assert task_queue.claim_next_task(database, "worker-3", max_concurrent_guest_runs=1) is None

    # g1 结束后，访客槽位释放，g2 即可被领取。
    task_queue.complete_attempt(database, g1.run_id, outcome="succeeded", delivery_status="done")
    c3 = task_queue.claim_next_task(database, "worker-1", max_concurrent_guest_runs=1)
    assert c3 is not None and c3.run_id == g2.run_id


def test_concurrent_workers_claim_each_queued_task_exactly_once(database):
    member, _ = _create_member(database, "batch@example.com")
    expected = []
    for idx in range(6):
        rid = f"20261128-03000{idx}-0000000{idx}"
        task_queue.enqueue_new_task(database, member.id, rid, f"问题 {idx}")
        expected.append(rid)

    barrier = threading.Barrier(4)
    claimed_by_threads: list[str] = []
    lock = threading.Lock()

    def worker_loop(wid: str):
        barrier.wait()
        while True:
            claimed = task_queue.claim_next_task(database, wid, max_concurrent_guest_runs=2)
            if claimed is None:
                break
            with lock:
                claimed_by_threads.append(claimed.run_id)

    threads = [threading.Thread(target=worker_loop, args=(f"w-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(claimed_by_threads) == 6
    assert sorted(claimed_by_threads) == sorted(expected)
    assert task_queue.claim_next_task(database, "w-after") is None


# ---- 5. 取消两阶段、配额退回与完成/取消竞争（CAS） --------------------------------------

def test_cancel_queued_refunds_quota_while_cancel_running_waits_for_worker(database):
    _set_trial(per_user_daily_runs=1, global_daily_runs=5)
    guest, _ = _create_guest(database, "canceller")

    rid1 = "20261128-040001-00000001"
    enq = task_queue.enqueue_new_task(database, guest.id, rid1, "排队即取消")
    with database.session() as session:
        session.add(RunMessage(attempt_id=enq.attempt_id, message_id="m1", revision=1, text="补充", status="queued"))
    assert _counter_sum(database, f"user:{guest.id}") == 1
    assert _counter_sum(database, "guests") == 1

    # 排队期间取消：直接 cancelled 并退回个人与全站访客次数，随后还能再次提交。
    cancelled_q = task_queue.cancel_task(database, rid1)
    assert (cancelled_q.status, cancelled_q.refunded) == ("cancelled", True)
    assert _counter_sum(database, f"user:{guest.id}") == 0
    assert _counter_sum(database, "guests") == 0
    with database.session() as session:
        msg = session.scalar(select(RunMessage).where(RunMessage.attempt_id == enq.attempt_id))
        assert msg.status == "undelivered"

    # 重新提交并由 Worker 领走进入 running 后再点取消：变 cancelling、不退次数。
    rid2 = "20261128-040002-00000002"
    task_queue.enqueue_new_task(database, guest.id, rid2, "运行中取消")
    task_queue.claim_next_task(database, "worker-1", max_concurrent_guest_runs=2)

    cancelling = task_queue.cancel_task(database, rid2)
    assert (cancelling.status, cancelling.refunded) == ("cancelling", False)
    assert _counter_sum(database, f"user:{guest.id}") == 1

    # Worker 确认退出后结案为 cancelled。
    assert task_queue.complete_attempt(database, rid2, outcome="cancelled", delivery_status="cancelled") == "cancelled"
    assert task_queue.cancel_task(database, rid2).status == "cancelled"


def test_cancelling_vs_succeeded_cas_keeps_completed_report(database):
    member, _ = _create_member(database, "cas@example.com")
    rid = "20261128-040009-00000009"
    task_queue.enqueue_new_task(database, member.id, rid, "刚交稿就点取消")
    task_queue.claim_next_task(database, "worker-1")

    # API 先一步写入 cancelling，紧接着 Worker 已完成报告提交 succeeded：结果保留 succeeded。
    assert task_queue.cancel_task(database, rid).status == "cancelling"
    final = task_queue.complete_attempt(database, rid, outcome="succeeded", delivery_status="done")
    assert final == "succeeded"
    with database.session() as session:
        task = session.scalar(select(ResearchTask).where(ResearchTask.current_run_id == rid))
        attempt = session.scalar(select(RunAttempt).where(RunAttempt.run_id == rid))
        assert task.status == "succeeded" and attempt.status == "succeeded" and attempt.delivery_status == "done"

    # 已进入 succeeded 后迟到的取消请求不会把状态改回 cancelling/cancelled。
    late_cancel = task_queue.cancel_task(database, rid)
    assert late_cancel.status == "succeeded" and late_cancel.refunded is False


# ---- 6. 心跳刷新、超时中断回收（interrupted）与防僵尸覆盖 -------------------------------

def test_heartbeat_and_reap_interrupted_blocks_stale_worker_overwrite(database):
    member, _ = _create_member(database, "hb@example.com")
    rid = "20261128-050001-00000001"
    t0 = datetime(2026, 11, 28, 5, 0, 0, tzinfo=UTC)

    enq = task_queue.enqueue_new_task(database, member.id, rid, "长耗时问题", now=t0)
    task_queue.claim_next_task(database, "worker-a", now=t0)
    with database.session() as session:
        session.add(RunMessage(attempt_id=enq.attempt_id, message_id="m1", revision=1, text="插话", status="queued"))

    # 30 秒时正常心跳，50 秒时巡检（阈值 60s）不应误杀。
    hb1 = task_queue.heartbeat(database, rid, "worker-a", now=t0 + timedelta(seconds=30))
    assert hb1.alive is True and hb1.status == "running"
    assert task_queue.reap_interrupted(database, timeout_s=60.0, now=t0 + timedelta(seconds=80)) == []

    # 距上次心跳超过 60 秒（t0 + 95s）：巡检将其标为 interrupted，关闭未送达消息。
    reaped = task_queue.reap_interrupted(database, timeout_s=60.0, now=t0 + timedelta(seconds=95))
    assert reaped == [rid]

    # 失联的僵尸 Worker 醒来尝试发心跳或提交完成，均被拒绝且不覆盖 interrupted。
    hb_dead = task_queue.heartbeat(database, rid, "worker-a", now=t0 + timedelta(seconds=96))
    assert hb_dead.alive is False and hb_dead.status == "interrupted"
    assert task_queue.complete_attempt(database, rid, outcome="succeeded", delivery_status="done") == "interrupted"

    with database.session() as session:
        msg = session.scalar(select(RunMessage).where(RunMessage.attempt_id == enq.attempt_id))
        assert msg.status == "undelivered"


# ---- 7. 手动重新执行（retry_task）：首次中断免费、二次中断与 failed 扣费及安全边界 ---------

def test_retry_interrupted_first_is_free_even_at_quota_limit_second_is_charged(database):
    _set_trial(per_user_daily_runs=1, global_daily_runs=5)
    guest, _ = _create_guest(database, "unlucky-guest")
    snap = json.dumps({"resolved_config": {"source_mode": "web", "library_selection": {}, "mcp_servers": []}})
    t0 = datetime(2026, 11, 28, 6, 0, 0, tzinfo=UTC)

    rid1 = "20261128-060001-00000001"
    task_queue.enqueue_new_task(database, guest.id, rid1, "访客唯一一次机会", config_snapshot=snap, now=t0)
    task_queue.claim_next_task(database, "worker-1", now=t0)
    assert task_queue.reap_interrupted(database, timeout_s=60.0, now=t0 + timedelta(seconds=61)) == [rid1]
    assert _counter_sum(database, f"user:{guest.id}") == 1  # 今日 1/1 已用满

    # 第 1 次针对 interrupted 重新执行：免费补偿，即使 1/1 已满也能入队！
    rid2 = "20261128-060002-00000002"
    retry1 = task_queue.retry_task(database, guest.id, rid1, rid2, idempotency_key="retry-k1", now=t0 + timedelta(seconds=65))
    assert (retry1.run_id, retry1.attempt_no, retry1.quota_charged, retry1.created) == (rid2, 2, False, True)
    assert _counter_sum(database, f"user:{guest.id}") == 1

    # 网络重发同一 idempotency_key：幂等返回 rid2，不报错。
    retry1_dup = task_queue.retry_task(database, guest.id, rid1, "20261128-060099-unused00", idempotency_key="retry-k1")
    assert (retry1_dup.run_id, retry1_dup.attempt_no, retry1_dup.created) == (rid2, 2, False)

    # 换新 key 再次对旧尝试 rid1 重试：409 拒绝。
    with pytest.raises(Conflict, match="新的重试记录"):
        task_queue.retry_task(database, guest.id, rid1, "20261128-060098-unused01", idempotency_key="retry-k2")

    # 假如 rid2 再次被领走并又一次 interrupted：第 2 次重试不再免费，因额度已满抛 429。
    task_queue.claim_next_task(database, "worker-2", now=t0 + timedelta(seconds=70))
    assert task_queue.reap_interrupted(database, timeout_s=60.0, now=t0 + timedelta(seconds=140)) == [rid2]
    with pytest.raises(trial.QuotaExceeded):
        task_queue.retry_task(database, guest.id, rid2, "20261128-060003-00000003", idempotency_key="retry-k3")


def test_retry_failed_charges_quota_and_respects_boundaries(database):
    _set_trial()
    member, _ = _create_member(database, "retryer@example.com", daily_runs=2)
    other, _ = _create_member(database, "other@example.com", daily_runs=2)
    web_snap = json.dumps({"resolved_config": {"source_mode": "web", "library_selection": {}, "mcp_servers": []}})

    rid1 = "20261128-070001-00000001"
    task_queue.enqueue_new_task(database, member.id, rid1, "执行失败题", config_snapshot=web_snap)
    task_queue.claim_next_task(database, "worker-1")
    task_queue.complete_attempt(database, rid1, outcome="failed", delivery_status="failed", error="upstream 500")
    assert _counter_sum(database, f"user:{member.id}") == 1

    # 非主人重试 -> 404
    with pytest.raises(NotFound):
        task_queue.retry_task(database, other.id, rid1, "20261128-070099-00000099", idempotency_key="k")

    # 本人对 failed 重试 -> 扣第 2 次额度；若在 queued 期间取消，则第 2 次额度可退回。
    rid2 = "20261128-070002-00000002"
    retried = task_queue.retry_task(database, member.id, rid1, rid2, idempotency_key="k-fail-1")
    assert retried.quota_charged is True and retried.attempt_no == 2
    assert _counter_sum(database, f"user:{member.id}") == 2

    cancelled = task_queue.cancel_task(database, rid2)
    assert cancelled.refunded is True
    assert _counter_sum(database, f"user:{member.id}") == 1

    # 调高额度以继续验证结构性边界（缺快照、非最新轮、降级为访客）。
    with database.session() as session:
        session.get(UserQuotaPolicy, member.id).daily_runs_limit = 10

    legacy_id = "20261128-070010-00000010"
    task_queue.enqueue_new_task(database, member.id, legacy_id, "无快照旧记录", config_snapshot=None)
    task_queue.claim_next_task(database, "worker-1")
    task_queue.complete_attempt(database, legacy_id, outcome="failed", delivery_status="failed")
    with pytest.raises(Conflict, match="缺少执行快照"):
        task_queue.retry_task(database, member.id, legacy_id, "20261128-070011-00000011", idempotency_key="k-leg")

    # 对话已有第 2 轮时，不允许重跑第 1 轮。
    conv_t1 = "20261128-070020-00000020"
    conv_t2 = "20261128-070021-00000021"
    task_queue.enqueue_new_task(database, member.id, conv_t1, "第1轮", config_snapshot=web_snap)
    task_queue.claim_next_task(database, "worker-1")
    task_queue.complete_attempt(database, conv_t1, outcome="failed", delivery_status="failed")
    task_queue.enqueue_followup_task(database, member.id, conv_t1, conv_t2, "第2轮", request_id="f2", config_snapshot=web_snap)
    with pytest.raises(Conflict, match="最新一轮"):
        task_queue.retry_task(database, member.id, conv_t1, "20261128-070022-00000022", idempotency_key="k-mid")

    # 降级为 guest 后不能通过 retry 恢复私域资料研究。
    priv_id = "20261128-070030-00000030"
    priv_snap = json.dumps({"resolved_config": {"source_mode": "library", "library_selection": {"docs": ["d1"]}}})
    task_queue.enqueue_new_task(database, member.id, priv_id, "私域研究", config_snapshot=priv_snap)
    task_queue.claim_next_task(database, "worker-1")
    task_queue.complete_attempt(database, priv_id, outcome="failed", delivery_status="failed")
    auth.set_role(database, "guest", email=member.email)
    with pytest.raises(Conflict, match="访客不能重新执行"):
        task_queue.retry_task(database, member.id, priv_id, "20261128-070031-00000031", idempotency_key="k-priv")

    # 公开示例对话冻结期间拒绝重试。
    admin_user, _ = _create_member(database, "admin-show@example.com", role="admin")
    show_id = "20261128-070040-00000040"
    task_queue.enqueue_new_task(database, admin_user.id, show_id, "示例对话", config_snapshot=web_snap)
    task_queue.claim_next_task(database, "worker-1")
    task_queue.complete_attempt(database, show_id, outcome="failed", delivery_status="failed")
    with database.session() as session:
        session.add(Showcase(conversation_id=show_id, title="示例"))
    with pytest.raises(Conflict, match="示例展示"):
        task_queue.retry_task(database, admin_user.id, show_id, "20261128-070041-00000041", idempotency_key="k-show")
