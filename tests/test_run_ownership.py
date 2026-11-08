"""归属检查（#31）：只能访问自己的研究和对话；别人的与不存在的完全一样（404，同一句话）。

设计 docs/design/阶段1-数据库与用户.md §3.1–§3.3。走真实的登录、Cookie 和数据库归属，
不用 conftest 的默认放行（real_auth）。
"""
from __future__ import annotations

import re

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from dra import web
from dra.api.deps import OWNED_RUN, SESSION_COOKIE, CurrentUserDep, owned_run
from dra.api.services import auth, run_records
from dra.api.services import runs as run_service
from dra.api.services.errors import NotFound
from dra.db_tables import RunAttempt
from dra.runstore import RunStore

pytestmark = pytest.mark.real_auth

PASSWORD = "correct horse battery"
GHOST = "20990101-000000-deadbeef"  # 合法格式、谁都没有的编号
# 路径参数填合法格式，让请求真正走到读取数据那一步；格式不对时接口会先报 404，看不出差别。
_SAMPLES = {"seq": "0", "sha256": "a" * 64, "receipt_id": "a" * 32, "ref": "s1",
            "execution_id": "exec_" + "a" * 32, "checkpoint_id": "ctx-" + "a" * 32,
            "artifact_id": "a1"}
_OWNED_KEYS = ("run_id", "conversation_id")


@pytest.fixture
def world(tmp_path, monkeypatch, database):
    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(web.app.state, "store", store)
    monkeypatch.setattr(web.app.state, "registry", run_service.RunRegistry(store, max_active_runs=1))

    def person(email):
        user = auth.create_user(database, email, PASSWORD)
        client = TestClient(web.app, cookies={SESSION_COOKIE: auth.login(database, email, PASSWORD).token})
        run_id = store.create_run(f"{email} 的问题", None, {"mode": "simple"})
        store.append_event(run_id, {"seq": 0, "type": "run_start", "ts": 0})
        store.finalize(run_id, "done", {})
        run_records.reserve_new_run(database, user.id, run_id, f"{email} 的问题")
        return user, client, run_id

    return {"store": store, "alice": person("alice@example.com"), "bob": person("bob@example.com")}


def _owned_operations(target_app) -> list[tuple[str, str]]:
    return [(method.upper(), path)
            for path, methods in target_app.openapi()["paths"].items()
            if any(f"{{{key}}}" in path for key in _OWNED_KEYS)
            for method in methods if method in {"get", "post", "put", "patch", "delete"}]


def _concrete(path: str, owned_id: str) -> str:
    return re.sub(r"\{([^}]+)\}",
                  lambda m: owned_id if m.group(1) in _OWNED_KEYS else _SAMPLES.get(m.group(1), "x"), path)


def cross_user_leaks(target_app, other_client, owned_id: str) -> tuple[list[str], int]:
    """用别人的身份访问 owned_id；必须与访问不存在的编号逐字节相同的 404。返回 (问题, 检查数)。"""
    leaks = []
    operations = _owned_operations(target_app)
    for method, path in operations:
        theirs = other_client.request(method, _concrete(path, owned_id), json={})
        ghost = other_client.request(method, _concrete(path, GHOST), json={})
        if theirs.status_code != 404 or (theirs.status_code, theirs.content) != (ghost.status_code, ghost.content):
            leaks.append(f"{method} {path} → {theirs.status_code} {theirs.text[:60]}")
    return leaks, len(operations)


def test_every_run_and_conversation_endpoint_hides_other_peoples_data(world):
    _, bob_client, _ = world["bob"]
    _, _, alice_run = world["alice"]
    leaks, checked = cross_user_leaks(web.app, bob_client, alice_run)
    assert leaks == []
    # 防止空跑：带研究编号 / 对话编号的接口当前共 20 个。
    assert checked >= 20
    paths = {path for _, path in _owned_operations(web.app)}
    assert {"/api/runs/{run_id}", "/api/runs/{run_id}/sources/{ref}/original",
            "/api/runs/{run_id}/artifacts/{artifact_id}/download", "/api/research/{run_id}/stream",
            "/api/research/{run_id}/cancel", "/api/conversations/{conversation_id}",
            "/api/runs/{run_id}/skills"} <= paths


def test_every_run_and_conversation_route_sits_in_an_owned_group():
    """结构检查（第二道保险）：带研究编号 / 对话编号的接口全部定义在挂了归属检查的分组里。

    只用公开的 APIRouter.routes，不读 FastAPI 内部的路由包装。
    """
    from dra.api.deps import READABLE_CONVERSATION, READABLE_RUN
    from dra.api.routers import conversations, research, runs, skills

    # 查看接口：属于自己或属于示例（#47）；修改接口：只有本人（#31）。两种检查对别人的研究都返回 404。
    groups = [(runs.read_router, READABLE_RUN), (research.read_router, READABLE_RUN),
              (skills.read_router, READABLE_RUN), (research.run_router, OWNED_RUN),
              (conversations.conversation_router, READABLE_CONVERSATION)]
    grouped = set()
    for group, required in groups:
        assert [d.dependency for d in group.dependencies] == [d.dependency for d in required]
        grouped |= {(method, route.path) for route in group.routes for method in route.methods}
    assert set(_owned_operations(web.app)) == grouped


