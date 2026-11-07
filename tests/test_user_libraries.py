"""资料库按用户分开（#37）：各看各的；嵌入模型全进程一份；旧资料库搬到管理员目录。

设计 docs/design/阶段1-数据库与用户.md §3.4、§5。走真实登录与 Cookie（real_auth）。
"""
from __future__ import annotations

import time

import pytest
from conftest import install_library
from fastapi.testclient import TestClient
from test_document_library import TinyEncoder

from dra import admin, web
from dra import library as lib
from dra import simple_research as sr
from dra.api.deps import SESSION_COOKIE
from dra.api.services import auth, run_records

pytestmark = pytest.mark.real_auth

PASSWORD = "correct horse battery"


@pytest.fixture
def people(database):
    stores = []

    def person(email, role="user"):
        user = auth.create_user(database, email, PASSWORD, role=role)
        store = install_library(lib.LibraryStore(lib.user_library_root(user.id), encoder=TinyEncoder()), user.id)
        stores.append(store)
        client = TestClient(web.app, cookies={SESSION_COOKIE: auth.login(database, email, PASSWORD).token})
        return {"user": user, "client": client, "store": store}

    yield person
    for store in stores:
        store.close()


def _upload(client, name, text):
    response = client.post(f"/api/library/documents?name={name}", content=text.encode())
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        docs = client.get("/api/library").json()["documents"]
        doc = next((d for d in docs if d["latest"]["version_id"] == response.json()["version_id"]), None)
        if doc and doc["current"]:
            return doc["document_id"], doc["current"]["version_id"]
        time.sleep(0.03)
    raise AssertionError("导入未完成")


def test_each_user_only_sees_their_own_library(people):
    alice, bob = people("alice@example.com"), people("bob@example.com")
    doc, version = _upload(alice["client"], "secret.md", "日志保留 43 天。")
    bob_client = bob["client"]

    assert bob_client.get("/api/library").json()["documents"] == []
    for path in (f"/api/library/versions/{version}/text", f"/api/library/versions/{version}/original"):
        assert bob_client.get(path).status_code == 404
    assert bob_client.delete(f"/api/library/documents/{doc}").status_code == 404
    search = bob_client.post("/api/library/search", json={"query": "日志", "document_ids": [doc]})
    assert search.status_code == 400
    # 别人的资料编号发起研究：与选了不存在的资料一样被拒。
    research = bob_client.post("/api/research", json={
        "query": "保留多久", "mode": "simple", "source_mode": "library", "document_ids": [doc]})
    ghost = bob_client.post("/api/research", json={
        "query": "保留多久", "mode": "simple", "source_mode": "library", "document_ids": ["no-such-doc"]})
    assert research.status_code == ghost.status_code == 400
    assert research.json() == ghost.json()

    # Alice 自己的照常可用，且没被 Bob 的操作动到。
    assert "43 天" in alice["client"].get(f"/api/library/versions/{version}/text").text
    assert [d["document_id"] for d in alice["client"].get("/api/library").json()["documents"]] == [doc]


def test_libraries_live_in_per_user_directories(people):
    alice, bob = people("alice@example.com"), people("bob@example.com")
    _upload(alice["client"], "a.md", "甲")
    _upload(bob["client"], "b.md", "乙")
    assert alice["store"].root != bob["store"].root
    assert alice["store"].root == lib.user_library_root(alice["user"].id)
    assert [d["latest"]["file_name"] for d in lib.get_library(alice["user"].id).list_documents()["documents"]] == ["a.md"]
    assert [d["latest"]["file_name"] for d in lib.get_library(bob["user"].id).list_documents()["documents"]] == ["b.md"]


def test_embedding_model_is_loaded_once_for_all_users(monkeypatch):
    built = []

    class FakeEncoder:
        signature = "fake"

        def __init__(self):
            built.append(self)

    monkeypatch.setattr(lib, "LocalEncoder", FakeEncoder)
    monkeypatch.setattr(lib, "_shared_encoder", None)
    first, second, third = (lib.get_library(i) for i in (1, 2, 3))
    assert first is not second
    assert first.encoder is second.encoder is third.encoder
    assert len(built) == 1
    assert first.pool is second.pool and first.slots is second.slots


