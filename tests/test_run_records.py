"""研究记录的归属（#30）：先占位、再启动、失败撤销；追问；规划记录；旧数据导入。

设计 docs/design/阶段1-数据库与用户.md §1、§3.2、§5。只替换模型与检索，不花钱。
"""
from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_web_research_e2e import _install_boundaries, _wait_done, isolated_web  # noqa: F401

from dra import admin, web
from dra.api.services import run_records
from dra.api.services.errors import InvalidRequest, NotFound, Unavailable
from dra.db import Database
from dra.db_tables import Conversation, ResearchTask, RunAttempt, User


def _rows(database):
    with database.session() as session:
        return {
            "conversations": [(c.id, c.user_id) for c in session.scalars(select(Conversation))],
            "tasks": [(t.conversation_id, t.user_id, t.query)
                      for t in session.scalars(select(ResearchTask).order_by(ResearchTask.id))],
            "attempts": [a.run_id for a in session.scalars(select(RunAttempt).order_by(RunAttempt.id))],
        }


def _empty(database):
    return _rows(database) == {"conversations": [], "tasks": [], "attempts": []}


# ---- 发起研究 --------------------------------------------------------------------

def test_new_research_is_recorded_before_it_starts(isolated_web, monkeypatch, database, logged_in_user):
    store, registry = isolated_web
    _install_boundaries(monkeypatch)
    seen_at_start = []
    original = registry.start_simple_research

    def start(*args, **kwargs):
        # 启动那一刻，占位已经在库里（先占位，再启动）。
        seen_at_start.append(_rows(database)["attempts"])
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "start_simple_research", start)
    with TestClient(web.app) as client:
        run_id = client.post("/api/research", json={"query": "新问题", "mode": "simple"}).json()["run_id"]
        _wait_done(client, run_id)
    assert seen_at_start == [[run_id]]
    assert _rows(database) == {
        "conversations": [(run_id, logged_in_user.id)],
        "tasks": [(run_id, logged_in_user.id, "新问题")],
        "attempts": [run_id],
    }
    assert store.get_meta(run_id)["user_id"] == logged_in_user.id


def test_database_down_starts_nothing(isolated_web, monkeypatch, database):
    store, registry = isolated_web
    _install_boundaries(monkeypatch)
    started = []
    monkeypatch.setattr(registry, "start_simple_research", lambda *a, **k: started.append(1))

    def down(*_args):
        raise Unavailable("数据库暂时不可用，请稍后再试")

    monkeypatch.setattr(run_records, "reserve_new_run", down)
    response = TestClient(web.app).post("/api/research", json={"query": "新问题", "mode": "simple"})
    assert response.status_code == 503
    assert started == []
    assert store.list_runs() == []


def test_unreachable_database_is_reported_as_unavailable():
    # 真实连不上的库：连接类错误翻译成 Unavailable（接口层 503），不是 500。
    unreachable = Database.from_url("postgresql://u:p@127.0.0.1:1/dra_test")
    try:
        with pytest.raises(Unavailable):
            run_records.reserve_new_run(unreachable, 1, "20990101-000000-deadbeef", "问题")
    finally:
        unreachable.dispose()


def test_rejected_start_releases_the_reservation(isolated_web, monkeypatch, database):
    store, _ = isolated_web
    brief = {"original_query": "另一个问题", "goal": "g", "requirements": [], "route": []}
    response = TestClient(web.app).post(
        "/api/research", json={"query": "新问题", "mode": "simple", "brief": brief})
    assert response.status_code == 400
    assert _empty(database)
    assert store.list_runs() == []


# ---- 追问 ------------------------------------------------------------------------

def _start_and_finish(client, query="新问题"):
    run_id = client.post("/api/research", json={"query": query, "mode": "simple"}).json()["run_id"]
    _wait_done(client, run_id)
    return run_id


def test_followup_joins_the_parent_conversation(isolated_web, monkeypatch, database, logged_in_user):
    store, _ = isolated_web
    _install_boundaries(monkeypatch)
    with TestClient(web.app) as client:
        parent = _start_and_finish(client)
        response = client.post(f"/api/research/{parent}/followup",
                               json={"request_id": "req-1", "text": "再细说"})
        assert response.status_code == 202, response.text
        child = response.json()["run_id"]
        # 同一 request_id 重发：返回原来那一轮，不留下多余的占位。
        again = client.post(f"/api/research/{parent}/followup",
                            json={"request_id": "req-1", "text": "再细说"})
        assert again.json()["run_id"] == child
        # 这里只关心归属记录；假模型只备了一轮回复，追问这一轮直接取消。
        client.post(f"/api/research/{child}/cancel")
        _wait_done(client, child)
    rows = _rows(database)
    assert rows["conversations"] == [(parent, logged_in_user.id)]
    assert rows["tasks"] == [(parent, logged_in_user.id, "新问题"),
                             (parent, logged_in_user.id, "再细说")]
    assert rows["attempts"] == [parent, child]
    assert store.get_meta(child)["user_id"] == logged_in_user.id


