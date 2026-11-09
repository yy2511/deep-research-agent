"""公开试用的次数与限制（#49）：只作用于 GitHub 访客；用户与管理员不受影响。

设计 docs/design/阶段2-公开试用.md §1、§3；决定 ADR 0007。走真实的登录、Cookie 与数据库（real_auth）。
研究引擎和规划模型用替身：记录“是否被调用、用的什么配置”，不调用真实模型。
"""
from __future__ import annotations

import threading
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from dra import runtime_config, web
from dra.api.deps import SESSION_COOKIE
from dra.api.routers import planning as planning_router
from dra.api.services import auth, run_records, trial
from dra.api.services import runs as run_service
from dra.api.services.auth import ExternalProfile
from dra.db_tables import DailyCounter, RunAttempt, utcnow
from dra.runstore import RunStore
from dra.simple_research import SimpleConfig

pytestmark = pytest.mark.real_auth

PASSWORD = "correct horse battery"


def _set_trial(**fields):
    settings = runtime_config._code_defaults()
    trial_fields = {"provider": "deepseek", "model": "deepseek-chat", **fields}
    runtime_config.save(settings.model_copy(update={"trial": runtime_config.TrialSettings(**trial_fields)}))


@pytest.fixture
def world(tmp_path, monkeypatch, database):
    store = RunStore(tmp_path / "runs")
    registry = run_service.RunRegistry(store, max_active_runs=10)
    monkeypatch.setattr(web.app.state, "store", store)
    monkeypatch.setattr(web.app.state, "registry", registry)
    _set_trial()
    started = []

    def fake_start(query, **kwargs):  # 研究引擎替身：记下配置，不真正运行
        started.append(kwargs)
        return kwargs["run_id"]

    monkeypatch.setattr(registry, "start_simple_research", fake_start)
    briefs = []

    def fake_brief(*args, planner=None, **kwargs):  # 规划模型替身
        briefs.append(planner)
        return {"kind": "clarify", "gaps": []}, {}

    for name in ("_brief_payload", "_plan_after_clarification_payload", "_revise_brief_payload"):
        monkeypatch.setattr(planning_router, name, fake_brief)

    def guest(login):
        result = auth.login_external(database, ExternalProfile(provider="github", subject=login, login=login))
        return TestClient(web.app, cookies={SESSION_COOKIE: result.token}), result.user

    def member(email, role="user"):
        auth.create_user(database, email, PASSWORD, role=role)
        result = auth.login(database, email, PASSWORD)
        return TestClient(web.app, cookies={SESSION_COOKIE: result.token}), result.user

    return {"db": database, "store": store, "registry": registry, "started": started, "briefs": briefs,
            "guest": guest, "member": member}


def _research(client, **extra):
    return client.post("/api/research", json={"query": "缓存怎么选", "mode": "simple", **extra})


def _brief(client, endpoint="/api/brief"):
    bodies = {"/api/brief": {"query": "问题"}, "/api/brief/plan": {"query": "问题"},
              "/api/brief/revise": {"brief": {}, "feedback": "改一下"}}
    return client.post(endpoint, json=bodies[endpoint])


def _counters(db):
    with db.session() as session:
        return {(c.scope.split(":")[0], c.kind): c.count for c in session.scalars(select(DailyCounter))}


# ---- 收紧 ------------------------------------------------------------------------

def test_guest_research_runs_on_the_trial_model_with_web_only(world):
    client, _ = world["guest"]("octo")
    assert _research(client).status_code == 200
    kwargs = world["started"][-1]
    config = kwargs["config"]
    assert (config.adapter.provider, config.adapter.model) == ("deepseek", "deepseek-chat")
    assert config.execution.enabled is False and config.browser.enabled is False
    assert config.mcp_servers == [] and config.source_mode == "web" and config.vision.provider == ""
    defaults = runtime_config.TrialSettings()
    assert (config.wall_timeout_s, config.max_llm_calls, config.max_tool_calls) == (
        defaults.wall_timeout_s, defaults.max_llm_calls, defaults.max_tool_calls)
    assert kwargs["guest_limit"] == defaults.max_concurrent_guest_runs


