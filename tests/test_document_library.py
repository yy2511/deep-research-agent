"""资料库的身份、位置、发布和隔离合同；encoder 替身不证明真实召回质量。"""
import json
import threading
from pathlib import Path

import numpy as np
import pytest

from dra.library import LibraryStore, LibraryError, chunk_ranges


class TinyEncoder:
    signature = "offline-test-v1"

    def offsets(self, text):
        return [(i, i + 1) for i in range(len(text))]

    def encode(self, texts):
        return np.asarray([[s.count("备份") + 0.01, s.count("日志") + 0.01,
                            s.count("Orchid") + 0.01] for s in texts], dtype="float32")


@pytest.fixture
def library(tmp_path):
    store = LibraryStore(tmp_path / "library", encoder=TinyEncoder())
    yield store
    store.close()


def add(library, text, name="约束.md", replace=None):
    return library.import_bytes(name, text.encode(), replace=replace, wait=True)


def test_identity_versions_and_idempotent_import(library):
    first = add(library, "Orchid 的备份保留 43 天。")
    assert first["status"] == "ready"
    assert add(library, "Orchid 的备份保留 43 天。")["version_id"] == first["version_id"]
    same_name = add(library, "另一部门的日志保留 9 天。")
    assert same_name["document_id"] != first["document_id"]
    pinned = library.pin([first["document_id"]])
    newer = add(library, "Orchid 的备份改为 17 天。", replace=first["document_id"])
    assert newer["version_id"] != first["version_id"]
    library.remove(first["document_id"])
    assert library.read_version(pinned["version_ids"][0])["text"] == "Orchid 的备份保留 43 天。"
    assert library.search("备份", pinned)[0].document["version_id"] == first["version_id"]
    with pytest.raises(LibraryError):
        library.pin([first["document_id"]])


def test_offsets_and_filters_are_applied_before_retrieval(library):
    text = "# Orchid 🙂\n\n" + "备份只允许在本地执行。\n" * 75 + "\n日志保留 43 天，编号 ZQ-719。"
    a = add(library, text)
    add(library, "日志保留 999 天。" * 200, name="未选资料.txt")
    pinned = library.pin([a["document_id"]])
    docs = library.search("ZQ-719 日志保留", pinned)
    assert len(docs) == 1  # 一份文件归成一个来源，命中位置可以有多处
    doc = docs[0]
    assert not doc.source_url and not doc.raw_content
    matches = doc.document["matches"]
    assert any("43 天" in hit["text"] for hit in matches)
    for hit in matches:
        assert text[hit["start"]:hit["end"]] == hit["text"]
    assert library.read_version(a["version_id"])["text"] == text


def test_failed_replacement_preserves_ready_version(library):
    first = add(library, "旧版本有效。")
    bad = library.import_bytes("broken.pdf", b"not a PDF", replace=first["document_id"], wait=True)
    assert bad["status"] == "failed" and bad["error"]
    assert library.pin([first["document_id"]])["version_ids"] == [first["version_id"]]
    assert library.list_documents()["documents"][0]["latest"]["status"] == "failed"


@pytest.mark.parametrize("name,body", [("bad.exe", b"x"), ("../x.md", b"x"),
                                        ("x.txt", b""), ("x.txt", b"\x00abc")])
def test_bad_uploads_are_rejected(library, name, body):
    with pytest.raises(LibraryError):
        library.import_bytes(name, body, wait=True)


def test_oversized_text_not_silently_published(library):
    result = add(library, "长" * 200001)
    assert result["status"] == "failed"
    assert library.list_documents()["documents"][0]["current"] is None


def test_restart_marks_unfinished_jobs_and_keeps_ready(library):
    ready = add(library, "备份 43 天。")
    path = library.root / "manifest.json"
    state = json.loads(path.read_text())
    state["versions"][ready["version_id"]]["status"] = "indexing"
    state["documents"][ready["document_id"]]["current_version"] = None
    path.write_text(json.dumps(state))
    restarted = LibraryStore(library.root, encoder=TinyEncoder())
    try:
        assert restarted.list_documents()["documents"][0]["latest"]["status"] == "failed"
    finally:
        restarted.close()


def test_chunk_ranges_cover_all_text_without_offset_drift():
    text = "# 标题🙂\n\n" + "中文 abc\n" * 140
    ranges = chunk_ranges(text, TinyEncoder().offsets(text), window=100, overlap=15)
    assert ranges[0][0] == 0 and ranges[-1][1] == len(text)
    assert all(a < b for a, b in ranges)
    assert all(right[0] <= left[1] for left, right in zip(ranges, ranges[1:]))
    assert all(b - a <= 100 for a, b in ranges)


def test_original_bytes_and_text_hash_detect_corruption(library):
    body = b"# Orchid\r\nBackup: 43 days.\r\n"
    result = library.import_bytes("manual.md", body, wait=True)
    version = library.read_version(result["version_id"])
    assert library.original(result["version_id"]) == body
    assert version["text"].encode() == body
    (library.root / "versions" / result["version_id"] / "text.txt").write_text("changed")
    with pytest.raises(LibraryError, match="校验"):
        library.read_version(result["version_id"])


def test_pdf_pages_encrypted_and_no_text(library):
    from pypdf import PdfWriter
    import io
    raw = (Path(__file__).resolve().parents[1] / "fixtures/library_demo/orchid-data-policy.pdf").read_bytes()
    version = library.import_bytes("规则.pdf", raw, wait=True)
    assert version["status"] == "ready", version
    parsed = library.read_version(version["version_id"])
    assert len(parsed["pages"]) == 2
    second = parsed["pages"][1]
    assert "17 days" in parsed["text"][second["start"]:second["end"]]
    assert library.original(version["version_id"]) == raw
    writer = PdfWriter(); writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO(); writer.write(buffer)
    blank = library.import_bytes("blank.pdf", buffer.getvalue(), wait=True)
    assert blank["status"] == "failed" and "OCR" in blank["error"]
    writer.encrypt("secret"); buffer = io.BytesIO(); writer.write(buffer)
    encrypted = library.import_bytes("encrypted.pdf", buffer.getvalue(), wait=True)
    assert encrypted["status"] == "failed" and "加密" in encrypted["error"]


def test_half_built_index_is_never_selected(library):
    entered, release = threading.Event(), threading.Event()
    class SlowEncoder(TinyEncoder):
        def encode(self, texts):
            entered.set()
            assert release.wait(5)
            return super().encode(texts)
    library._encoder = SlowEncoder()
    version = library.import_bytes("a.md", "备份规则".encode())
    try:
        assert entered.wait(5)
        assert library.list_documents()["documents"][0]["latest"]["status"] == "indexing"
        with pytest.raises(LibraryError):
            library.pin([version["document_id"]])
    finally:
        release.set()


def test_encoder_failure_never_publishes_replacement(library):
    old = add(library, "旧版备份规则")
    class Broken(TinyEncoder):
        def encode(self, texts):
            raise RuntimeError("simulated encoder failure")
    library._encoder = Broken()
    result = add(library, "新版备份规则", replace=old["document_id"])
    assert result["status"] == "failed"
    assert library.pin([old["document_id"]])["version_ids"] == [old["version_id"]]


def test_changed_encoder_allows_same_bytes_to_build_new_version(library):
    old = add(library, "日志保留43天")
    class Changed(TinyEncoder):
        signature = "offline-test-v2"
    library._encoder = Changed()
    new = add(library, "日志保留43天", replace=old["document_id"])
    assert new["status"] == "ready"
    assert new["version_id"] != old["version_id"]
    assert new["encoder"] == "offline-test-v2"