def test_followup_on_unimported_research_is_refused(isolated_web, database):
    from test_research_history import history_fixture

    store, _ = isolated_web
    legacy_store, parent, _, _ = history_fixture(store.root.parent / "legacy")
    web.app.state.store = legacy_store  # isolated_web 的 monkeypatch 会在测试后还原
    response = TestClient(web.app).post(f"/api/research/{parent}/followup",
                                        json={"request_id": "req-1", "text": "继续"})
    assert response.status_code == 409
    assert "import-legacy" in response.json()["detail"]
    assert _empty(database)
    assert [m["run_id"] for m in legacy_store.list_runs()] == [parent]


def test_release_keeps_a_conversation_that_still_has_turns(database, logged_in_user):
    run_records.reserve_new_run(database, logged_in_user.id, "20990101-000000-aaaaaaaa", "首问")
    run_records.reserve_followup(database, logged_in_user.id, "20990101-000000-aaaaaaaa",
                                 "20990101-000001-bbbbbbbb", "追问")
    run_records.release(database, "20990101-000001-bbbbbbbb")
    assert _rows(database)["attempts"] == ["20990101-000000-aaaaaaaa"]
    run_records.release(database, "20990101-000000-aaaaaaaa")
    assert _empty(database)
    run_records.release(database, "20990101-000000-aaaaaaaa")  # 再撤销一次也没事


# ---- 规划记录 ----------------------------------------------------------------------

def test_planning_records_who_asked(isolated_web, monkeypatch, logged_in_user):
    from dra.api.routers import planning as planning_api

    seen = {}

    def fake_brief(query, **kwargs):
        seen.update(kwargs)
        return {"kind": "clarify", "gaps": []}, {}

    monkeypatch.setattr(planning_api, "_brief_payload", fake_brief)
    assert TestClient(web.app).post("/api/brief", json={"query": "问题"}).status_code == 200
    assert seen["user_id"] == logged_in_user.id


def test_plan_store_writes_user_id(tmp_path):
    from dra.runstore import PlanAttemptStore

    store = PlanAttemptStore(tmp_path)
    plan_id = store.create(query="q", kind="brief", config_summary={}, user_id=7)
    assert json.loads((tmp_path / plan_id / "meta.json").read_text())["user_id"] == 7
    other = store.create(query="q", kind="brief", config_summary={})
    assert "user_id" not in json.loads((tmp_path / other / "meta.json").read_text())


# ---- 旧数据导入 --------------------------------------------------------------------

def _legacy_runs(root):
    """几种真实会遇到的旧目录：两轮对话、旧格式（无对话字段）、坏 JSON、缺 meta、内部目录、杂文件。"""
    def run(run_id, **meta):
        directory = root / run_id
        directory.mkdir(parents=True)
        (directory / "meta.json").write_text(json.dumps({"run_id": run_id, **meta}), encoding="utf-8")
        (directory / "events.jsonl").write_text('{"seq": 0}\n', encoding="utf-8")

    run("20260901-100000-aaaaaaaa", query="首问", started_at=1_756_720_800.0,
        conversation_id="20260901-100000-aaaaaaaa", turn_index=1)
    run("20260901-110000-bbbbbbbb", query="追问", started_at=1_756_724_400.0,
        conversation_id="20260901-100000-aaaaaaaa", turn_index=2)
    run("20260801-090000-cccccccc", query="旧格式", started_at=1_754_038_800.0)
    (root / "20260802-090000-dddddddd").mkdir()
    (root / "20260802-090000-dddddddd" / "meta.json").write_text("{坏", encoding="utf-8")
    (root / "20260803-090000-eeeeeeee").mkdir()
    (root / ".workspaces").mkdir()
    (root / "notes.txt").write_text("x", encoding="utf-8")