def test_same_directory_opens_one_store():
    assert lib.library_at(lib.user_library_root(5)) is lib.get_library(5)


def test_engine_refuses_to_guess_a_library():
    config = sr.SimpleConfig(source_mode="library", library_selection={"version_ids": ["v"]})
    with pytest.raises(lib.LibraryError, match="没有指定资料库目录"):
        sr._library_for(config)


# ---- 旧资料库搬到管理员目录 -----------------------------------------------------------

def _legacy_library(base):
    store = lib.LibraryStore(base, encoder=TinyEncoder())
    try:
        version = store.import_bytes("old.md", "旧资料：日志保留 43 天。".encode(), wait=True)
    finally:
        store.close()
    return version


def test_legacy_library_moves_to_the_admin(tmp_path, database):
    base = tmp_path / "library-base"
    version = _legacy_library(base)
    admin_user = auth.create_user(database, "boss@example.com", PASSWORD, role="admin")
    target = lib.user_library_root(admin_user.id)
    message = lib.adopt_legacy_library(base, target)
    assert str(target) in message
    assert not (base / "manifest.json").exists() and not (base / "versions").exists()
    moved = lib.LibraryStore(target, encoder=TinyEncoder())
    try:
        # 旧研究里引用的版本编号不变，搬过去后照样读得到。
        assert "43 天" in moved.read_version(version["version_id"])["text"]
    finally:
        moved.close()
    assert "无需搬移" in lib.adopt_legacy_library(base, target)


def test_legacy_library_replaces_an_empty_target_but_never_a_used_one(tmp_path):
    base = tmp_path / "library-base"
    _legacy_library(base)
    empty = tmp_path / "users" / "1"
    lib.LibraryStore(empty, encoder=TinyEncoder()).close()  # 登录后打开过的空资料库
    assert lib.library_is_empty(empty)
    lib.adopt_legacy_library(base, empty)
    assert not lib.library_is_empty(empty)

    other_base = tmp_path / "other-base"
    _legacy_library(other_base)
    with pytest.raises(lib.LibraryError, match="已有资料"):
        lib.adopt_legacy_library(other_base, empty)
    assert (other_base / "manifest.json").exists()


def test_import_legacy_command_moves_the_library(tmp_path, database, monkeypatch, capsys):
    base = lib.LIBRARY_BASE
    version = _legacy_library(base)
    admin_user = auth.create_user(database, "boss@example.com", PASSWORD, role="admin")
    (tmp_path / "runs").mkdir()
    monkeypatch.setattr(admin, "_ready_database", lambda: database)
    assert admin.main(["import-legacy", "--owner", admin_user.email,
                       "--runs-dir", str(tmp_path / "runs"), "--library-dir", str(base)]) == 0
    assert "旧资料库已移到" in capsys.readouterr().out
    store = install_library(lib.LibraryStore(lib.user_library_root(admin_user.id), encoder=TinyEncoder()),
                            admin_user.id)
    try:
        assert lib.get_library(admin_user.id).read_version(version["version_id"])["file_name"] == "old.md"
    finally:
        store.close()


def test_followup_reads_the_users_own_library(tmp_path, database, people, monkeypatch):
    """追问沿用父研究的资料范围，在当前用户自己的资料库里核对。"""
    from dra.api.services import runs as run_service
    from dra.runstore import RunStore

    alice = people("alice@example.com")
    doc, _ = _upload(alice["client"], "a.md", "日志保留 43 天。")
    selection = alice["store"].pin([doc])
    store = RunStore(tmp_path / "runs")
    parent = store.create_run("问题", None, {"mode": "simple", "source_mode": "library",
                                           "library_selection": selection})
    store.finalize(parent, "done", {})
    run_records.reserve_new_run(database, alice["user"].id, parent, "问题")
    seen = []
    registry = run_service.RunRegistry(store, max_active_runs=1)
    monkeypatch.setattr(registry, "start_followup",
                        lambda *a, config, **k: seen.append(config.library_root) or parent)
    run_service.start_followup(store, registry, parent, "r1", "继续", user_id=alice["user"].id)
    assert seen == [str(lib.user_library_root(alice["user"].id))]
