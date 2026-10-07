"""检索结果附全文长度：读之前就知道长短；没带回正文的结果不猜。摘要上限可配置。"""
import json

from dra import simple_research as sr
from test_budget_wrapup import _doc


def _rows(tmp_path, docs, **config):
    store = sr.open_run(tmp_path / "run", query="q", run_id="r")
    context = sr.ToolContext(store, sr.SimpleConfig(**config), budget=None, search_fn=lambda _q: docs)
    payload, ok = context.search_sources({"query": "q"})
    assert ok
    return json.loads(payload)["results"]


def test_rows_with_body_report_total_chars_and_rows_without_do_not(tmp_path):
    long_doc = _doc(raw="长" * 50_000, id="d1", url="https://example.com/a")
    no_body = _doc(raw=None, id="d2", url="https://example.com/b")
    rows = _rows(tmp_path, [long_doc, no_body])
    assert rows[0]["total_chars"] == 50_000
    assert "total_chars" not in rows[1], "没带回正文就不知道长度，不能猜"


def test_total_chars_is_capped_at_what_reading_can_store(tmp_path):
    rows = _rows(tmp_path, [_doc(raw="x" * 300)], storage_max_chars=100)
    assert rows[0]["total_chars"] == 100


def test_snippet_is_complete_by_default_and_explicit_limits_remain_configurable(tmp_path):
    doc = _doc(raw="r")
    doc.snippet = "s" * 3000 + "末尾的关键限定条件。"
    assert _rows(tmp_path / "a", [doc])[0]["snippet"] == doc.snippet
    assert len(_rows(tmp_path / "b", [doc], search_snippet_chars=2500)[0]["snippet"]) == 2500