def _digest(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_import_legacy_assigns_everything_to_the_admin(tmp_path, database, logged_in_user):
    root = tmp_path / "runs"
    _legacy_runs(root)
    before = _digest(root)
    report = run_records.import_legacy(database, root, logged_in_user.email.upper())
    assert sorted(report.imported) == ["20260801-090000-cccccccc", "20260901-100000-aaaaaaaa",
                                       "20260901-110000-bbbbbbbb"]
    assert [r for r, _ in report.unreadable] == ["20260802-090000-dddddddd", "20260803-090000-eeeeeeee"]
    rows = _rows(database)
    assert sorted(rows["conversations"]) == [("20260801-090000-cccccccc", logged_in_user.id),
                                             ("20260901-100000-aaaaaaaa", logged_in_user.id)]
    turns = [(c, q) for c, _, q in rows["tasks"] if c == "20260901-100000-aaaaaaaa"]
    assert turns == [("20260901-100000-aaaaaaaa", "首问"), ("20260901-100000-aaaaaaaa", "追问")]
    with database.session() as session:
        created = session.get(Conversation, "20260901-100000-aaaaaaaa").created_at
    assert created.timestamp() == 1_756_720_800.0
    # 文件一个字节都没动。
    assert _digest(root) == before

    again = run_records.import_legacy(database, root, logged_in_user.email)
    assert again.imported == [] and len(again.already) == 3
    assert _rows(database) == rows


def test_import_legacy_requires_an_existing_admin(tmp_path, database):
    root = tmp_path / "runs"
    _legacy_runs(root)
    with pytest.raises(NotFound):
        run_records.import_legacy(database, root, "nobody@example.com")
    with database.session() as session:
        session.add(User(email="plain@example.com", password_hash="x", role="user"))
    with pytest.raises(InvalidRequest, match="不是管理员"):
        run_records.import_legacy(database, root, "plain@example.com")
    assert _rows(database)["attempts"] == []


def test_imported_research_can_be_followed_up(tmp_path, database, logged_in_user):
    root = tmp_path / "runs"
    _legacy_runs(root)
    run_records.import_legacy(database, root, logged_in_user.email)
    conversation = run_records.reserve_followup(database, logged_in_user.id, "20260901-110000-bbbbbbbb",
                                                "20261107-000000-ffffffff", "第三轮")
    assert conversation == "20260901-100000-aaaaaaaa"


def test_import_legacy_command(tmp_path, database, logged_in_user, monkeypatch, capsys):
    root = tmp_path / "runs"
    _legacy_runs(root)
    monkeypatch.setattr(admin, "_ready_database", lambda: database)
    assert admin.main(["import-legacy", "--owner", logged_in_user.email, "--runs-dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert "新登记 3 条；已登记过、跳过 0 条；读不了、跳过 2 条" in out
    assert "读不了：20260802-090000-dddddddd" in out
    assert admin.main(["import-legacy", "--owner", "nobody@example.com", "--runs-dir", str(root)]) == 2
    assert "不存在" in capsys.readouterr().err


def test_migrations_create_run_record_tables(database):
    from sqlalchemy import inspect

    tables = set(inspect(database.engine).get_table_names())
    assert {"conversations", "research_tasks", "run_attempts"} <= tables
    with database.session() as session:
        assert session.scalar(select(func.count()).select_from(RunAttempt)) == 0


# ---- #36 审查：取消与坏时间戳 -------------------------------------------------------

def _cancel_during_reservation(monkeypatch, name, start_owned):
    """把真实的占位卡在写库前，取消请求，再放行；返回启动次数。"""
    import asyncio
    import threading

    from dra.api.services import runs as run_service

    entered, gate = threading.Event(), threading.Event()
    real = getattr(run_records, name)

    def slow(*args):
        entered.set()
        assert gate.wait(5)
        return real(*args)

    monkeypatch.setattr(run_records, name, slow)
    started = []
    monkeypatch.setattr(run_service, "start_research", lambda *a, **k: started.append(1) or "x")
    monkeypatch.setattr(run_service, "start_followup", lambda *a, **k: started.append(1) or {"run_id": "x"})

    async def scenario():
        task = asyncio.create_task(start_owned(run_service))
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.set()  # 占位线程这时才提交
        await asyncio.gather(*list(run_service._pending_cleanups))

    asyncio.run(scenario())
    return started


def test_cancelled_new_research_leaves_no_reservation(monkeypatch, database, logged_in_user):
    started = _cancel_during_reservation(
        monkeypatch, "reserve_new_run",
        lambda rs: rs.start_owned_research(database, None, logged_in_user.id, "问题"))
    assert started == []
    assert _empty(database)


def test_cancelled_followup_leaves_no_reservation(tmp_path, monkeypatch, database, logged_in_user):
    from dra.runstore import RunStore

    store = RunStore(tmp_path / "runs")
    parent = store.create_run("首问", None, {"mode": "simple"})
    run_records.reserve_new_run(database, logged_in_user.id, parent, "首问")
    started = _cancel_during_reservation(
        monkeypatch, "reserve_followup",
        lambda rs: rs.start_owned_followup(database, store, None, logged_in_user.id, parent, "r1", "追问"))
    assert started == []
    assert _rows(database)["attempts"] == [parent]


@pytest.mark.parametrize("bad", [1_700_000_000_000, "inf", "nan", "not-a-number"])
def test_one_bad_timestamp_does_not_roll_back_the_batch(tmp_path, database, logged_in_user, bad):
    root = tmp_path / "runs"
    for run_id, started in (("20260901-100000-aaaaaaaa", 1_756_720_800.0),
                            ("20260902-100000-bbbbbbbb", bad)):
        (root / run_id).mkdir(parents=True)
        (root / run_id / "meta.json").write_text(
            json.dumps({"run_id": run_id, "query": "q", "started_at": started}), encoding="utf-8")
    report = run_records.import_legacy(database, root, logged_in_user.email)
    assert report.imported == ["20260901-100000-aaaaaaaa"]
    assert [r for r, _ in report.unreadable] == ["20260902-100000-bbbbbbbb"]
    assert _rows(database)["attempts"] == ["20260901-100000-aaaaaaaa"]
