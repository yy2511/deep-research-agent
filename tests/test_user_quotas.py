"""个人每日次数、访客全局池、原子台账与身份变化的离线回归。"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select
from test_trial import _brief, _research, _set_trial, world  # noqa: F401

from dra.api.services import quotas, run_records, trial
from dra.api.services.errors import Conflict, Forbidden, Unavailable
from dra.db_tables import DailyCounter, PlatformGovernance, QuotaReservation, User, UserQuotaPolicy

pytestmark = pytest.mark.real_auth


def policy(db, user, runs=2, briefs=3):
    with db.session() as session:
        row = session.get(UserQuotaPolicy, user.id)
        if row is None:
            row = UserQuotaPolicy(user_id=user.id, mode="limited")
            session.add(row)
        row.mode, row.daily_runs_limit, row.daily_briefs_limit = "limited", runs, briefs
        row.version = (row.version or 0) + 1


def count(db, scope, kind="run"):
    with db.session() as session:
        return sum(session.scalars(select(DailyCounter.count).where(DailyCounter.scope == scope, DailyCounter.kind == kind)))


def test_user_personal_limit_does_not_consume_guest_pool(world):
    client, user = world["member"]("member@example.com")
    policy(world["db"], user, 2)
    _set_trial(global_daily_runs=0)
    assert [_research(client).status_code for _ in range(3)] == [200, 200, 429]
    assert count(world["db"], f"user:{user.id}") == 2
    assert count(world["db"], "guests") == 0
    state = quotas.status(world["db"], user)
    assert state["runs_used"] == 2 and state["runs_left"] == 0
    assert state["source"] == "personal" and state["mode"] == "limited"


def test_user_zero_runs_does_not_block_independent_planning_quota(world):
    client, user = world["member"]("member@example.com")
    policy(world["db"], user, 0, 5)
    assert _research(client).status_code == 429
    assert _brief(client).status_code == 200
    assert count(world["db"], f"user:{user.id}") == 0
    assert count(world["db"], f"user:{user.id}", "brief") == 1


def test_policy_increase_and_decrease_preserve_usage(world):
    client, user = world["member"]("member@example.com")
    policy(world["db"], user, 3)
    assert _research(client).status_code == 200
    policy(world["db"], user, 0)
    assert _research(client).status_code == 429
    policy(world["db"], user, 2)
    assert _research(client).status_code == 200
    assert _research(client).status_code == 429
    assert count(world["db"], f"user:{user.id}") == 2


def test_admin_unlimited_usage_is_counted_without_trial_pool(world):
    client, user = world["member"]("admin@example.com", "admin")
    assert _research(client).status_code == 200
    assert _brief(client).status_code == 200
    assert count(world["db"], f"user:{user.id}") == 1
    assert count(world["db"], f"user:{user.id}", "brief") == 1
    state = quotas.status(world["db"], user)
    assert state["runs_limit"] is None and state["runs_left"] is None
    assert count(world["db"], "guests") == 0


def test_missing_user_policy_fails_closed(world):
    _client, user = world["member"]("member@example.com")
    with world["db"].session() as session:
        row = session.get(UserQuotaPolicy, user.id)
        if row is not None:
            session.delete(row)
    with pytest.raises(Unavailable, match="配置缺失"):
        run_records.reserve_new_run(world["db"], user.id, "20990101-000000-00000001", "问题")


def test_stale_request_role_is_rechecked_and_disabled_denied(world):
    _client, user = world["member"]("member@example.com")
    with world["db"].session() as session:
        session.get(User, user.id).role = "guest"
    access = run_records.reserve_new_run(world["db"], user.id, "20990101-000000-00000001", "问题")
    assert access.role == "guest" and access.trial is not None
    assert count(world["db"], "guests") == 1
    with world["db"].session() as session:
        session.get(User, user.id).status = "disabled"
    with pytest.raises(Forbidden):
        run_records.reserve_new_run(world["db"], user.id, "20990101-000000-00000002", "问题")


def test_guest_refund_after_promotion_uses_original_scopes_once(world):
    _client, user = world["guest"]("guest")
    run_id = "20990101-000000-00000001"
    run_records.reserve_new_run(world["db"], user.id, run_id, "问题")
    with world["db"].session() as session:
        session.get(User, user.id).role = "user"
    policy(world["db"], user)
    run_records.release(world["db"], run_id)
    run_records.release(world["db"], run_id)
    assert count(world["db"], f"user:{user.id}") == 0
    assert count(world["db"], "guests") == 0
    with world["db"].session() as session:
        assert session.get(QuotaReservation, f"run:{run_id}").state == "refunded"


def test_member_refund_after_demotion_does_not_touch_guest_pool(world):
    _guest_client, guest = world["guest"]("guest")
    run_records.reserve_new_run(world["db"], guest.id, "20990101-000000-00000001", "guest")
    _client, user = world["member"]("member@example.com")
    policy(world["db"], user)
    run_id = "20990101-000000-00000002"
    run_records.reserve_new_run(world["db"], user.id, run_id, "member")
    with world["db"].session() as session:
        session.get(User, user.id).role = "guest"
    run_records.release(world["db"], run_id)
    assert count(world["db"], "guests") == 1
    assert count(world["db"], f"user:{user.id}") == 0


def test_started_run_cannot_be_refunded_or_removed(world):
    _client, user = world["guest"]("guest")
    run_id = "20990101-000000-00000001"
    run_records.reserve_new_run(world["db"], user.id, run_id, "问题")
    quotas.mark_started(world["db"], run_id)
    run_records.release(world["db"], run_id)
    assert count(world["db"], f"user:{user.id}") == 1
    assert run_records.run_owner(world["db"], run_id) == user.id


def test_duplicate_planning_request_is_not_charged_or_run_twice(world):
    client, user = world["member"]("member@example.com")
    policy(world["db"], user, 2, 3)
    body = {"query": "问题", "request_id": "same-id"}
    assert client.post("/api/brief", json=body).status_code == 200
    response = client.post("/api/brief", json=body)
    assert response.status_code == 409
    assert len(world["briefs"]) == 1
    assert count(world["db"], f"user:{user.id}", "brief") == 1


def test_invalid_guest_options_do_not_charge(world):
    client, user = world["guest"]("guest")
    assert client.post("/api/brief", json={"query": "问题", "source_mode": "library"}).status_code == 400
    assert count(world["db"], f"user:{user.id}", "brief") == 0


def test_personal_quota_uses_governed_timezone_and_resets(world, monkeypatch):
    client, user = world["member"]("member@example.com")
    policy(world["db"], user, 1)
    with world["db"].session() as session:
        session.get(PlatformGovernance, 1).quota_timezone = "UTC"
    assert _research(client).status_code == 200
    assert _research(client).status_code == 429
    before = quotas.status(world["db"], user)
    assert before["timezone"] == "UTC" and before["resets_at"].endswith("+00:00")
    tomorrow = trial.utcnow() + timedelta(days=1)
    monkeypatch.setattr(trial, "utcnow", lambda: tomorrow)
    assert _research(client).status_code == 200


def test_two_threads_racing_last_personal_slot(world):
    _client, user = world["member"]("member@example.com")
    policy(world["db"], user, 1)
    barrier, outcomes = threading.Barrier(2), []
    def attempt(i):
        barrier.wait()
        try:
            run_records.reserve_new_run(world["db"], user.id, f"20990101-000000-0000000{i}", "问题")
            outcomes.append("ok")
        except trial.QuotaExceeded:
            outcomes.append("full")
        except Exception as exc:
            outcomes.append(type(exc).__name__)
    threads = [threading.Thread(target=attempt, args=(i,)) for i in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(outcomes) == ["full", "ok"]
    assert count(world["db"], f"user:{user.id}") == 1


@pytest.mark.parametrize("scope", [{"source_mode": "library", "library_selection": {"documents": ["private"]}},
                                  {"source_mode": "web", "mcp_server_ids": ["private-mcp"]},
                                  {"source_mode": "mixed"}])
def test_demotion_to_guest_cannot_restore_private_followup_sources(world, scope):
    from fastapi.testclient import TestClient
    from dra import web
    from dra.api.deps import SESSION_COOKIE
    from dra.api.services import auth
    _client, user = world["member"]("member@example.com")
    parent = "20990101-000000-00000001"
    run_records.reserve_new_run(world["db"], user.id, parent, "原私域研究")
    quotas.mark_started(world["db"], parent)
    world["store"].create_run("原私域研究", None, {"mode": "simple", **scope}, run_id=parent)
    world["store"].finalize(parent, "done", {})
    auth.set_role(world["db"], "guest", email="member@example.com")
    token = auth.login(world["db"], "member@example.com", "correct horse battery").token
    client = TestClient(web.app, cookies={SESSION_COOKIE: token})
    response = client.post(f"/api/research/{parent}/followup", json={"request_id": "next", "text": "继续"})
    assert response.status_code == 409, response.text
    assert "访客不能追问" in response.json()["detail"]
    assert world["started"] == []
    assert count(world["db"], f"user:{user.id}") == 1  # Only the original member run remains charged.
    assert count(world["db"], "guests") == 0


def test_completed_followup_retry_at_quota_limit_returns_original_without_charging(world):
    client, user = world["member"]("member@example.com")
    parent, child = "20990101-000000-00000001", "20990101-000000-00000002"
    run_records.reserve_new_run(world["db"], user.id, parent, "原研究")
    quotas.mark_started(world["db"], parent)
    world["store"].create_run("原研究", None, {"mode": "simple"}, run_id=parent)
    world["store"].finalize(parent, "done", {})
    policy(world["db"], user, 2)
    run_records.reserve_followup(world["db"], user.id, parent, child, "继续")
    quotas.mark_started(world["db"], child)
    world["store"].create_run("继续", None, {"mode": "simple"}, run_id=child, identity={
        "conversation_id": parent, "parent_run_id": parent, "turn_index": 2, "request_id": "same",
        "followup_request": {"parent_run_id": parent, "text": "继续"}})
    world["store"].finalize(child, "done", {})
    # The last available slot was consumed by the first delivery of this request.
    for _ in range(2):
        response = client.post(f"/api/research/{parent}/followup", json={"request_id": "same", "text": "继续"})
        assert response.status_code == 202, response.text
        assert response.json()["run_id"] == child
    changed = client.post(f"/api/research/{parent}/followup", json={"request_id": "same", "text": "不同正文"})
    assert changed.status_code == 409
    assert count(world["db"], f"user:{user.id}") == 2
    assert world["started"] == []
