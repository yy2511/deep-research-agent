"""示例（#47）：所有人只读可见；修改只有本人；示例期间冻结；只能公开纯网页、已结束的对话。

设计 docs/design/阶段2-公开试用.md §4；决定 ADR 0007。走真实的登录、Cookie 与数据库（real_auth）。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from test_run_ownership import _OWNED_KEYS, GHOST, _concrete

from dra import admin, web
from dra.api.deps import OWNED_RUN, READABLE_CONVERSATION, READABLE_RUN, SESSION_COOKIE
from dra.api.services import auth, run_records, showcases
from dra.api.services import runs as run_service
from dra.api.services.errors import Conflict, InvalidRequest, NotFound
from dra.runstore import RunStore

pytestmark = pytest.mark.real_auth

PASSWORD = "correct horse battery"


@pytest.fixture
def world(tmp_path, monkeypatch, database):
    store = RunStore(tmp_path / "runs")
    monkeypatch.setattr(web.app.state, "store", store)
    monkeypatch.setattr(web.app.state, "registry", run_service.RunRegistry(store, max_active_runs=1))

    def person(email, role="user"):
        user = auth.create_user(database, email, PASSWORD, role=role)
        client = TestClient(web.app, cookies={SESSION_COOKIE: auth.login(database, email, PASSWORD).token})
        return user, client

    def research(user, query, *, source_mode="web", status="done", parent=None, mcp=()):
        identity = None
        if parent is not None:
            meta = store.get_meta(parent)
            identity = {"conversation_id": meta["conversation_id"], "parent_run_id": parent,
                        "turn_index": meta["turn_index"] + 1}
        run_id = store.create_run(query, None, {"mode": "simple", "source_mode": source_mode,
                                                "library_selection": {"version_ids": ["v1"]} if source_mode != "web" else {},
                                                "mcp_server_ids": list(mcp)},
                                  identity=identity)
        store.append_event(run_id, {"seq": 0, "type": "run_start", "ts": 0})
        if status != "running":
            store.finalize(run_id, status, {})
        if parent is None:
            run_records.reserve_new_run(database, user.id, run_id, query)
        else:
            run_records.reserve_followup(database, user.id, parent, run_id, query)
        return run_id

    boss, boss_client = person("boss@example.com", role="admin")
    bob, bob_client = person("bob@example.com")
    first = research(boss, "示例问题")
    second = research(boss, "示例追问", parent=first)
    private = research(boss, "管理员的私有研究")
    return {"store": store, "db": database, "boss": boss, "boss_client": boss_client, "bob": bob,
            "bob_client": bob_client, "anonymous": TestClient(web.app), "research": research,
            "showcase": first, "turns": [first, second], "private": private}


def _publish(world, title="示例一"):
    return showcases.add(world["db"], world["store"], world["showcase"], title)


def _read_operations() -> list[tuple[str, str]]:
    return [(method.upper(), path) for path, methods in web.app.openapi()["paths"].items()
            if any(f"{{{key}}}" in path for key in _OWNED_KEYS)
            for method in methods if method == "get"]


# ---- 谁能看 --------------------------------------------------------------------

def test_anyone_reads_a_showcase_exactly_as_the_owner_does(world):
    """每个查看接口：未登录、别的用户读示例，得到的与主人自己读的逐字节相同。"""
    _publish(world)
    operations = _read_operations()
    assert len(operations) >= 16  # 防止空跑：查看接口当前 16 个
    for run_id in world["turns"]:
        for method, path in operations:
            url = _concrete(path, world["showcase"] if "{conversation_id}" in path else run_id)
            owner = world["boss_client"].request(method, url)
            for reader in (world["anonymous"], world["bob_client"]):
                seen = reader.request(method, url)
                assert (seen.status_code, seen.content) == (owner.status_code, owner.content), (path, run_id)


def test_non_showcase_research_looks_missing_to_others(world):
    """管理员的私有研究、示例对话以外的编号：未登录、别人看到的与不存在的编号一样（404）。"""
    _publish(world)
    for method, path in _read_operations():
        for reader in (world["anonymous"], world["bob_client"]):
            theirs = reader.request(method, _concrete(path, world["private"]))
            ghost = reader.request(method, _concrete(path, GHOST))
            assert theirs.status_code == 404 and theirs.content == ghost.content, path


def test_showcase_list_is_public_and_only_counts_registered_turns(world):
    assert world["anonymous"].get("/api/showcases").json() == []
    _publish(world, "缓存怎么选")
    items = world["anonymous"].get("/api/showcases").json()
    assert [(i["title"], i["query"], i["run_ids"], i["turn_count"], i["available"]) for i in items] == [
        ("缓存怎么选", "示例问题", world["turns"], 2, True)]
    conversation = world["anonymous"].get(f"/api/conversations/{world['showcase']}").json()
    assert [t["run_id"] for t in conversation["turns"]] == world["turns"]


def test_expired_login_still_sees_showcases_but_is_sent_to_login_for_own_research(world):
    stale = TestClient(web.app, cookies={SESSION_COOKIE: "expired-or-forged-token"})
    _publish(world)
    assert stale.get(f"/api/runs/{world['showcase']}").status_code == 200
    # 自己的研究不能突然显示“不存在”：Cookie 失效要回登录页。
    assert stale.get(f"/api/runs/{world['private']}").status_code == 401


# ---- 谁能改 --------------------------------------------------------------------

@pytest.mark.parametrize("action", ["followup", "messages", "cancel"])
def test_nobody_else_can_change_a_showcase(world, action):
    _publish(world)
    url = f"/api/research/{world['showcase']}/{action}"
    body = {"request_id": "r1", "text": "继续"} if action == "followup" else {"message_id": "m1", "text": "hi"}
    assert world["anonymous"].post(url, json=body).status_code == 401
    assert world["bob_client"].post(url, json=body).status_code == 404


def test_showcase_conversation_is_frozen_even_for_its_owner(world):
    _publish(world)
    response = world["boss_client"].post(f"/api/research/{world['turns'][-1]}/followup",
                                         json={"request_id": "r1", "text": "再查一点"})
    assert response.status_code == 409 and "取消示例" in response.json()["detail"]
    assert run_records.registered_conversation_run_ids(world["db"], world["showcase"]) == set(world["turns"])
    # 取消示例后恢复：可以继续追问（这里只验证数据库占位，不启动研究）。
    showcases.remove(world["db"], world["showcase"])
    run_records.reserve_followup(world["db"], world["boss"].id, world["turns"][-1], "20990101-000000-00000001", "再查")
    assert world["anonymous"].get(f"/api/runs/{world['showcase']}").status_code == 404


# ---- 设为示例的条件 --------------------------------------------------------------

def test_only_finished_web_only_admin_conversations_can_be_published(world):
    db, store, research = world["db"], world["store"], world["research"]
    with pytest.raises(InvalidRequest, match="管理员"):
        showcases.add(db, store, research(world["bob"], "普通用户的研究"), "x")
    with pytest.raises(InvalidRequest, match="还没有结束"):
        showcases.add(db, store, research(world["boss"], "还在跑", status="running"), "x")
    with pytest.raises(InvalidRequest, match="纯网页"):
        showcases.add(db, store, research(world["boss"], "用了资料库", source_mode="library"), "x")
    with pytest.raises(InvalidRequest, match="纯网页"):
        showcases.add(db, store, research(world["boss"], "用了外部资料服务", mcp=["github"]), "x")
    # 对话里有一轮已占位、还没建目录（追问刚提交）：不能发布。
    run_records.reserve_followup(db, world["boss"].id, world["private"], "20990101-000000-00000002", "追问")
    with pytest.raises(InvalidRequest, match="没有可读的运行记录"):
        showcases.add(db, store, world["private"], "x")
    with pytest.raises(InvalidRequest, match="标题"):
        showcases.add(db, store, world["showcase"], " ")
    with pytest.raises(NotFound):
        showcases.add(db, store, GHOST, "x")
    _publish(world)
    with pytest.raises(Conflict):
        _publish(world)
    with pytest.raises(NotFound):
        showcases.remove(db, world["private"])


@pytest.mark.postgres
def test_publishing_and_following_up_at_once_never_leaks_a_new_turn(world, monkeypatch):
    """两边都锁对话行：先拿到锁的一方完成后，另一方看到它的结果（发布在先 → 追问 409）。"""
    entered, release = threading.Event(), threading.Event()
    real_check = showcases._check

    def slow_check(session, store, conversation_id):
        result = real_check(session, store, conversation_id)
        entered.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(showcases, "_check", slow_check)
    publisher = threading.Thread(target=_publish, args=(world,))
    publisher.start()
    assert entered.wait(10)
    outcome = {}

    def follow_up():
        try:
            run_records.reserve_followup(world["db"], world["boss"].id, world["turns"][-1],
                                         "20990101-000000-00000003", "同时追问")
            outcome["result"] = "reserved"
        except Conflict:
            outcome["result"] = "frozen"

    follower = threading.Thread(target=follow_up)
    follower.start()
    follower.join(0.5)
    assert follower.is_alive()  # 在等发布那边的行锁
    release.set()
    publisher.join(10)
    follower.join(10)
    assert outcome == {"result": "frozen"}
    assert run_records.registered_conversation_run_ids(world["db"], world["showcase"]) == set(world["turns"])


@pytest.mark.postgres
def test_a_followup_in_progress_blocks_publishing(world, monkeypatch):
    """追问先拿到锁：占位提交后，发布看到一轮没有运行目录，拒绝。"""
    entered, release = threading.Event(), threading.Event()
    real_ids = run_records.conversation_run_ids

    def slow_ids(session, conversation_id):
        ids = real_ids(session, conversation_id)
        if threading.current_thread().name == "follower":
            entered.set()
            assert release.wait(10)
        return ids

    monkeypatch.setattr(run_records, "conversation_run_ids", slow_ids)
    follower = threading.Thread(name="follower", target=run_records.reserve_followup,
                                args=(world["db"], world["boss"].id, world["turns"][-1], "20990101-000000-00000004", "先追问"))
    follower.start()
    assert entered.wait(10)
    outcome = {}

    def publish():
        try:
            _publish(world)
            outcome["result"] = "published"
        except InvalidRequest:
            outcome["result"] = "refused"

    publisher = threading.Thread(target=publish)
    publisher.start()
    publisher.join(0.5)
    assert publisher.is_alive()
    release.set()
    follower.join(10)
    publisher.join(10)
    assert outcome == {"result": "refused"}
    assert world["anonymous"].get("/api/showcases").json() == []


# ---- 结构检查与管理命令 -------------------------------------------------------------

def test_read_and_write_groups_are_separate():
    """查看组只有 GET、挂“属于自己或示例”；修改组只有 POST、挂“只有本人”。新接口放错组会失败。"""
    from dra.api.routers import conversations, research, runs, skills

    for group, required in ((runs.read_router, READABLE_RUN), (research.read_router, READABLE_RUN),
                            (skills.read_router, READABLE_RUN),
                            (conversations.conversation_router, READABLE_CONVERSATION)):
        assert [d.dependency for d in group.dependencies] == [d.dependency for d in required]
        assert {m for route in group.routes for m in route.methods} <= {"GET", "HEAD"}
    assert [d.dependency for d in research.run_router.dependencies] == [d.dependency for d in OWNED_RUN]
    assert {m for route in research.run_router.routes for m in route.methods} == {"POST"}


def test_showcase_command_lists_what_becomes_public_and_asks_first(world, monkeypatch, capsys):
    monkeypatch.setattr(admin, "_ready_database", lambda: world["db"])
    runs_dir = str(world["store"].root)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert admin.main(["showcase", "add", world["showcase"], "--title", "示例一", "--runs-dir", runs_dir]) == 1
    out = capsys.readouterr().out
    assert "第 1 轮：示例问题" in out and "第 2 轮：示例追问" in out and "未登录" in out and "没有改动" in out
    assert showcases.list_public(world["db"], world["store"]) == []
    assert admin.main(["showcase", "add", world["showcase"], "--title", "示例一", "--yes", "--runs-dir", runs_dir]) == 0
    assert admin.main(["showcase", "list", "--runs-dir", runs_dir]) == 0
    assert f"{world['showcase']}  示例一  · 2 轮" in capsys.readouterr().out
    assert admin.main(["showcase", "remove", world["showcase"], "--runs-dir", runs_dir]) == 0
    assert admin.main(["showcase", "add", "--title", "x", "--runs-dir", runs_dir]) == 2
    assert "需要对话编号" in capsys.readouterr().err