@pytest.mark.parametrize("extra, message", [
    ({"source_mode": "library", "document_ids": ["d1"]}, "只能用网页搜索"),
    ({"source_mode": "mixed", "document_ids": ["d1"]}, "只能用网页搜索"),
    ({"mcp_server_ids": ["github"]}, "外部资料服务"),
])
def test_guests_cannot_pick_private_sources_and_are_not_charged(world, extra, message):
    client, _ = world["guest"]("octo")
    response = _research(client, **extra)
    assert response.status_code == 400 and message in response.json()["detail"]
    assert _counters(world["db"]) == {} and world["started"] == []


def test_guests_cannot_use_the_library(world):
    guest, _ = world["guest"]("octo")
    member, _ = world["member"]("alice@example.com")
    for path in ("/api/library",):
        assert guest.get(path).status_code == 403
        assert member.get(path).status_code == 200


def test_trial_closed_until_a_model_is_chosen(world):
    runtime_config.save(runtime_config._code_defaults())  # 没有试用模型
    client, _ = world["guest"]("octo")
    response = _research(client)
    assert response.status_code == 503 and "试用暂未开放" in response.json()["detail"]
    assert _brief(client).status_code == 503
    assert client.get("/api/me").json()["trial"]["open"] is False
    member, _ = world["member"]("alice@example.com")
    assert _research(member).status_code == 200  # 非访客不受影响


# ---- 研究次数 --------------------------------------------------------------------

def test_daily_research_limit_per_guest_then_overall(world):
    _set_trial(per_user_daily_runs=2, global_daily_runs=3)
    first, _ = world["guest"]("octo")
    assert [_research(first).status_code for _ in range(2)] == [200, 200]
    third = _research(first)
    assert third.status_code == 429 and "你今天的试用研究次数用完了" in third.json()["detail"]
    assert first.get("/api/me").json()["trial"]["runs_left"] == 0
    second, _ = world["guest"]("hubot")
    assert _research(second).status_code == 200
    blocked = _research(second)
    assert blocked.status_code == 429 and "今天的试用研究名额已经用完了" in blocked.json()["detail"]
    assert _counters(world["db"])[("guests", "run")] == 3
    with world["db"].session() as session:
        assert session.scalar(select(func.count()).select_from(RunAttempt)) == 3
    # 用户与管理员不受影响。
    member, _ = world["member"]("alice@example.com")
    assert [_research(member).status_code for _ in range(4)] == [200] * 4


def test_failed_start_gives_the_attempt_back(world, monkeypatch):
    _set_trial(per_user_daily_runs=1)
    client, _ = world["guest"]("octo")
    full = {"on": True}

    def start(query, **kwargs):
        if full["on"]:
            raise run_service.ActiveRunLimitError("现在使用的人较多，请稍后再试。")
        return kwargs["run_id"]

    monkeypatch.setattr(world["registry"], "start_simple_research", start)
    assert _research(client).status_code == 429
    assert set(_counters(world["db"]).values()) == {0}
    with world["db"].session() as session:
        assert session.scalar(select(func.count()).select_from(RunAttempt)) == 0
    full["on"] = False
    assert _research(client).status_code == 200  # 退回的那次还能用


def test_the_count_resets_at_local_midnight(world, monkeypatch):
    _set_trial(per_user_daily_runs=1)
    client, user = world["guest"]("octo")
    assert _research(client).status_code == 200
    assert _research(client).status_code == 429
    resets_at = client.get("/api/me").json()["trial"]["resets_at"]
    assert resets_at.endswith("T00:00:00+08:00")  # 北京时间 0 点
    tomorrow = utcnow() + timedelta(days=1)
    monkeypatch.setattr(trial, "utcnow", lambda: tomorrow)
    assert _research(client).status_code == 200


def test_followups_count_as_research(world):
    _set_trial(per_user_daily_runs=1)
    _client, user = world["guest"]("octo")
    t = trial.for_user(user)
    run_records.reserve_new_run(world["db"], user.id, "20990101-000000-00000001", "问题", trial=t)
    with pytest.raises(trial.QuotaExceeded):
        run_records.reserve_followup(world["db"], user.id, "20990101-000000-00000001",
                                     "20990101-000000-00000002", "追问", trial=t)
    run_records.release(world["db"], "20990101-000000-00000001")
    assert set(_counters(world["db"]).values()) == {0}