def test_owner_still_reaches_their_own_research(world):
    _, alice_client, alice_run = world["alice"]
    detail = alice_client.get(f"/api/runs/{alice_run}")
    assert detail.status_code == 200
    assert detail.json()["meta"]["run_id"] == alice_run
    conversation = alice_client.get(f"/api/conversations/{alice_run}")
    assert conversation.status_code == 200
    assert conversation.json()["latest_run_id"] == alice_run


def test_lists_only_show_your_own(world):
    for name, other in (("alice", "bob"), ("bob", "alice")):
        _, client, own_run = world[name]
        _, _, other_run = world[other]
        runs = [m["run_id"] for m in client.get("/api/runs").json()]
        conversations = [c["conversation_id"] for c in client.get("/api/conversations").json()]
        assert runs == [own_run] and conversations == [own_run]
        assert other_run not in runs


def test_admins_do_not_see_other_peoples_research(world, database):
    admin = auth.create_user(database, "boss@example.com", PASSWORD, role="admin")
    client = TestClient(web.app, cookies={
        SESSION_COOKIE: auth.login(database, admin.email, PASSWORD).token})
    _, _, alice_run = world["alice"]
    assert client.get("/api/runs").json() == []
    assert client.get(f"/api/runs/{alice_run}").status_code == 404


def test_other_users_cannot_follow_up_or_cancel(world, database):
    store = world["store"]
    _, bob_client, _ = world["bob"]
    _, _, alice_run = world["alice"]
    with database.session() as session:
        before = session.scalar(select(func.count()).select_from(RunAttempt))
    followup = bob_client.post(f"/api/research/{alice_run}/followup",
                               json={"request_id": "r1", "text": "偷偷追问"})
    cancel = bob_client.post(f"/api/research/{alice_run}/cancel")
    assert followup.status_code == cancel.status_code == 404
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(RunAttempt)) == before
    assert store.get_meta(alice_run)["status"] == "done"


def test_followup_reservation_refuses_someone_elses_conversation(world, database):
    """接口层之外再守一道：业务层占位时也核对对话主人。"""
    bob, _, _ = world["bob"]
    _, _, alice_run = world["alice"]
    with pytest.raises(NotFound):
        run_records.reserve_followup(database, bob.id, alice_run, "20990101-000001-aaaaaaaa", "追问")


def test_unregistered_research_is_hidden_from_everyone(world):
    """没登记到数据库的研究（旧数据未导入）谁都看不到，需先执行 import-legacy。"""
    store = world["store"]
    _, alice_client, _ = world["alice"]
    orphan = store.create_run("没登记的旧研究", None, {"mode": "simple"})
    assert alice_client.get(f"/api/runs/{orphan}").status_code == 404
    assert orphan not in [m["run_id"] for m in alice_client.get("/api/runs").json()]


def test_the_check_catches_a_route_outside_the_owned_group(world, database):
    """变异用例：放在受保护分组外、忘了查归属的接口必须被抓到。"""
    probe = FastAPI()
    probe.state.db = database
    guarded, forgotten = APIRouter(dependencies=OWNED_RUN), APIRouter()

    @guarded.get("/api/probe/{run_id}/guarded")
    def _guarded(run_id: str):
        return {"run_id": run_id}

    @forgotten.get("/api/probe/{run_id}/forgotten")
    def _forgotten(run_id: str, user: CurrentUserDep):
        return {"run_id": run_id}

    probe.include_router(guarded)
    probe.include_router(forgotten)
    _, _, alice_run = world["alice"]
    bob = TestClient(probe, cookies=dict(world["bob"][1].cookies))
    leaks, checked = cross_user_leaks(probe, bob, alice_run)
    assert checked == 2
    assert len(leaks) == 1 and leaks[0].startswith("GET /api/probe/{run_id}/forgotten → 200")


def test_ownership_check_returns_the_connection_before_streaming(world, database):
    """进度流这类长连接：查完归属就还连接，推送期间不占（ADR 0006 规则 1、5）。"""
    probe = FastAPI()
    probe.state.db = database
    seen = []
    router = APIRouter(dependencies=[Depends(owned_run)])

    @router.get("/api/probe/{run_id}/stream")
    async def stream(run_id: str):
        async def body():
            seen.append(database.engine.pool.checkedout())
            yield run_id

        return StreamingResponse(body())

    probe.include_router(router)
    _, alice_client, alice_run = world["alice"]
    client = TestClient(probe, cookies=dict(alice_client.cookies))
    with client.stream("GET", f"/api/probe/{alice_run}/stream") as response:
        assert response.read() == alice_run.encode()
    assert seen == [0]


