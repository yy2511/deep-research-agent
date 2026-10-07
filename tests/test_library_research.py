import json

import pytest

from dra import library as lib, simple_research as sr
from test_document_library import TinyEncoder


@pytest.fixture
def setup(tmp_path, monkeypatch):
    library = lib.LibraryStore(tmp_path / "library", encoder=TinyEncoder())
    monkeypatch.setattr(lib, "_library", library)
    def forbidden(*args, **kwargs):
        raise AssertionError("library-only research must not access web")
    monkeypatch.setattr(sr, "_retrieve", forbidden)
    monkeypatch.setattr(sr, "fetch_url_content", forbidden)
    text = "# Orchid 🙂\n\n" + "备份只能写入本地存储。\n" * 100 + "\n日志保留 43 天，编号 ZQ-719。"
    doc = library.import_bytes("内部约束.md", text.encode(), wait=True)
    selection = library.pin([doc["document_id"]])
    config = sr.SimpleConfig(source_mode="library", library_selection=selection)
    store = sr.RunStore(tmp_path / "run", run_id="local", query="保留多久")
    context = sr.ToolContext(store, config)
    yield library, doc, text, context
    library.close()


def test_search_only_is_not_citable_and_tail_hit_reads_correct_full_snapshot(setup):
    library, doc, text, ctx = setup
    receipt, ok = ctx.search_sources({"query": "ZQ-719 日志保留"})
    assert ok
    row = json.loads(receipt)["results"][0]
    assert row["document"]["version_id"] == doc["version_id"]
    denied, _ = ctx.save_evidence({"cards": [{"claim": "保留 43 天", "quote": "日志保留 43 天，编号 ZQ-719。"}]})
    assert not ctx.store.cards()
    hit = next(m for m in row["matches"] if "43 天" in m["text"])
    result, ok = ctx.read_source({"source_ref": row["source_ref"], "range": f'{hit["start"]}-{hit["end"]}'})
    assert ok
    parsed = json.loads(result)
    assert "43 天" in result
    assert ctx.store.read_source_text(parsed["source_ref"]) == text
    result, _ = ctx.save_evidence({"cards": [{"claim": "保留 43 天", "quote": "日志保留 43 天，编号 ZQ-719。"}]})
    assert len(ctx.store.cards()) == 1
    card = ctx.store.cards()[0]
    assert not card.source_url
    assert (ctx.store.sources_dir / f"{card.source_ref}.original").read_bytes() == text.encode()
    first_ref = card.source_ref
    # 同文件另一查询命中头部，复用整份已保存正文，不能复用错误的块。
    receipt, _ = ctx.search_sources({"query": "备份 本地"})
    row = json.loads(receipt)["results"][0]
    result, ok = ctx.read_source({"source_ref": row["source_ref"], "range": "0-300"})
    assert ok and json.loads(result)["source_ref"] == first_ref
    assert "备份只能" in result


def test_only_library_blocks_scope_and_url_and_child_inherits_fixed_versions(setup):
    library, doc, text, ctx = setup
    assert not ctx.search_sources({"query": "test", "scope": "web"})[1]
    assert not ctx.read_source({"url": "https://example.com"})[1]
    child = ctx.fork_for_subagent(sr.SimpleConfig(), budget=sr.Budget(sr.SimpleConfig(), None))
    assert child.config.source_mode == "library"
    assert child.config.library_selection == ctx.config.library_selection
    assert not child.read_source({"url": "https://example.com"})[1]
    library.import_bytes("内部约束.md", "日志保留 999 天。".encode(), replace=doc["document_id"], wait=True)
    library.remove(doc["document_id"])
    result, ok = child.search_sources({"query": "日志保留"})
    assert ok
    rows = json.loads(result)["results"]
    # 随机的版本/块哈希可能包含 999；版本隔离应核对正文和实际版本。
    assert all(row["document"]["version_id"] == doc["version_id"] for row in rows)
    matched_text = "\n".join(m["text"] for row in rows for m in row["matches"])
    assert "43 天" in matched_text and "999 天" not in matched_text


def test_default_read_starts_at_match_and_exposes_page_metadata(setup):
    _, _, _, ctx = setup
    receipt, _ = ctx.search_sources({"query": "ZQ-719 日志保留"})
    row = json.loads(receipt)["results"][0]
    result, ok = ctx.read_source({"source_ref": row["source_ref"]})
    assert ok and "43 天" in result
    payload = json.loads(result)
    assert payload["document"]["file_name"] == "内部约束.md"


def test_out_of_scope_local_source_cannot_be_read(setup):
    library, _, _, ctx = setup
    other = library.import_bytes("其他资料.txt", "保密内容 999".encode(), wait=True)
    selection = library.pin([other["document_id"]])
    doc = library.search("保密", selection)[0]
    source = ctx.store.register_summary(doc)
    _, ok = ctx.read_source({"source_ref": source.ref})
    assert not ok


def test_mixed_search_preserves_local_hits_and_reports_web_failure(setup, monkeypatch):
    _, _, _, ctx = setup
    ctx.config = ctx.config.model_copy(update={"source_mode":"mixed"})
    monkeypatch.setattr(sr, "_retrieve", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("web down")))
    receipt, ok = ctx.search_sources({"query":"日志"})
    assert ok
    data = json.loads(receipt)
    assert data["results"] and data["warnings"]


def test_archived_local_default_read_returns_saved_original(setup):
    _, _, _, ctx = setup
    receipt, _ = ctx.search_sources({"query":"日志"})
    ref = json.loads(receipt)["results"][0]["source_ref"]
    ctx.read_source({"source_ref":ref})
    ctx.save_evidence({"cards":[{"claim":"保留43天","quote":"日志保留 43 天，编号 ZQ-719。"}]})
    receipt, ok = ctx.read_source({"source_ref":ref})
    assert ok and not json.loads(receipt).get("archived")
    assert "日志保留 43 天，编号 ZQ-719。" in receipt


def test_full_loop_delivers_file_citation_with_scripted_model(setup, tmp_path):
    from test_simple_research import Script, _turn
    library, version, _, ctx = setup
    script = Script([
        _turn(("search_sources", {"query":"日志保留"})),
        _turn(("read_source", {"source_ref":"s1", "find":"日志保留"})),
        _turn(("save_evidence", {"cards":[{"claim":"日志保留 43 天", "quote":"日志保留 43 天，编号 ZQ-719。"}]})),
        _turn(("finish_research", {"draft":"日志保留 43 天 [c1]。", "coverage":[{"requirement_id":"goal", "status":"answered"}]})),
    ])
    result = sr.run_simple_research("日志保留多久", sr.requirements_from_query("日志保留多久"), config=ctx.config,
                                    run_dir=tmp_path / "full", run_id="scripted-library", model_call=script)
    assert result.delivery_status == "delivered", result
    assert len(result.cards) == 1
    assert '/api/runs/scripted-library/sources/s2?hl=' in result.markdown
    assert '仅用户所选本地资料' in script.seen[0][0]['content']
    ledger = json.loads((tmp_path / "full/run.json").read_text())
    assert ledger["source_scope"]["selection"]["version_ids"] == [version["version_id"]]