@pytest.mark.postgres
def test_two_guests_racing_for_the_last_slot_get_exactly_one(world):
    _set_trial(global_daily_runs=1)
    users = [world["guest"](name)[1] for name in ("octo", "hubot")]
    barrier, outcomes = threading.Barrier(2), []

    def attempt(user, run_id):
        barrier.wait()
        try:
            run_records.reserve_new_run(world["db"], user.id, run_id, "问题", trial=trial.for_user(user))
            outcomes.append("ok")
        except trial.QuotaExceeded:
            outcomes.append("full")

    threads = [threading.Thread(target=attempt, args=(u, f"20990101-000000-0000000{i}"))
               for i, u in enumerate(users)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(outcomes) == ["full", "ok"]
    assert _counters(world["db"])[("guests", "run")] == 1


# ---- 规划次数 --------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", ["/api/brief", "/api/brief/plan", "/api/brief/revise"])
def test_planning_is_charged_before_the_model_is_called(world, endpoint):
    _set_trial(per_user_daily_briefs=5, global_daily_briefs=2)
    clients = [world["guest"](name)[0] for name in ("a", "b", "c")]
    assert [_brief(c, endpoint).status_code for c in clients[:2]] == [200, 200]
    blocked = _brief(clients[2], endpoint)
    assert blocked.status_code == 429 and "今天的试用规划名额已经用完了" in blocked.json()["detail"]
    assert len(world["briefs"]) == 2  # 第三次没有调用模型
    assert world["briefs"][0] == {"planner_model": "deepseek-chat", "planner_provider": "deepseek",
                                  "planner_effort": None}


def test_no_planning_once_research_is_used_up(world):
    _set_trial(per_user_daily_runs=1)
    client, _ = world["guest"]("octo")
    assert _research(client).status_code == 200
    response = _brief(client)
    assert response.status_code == 429 and "研究次数" in response.json()["detail"]
    assert world["briefs"] == []


def test_members_plan_with_the_site_model_and_without_limits(world):
    _set_trial(per_user_daily_briefs=1, global_daily_briefs=1)
    member, _ = world["member"]("alice@example.com")
    assert [_brief(member).status_code for _ in range(3)] == [200] * 3
    assert world["briefs"] == [{}, {}, {}]


# ---- 同时进行 --------------------------------------------------------------------

def _busy(registry, run_id, *, user_id, guest):
    handle = run_service.RunHandle(run_id)
    handle.busy, handle.user_id, handle.guest = True, user_id, guest
    registry.runs[run_id] = handle


def test_one_running_research_per_guest_and_a_cap_for_all_guests(tmp_path):
    registry = run_service.RunRegistry(RunStore(tmp_path), max_active_runs=10)
    web_only = SimpleConfig()
    _busy(registry, "r1", user_id=1, guest=True)
    with pytest.raises(run_service.ActiveRunLimitError, match="已经有一个研究在进行中"):
        registry.start_simple_research("问题", config=web_only, user_id=1, guest_limit=2)
    _busy(registry, "r2", user_id=2, guest=True)
    with pytest.raises(run_service.ActiveRunLimitError, match="使用的人较多"):
        registry.start_simple_research("问题", config=web_only, user_id=3, guest_limit=2)
    # 管理员与用户不占访客名额、也不受它限制（只受全站上限）。
    _busy(registry, "r3", user_id=9, guest=False)
    assert sum(h.guest for h in registry.runs.values() if h.busy) == 2


# ---- 设置 ------------------------------------------------------------------------

def test_saving_other_settings_keeps_the_trial_settings(world):
    _set_trial(per_user_daily_runs=7)
    admin, _ = world["member"]("boss@example.com", role="admin")
    current = admin.get("/api/config").json()
    body = {key: current[key] for key in ("planner", "subagent")}
    assert admin.put("/api/config", json=body).status_code == 200
    assert runtime_config.current().trial.per_user_daily_runs == 7


@pytest.mark.parametrize("bad", [{"timezone": "Mars/Base"}, {"model": None}, {"per_user_daily_runs": -1}])
def test_invalid_trial_settings_are_rejected(bad):
    with pytest.raises(ValueError):
        runtime_config.TrialSettings(**{"provider": "deepseek", "model": "deepseek-chat", **bad})


def test_me_reports_trial_only_for_guests(world):
    _set_trial(per_user_daily_runs=2)
    guest, _ = world["guest"]("octo")
    assert guest.get("/api/me").json()["trial"]["runs_left"] == 2
    _research(guest)
    assert guest.get("/api/me").json()["trial"]["runs_left"] == 1
    member, _ = world["member"]("alice@example.com")
    assert member.get("/api/me").json()["trial"] is None
