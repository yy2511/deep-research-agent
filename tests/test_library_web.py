import json
import time

import pytest
from fastapi.testclient import TestClient

from dra import library as lib, web
from test_document_library import TinyEncoder
from test_web_research_e2e import isolated_web


@pytest.fixture
def library_web(tmp_path, monkeypatch, isolated_web):
    store = lib.LibraryStore(tmp_path / "library", encoder=TinyEncoder())
    monkeypatch.setattr(lib, "_library", store)
    with TestClient(web.app) as client:
        yield client, store, isolated_web
    store.close()


def test_upload_preview_search_remove(library_web):
    client, store, _ = library_web
    response = client.post("/api/library/documents?name=Orchid.md", content="日志保留 43 天。".encode())
    assert response.status_code == 202
    version_id = response.json()["version_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = client.get("/api/library").json()
        if payload["documents"][0]["current"]:
            break
        time.sleep(.03)
    document_id = payload["documents"][0]["document_id"]
    assert "43 天" in client.get(f"/api/library/versions/{version_id}/text").text
    result = client.post("/api/library/search", json={"query": "日志保留", "document_ids": [document_id]})
    assert result.status_code == 200 and "43 天" in result.text
    assert client.delete(f"/api/library/documents/{document_id}").status_code == 200
    assert client.get("/api/library").json()["documents"] == []
    assert client.get(f"/api/library/versions/{version_id}/original").content == "日志保留 43 天。".encode()


def test_research_pins_selection_and_refuses_unready_or_empty(library_web, monkeypatch):
    client, library, (_, registry) = library_web
    captured = []
    monkeypatch.setattr(registry, "start_simple_research", lambda query, **kw: captured.append(kw["config"]) or "test")
    bad = client.post("/api/research", json={"query": "资料研究", "mode": "simple", "source_mode": "library"})
    assert bad.status_code == 400 and not captured
    v = library.import_bytes("a.md", "备份 43 天。".encode(), wait=True)
    response = client.post("/api/research", json={"query": "资料研究", "mode": "simple", "source_mode": "library", "document_ids": [v["document_id"]]})
    assert response.status_code == 200
    assert captured[0].source_mode == "library"
    assert captured[0].library_selection["version_ids"] == [v["version_id"]]
    assert client.post("/api/research", json={"query": "x", "mode": "simple", "source_mode": "invalid"}).status_code == 422


def test_reject_bad_upload_and_unknown_version(library_web):
    client, _, _ = library_web
    assert client.post("/api/library/documents?name=x.exe", content=b"abc").status_code == 400
    assert client.get("/api/library/versions/vmissing/text").status_code == 404


def test_history_original_and_highlight_survive_library_changes(library_web):
    from dra import simple_research as sr
    from pathlib import Path
    client, library, (store, _) = library_web
    raw = (Path(__file__).resolve().parents[1] / "fixtures/library_demo/orchid-data-policy.pdf").read_bytes()
    version = library.import_bytes("policy.pdf", raw, wait=True)
    run_id = store.create_run("backup?", None, {"mode": "simple"})
    inner = sr.RunStore(store.root / run_id, run_id=run_id, query="backup?")
    ctx = sr.ToolContext(inner, sr.SimpleConfig(source_mode="library",library_selection=library.pin([version["document_id"]])))
    result, _ = ctx.search_sources({"query":"backup"})
    ref = json.loads(result)["results"][0]["source_ref"]
    result, ok = ctx.read_source({"source_ref":ref,"find":"17 days"})
    assert ok
    body = json.loads(result); full_ref = body["source_ref"]
    span = body["excerpts"][0]["location"].removeprefix("chars=")
    library.remove(version["document_id"])
    assert client.get(f"/api/runs/{run_id}/sources/{full_ref}/original").content == raw
    highlighted = client.get(f"/api/runs/{run_id}/sources/{full_ref}?hl={span}")
    assert highlighted.status_code == 200 and '<mark id="cited">' in highlighted.text and '17 days' in highlighted.text
    path = inner.sources_dir / f"{full_ref}.original"
    path.write_bytes(b"changed")
    assert client.get(f"/api/runs/{run_id}/sources/{full_ref}/original").status_code == 404