# ---- #39 审查：没登记的轮次在对话汇总和追问判断里都不算 ---------------------------------

def _leftover_turn(store, parent, *, request_id="r1", text="追问"):
    """模拟启动失败后残留的目录：meta.json 完整、指向父对话，但数据库里没有登记。"""
    child = store.create_run(text, None, {"mode": "simple"}, identity={
        "conversation_id": parent, "parent_run_id": parent, "turn_index": 2, "request_id": request_id,
        "followup_request": {"parent_run_id": parent, "text": text}})
    store.finalize(child, "failed", None)
    return child


def test_conversations_ignore_unregistered_turns(world):
    store = world["store"]
    _, alice_client, alice_run = world["alice"]
    leftover = _leftover_turn(store, alice_run)
    summary = alice_client.get("/api/conversations").json()
    assert [(c["conversation_id"], c["latest_run_id"], c["run_id"], c["turn_count"]) for c in summary] == [
        (alice_run, alice_run, alice_run, 1)]
    detail = alice_client.get(f"/api/conversations/{alice_run}").json()
    assert detail["latest_run_id"] == alice_run and detail["total"] == 1
    assert [t["run_id"] for t in detail["turns"]] == [alice_run]
    assert alice_client.get(f"/api/runs/{leftover}").status_code == 404


@pytest.fixture
def failed_followup(tmp_path, monkeypatch, database):
    """真实追问接口：保存追问上下文时失败一次，留下一个没登记的目录。"""
    from test_research_history import history_fixture

    import dra.durable

    store, parent, _, _ = history_fixture(tmp_path / "history")
    registry = run_service.RunRegistry(store, max_active_runs=1)
    monkeypatch.setattr(web.app.state, "store", store)
    monkeypatch.setattr(web.app.state, "registry", registry)
    user = auth.create_user(database, "carol@example.com", PASSWORD)
    run_records.reserve_new_run(database, user.id, parent, "研究日志与备份")
    client = TestClient(web.app, raise_server_exceptions=False,
                        cookies={SESSION_COOKIE: auth.login(database, user.email, PASSWORD).token})

    real_atomic_json = dra.durable.atomic_json
    failures = []

    def fail_once(path, value):
        if getattr(path, "name", "") == "followup_context.json" and not failures:
            failures.append(path)
            raise OSError("disk full")
        return real_atomic_json(path, value)

    monkeypatch.setattr(dra.durable, "atomic_json", fail_once)
    response = client.post(f"/api/research/{parent}/followup", json={"request_id": "r1", "text": "继续"})
    assert response.status_code == 500
    leftover = failures[0].parent.name
    assert store.get_meta(leftover)["status"] == "failed"

    started = []

    def start(query, **kwargs):
        # 只核对追问判断，不真正开跑研究：按真实流程建好这一轮的目录。
        context = kwargs["followup_context"]
        started.append(kwargs["run_id"])
        return store.create_run(query, None, {"mode": "simple"}, run_id=kwargs["run_id"], identity={
            **{k: context[k] for k in ("conversation_id", "parent_run_id", "turn_index", "request_id")},
            "followup_request": {"parent_run_id": context["parent_run_id"], "text": query}})

    monkeypatch.setattr(registry, "start_simple_research", start)
    return {"client": client, "parent": parent, "leftover": leftover, "started": started}


def test_failed_followup_leaves_the_conversation_usable(failed_followup):
    client, parent, leftover = (failed_followup[k] for k in ("client", "parent", "leftover"))
    assert client.get(f"/api/runs/{leftover}").status_code == 404
    summary = client.get("/api/conversations").json()
    assert [(c["latest_run_id"], c["turn_count"]) for c in summary] == [(parent, 1)]
    assert client.get(f"/api/conversations/{parent}").json()["latest_run_id"] == parent


def test_retrying_the_same_followup_starts_a_reachable_turn(failed_followup):
    client, parent, leftover = (failed_followup[k] for k in ("client", "parent", "leftover"))
    response = client.post(f"/api/research/{parent}/followup", json={"request_id": "r1", "text": "继续"})
    assert response.status_code == 202, response.text
    new_run = response.json()["run_id"]
    assert new_run != leftover and failed_followup["started"] == [new_run]
    assert client.get(f"/api/runs/{new_run}").status_code == 200
    detail = client.get(f"/api/conversations/{parent}").json()
    assert [t["run_id"] for t in detail["turns"]] == [parent, new_run]


def test_a_different_followup_is_not_blocked_by_the_leftover(failed_followup):
    client, parent = failed_followup["client"], failed_followup["parent"]
    response = client.post(f"/api/research/{parent}/followup", json={"request_id": "r2", "text": "换个问法"})
    assert response.status_code == 202, response.text
    assert client.get(f"/api/runs/{response.json()['run_id']}").status_code == 200
