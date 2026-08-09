"""tests/test_tools_exa.py — exa_search 单元测试(C 方案双源)。

覆盖:
- 字段映射(title/source_url/snippet/raw_content/score/published_at)
- publishedDate 多格式容错(ISO long / ISO short / human "Mar 15, 2024")
- 空 text / 空 results / 422 API 错 / 缺 key 边界
- HTTP 错误重试(指数退避)

不打真 Exa API:全 mock `dra.tools.requests.post`。配合 conftest 的 _block_real_exa
fixture(autouse + raising=False)兜底防漏 mock。
"""

from unittest.mock import MagicMock

import pytest

from dra.tools import exa_search


@pytest.fixture(autouse=True)
def _isolated_exa_credentials(monkeypatch, request):
    """非 live 测试只用假 key，绝不依赖或读取项目根目录的真实 .env。"""
    if request.node.get_closest_marker("live"):
        return
    monkeypatch.setattr("dra.tools.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")


def _fake_exa_response(results: list[dict]):
    """构造 fake requests.Response,200 OK + json=results。"""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"results": results, "costDollars": {"total": 0.007}}
    return resp


def _fake_exa_error(status: int, body: str = "boom"):
    """构造 fake error Response(非 200)。"""
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    return resp


# ---------------------------------------------------------------------------
# 字段映射(normal case)
# ---------------------------------------------------------------------------


def test_exa_search_normal(monkeypatch):
    """正常返 3 results,验所有字段映射正确。"""
    fake_resp = _fake_exa_response([
        {
            "title": "Title 1",
            "url": "https://example.com/a",
            "text": "Full article text body 1234567890" * 20,  # 长 text
            "highlights": ["Query-related source excerpt with the useful fact."],
            "publishedDate": "2024-03-15T00:00:00.000Z",
            "score": 0.95,
        },
        {
            "title": "Title 2",
            "url": "https://example.com/b",
            "text": "Short text",
            "publishedDate": None,
            "score": 0.80,
        },
        {
            "title": "Title 3",
            "url": "https://example.com/c",
            "text": "Another body",
            "publishedDate": "2024-01-01",
            "score": 0.70,
        },
    ])
    monkeypatch.setattr("dra.tools.requests.post", lambda *a, **k: fake_resp)
    # 要绕开 _block_real_exa 给 dra.tools.exa_search 设的 _fail,
    # 直接 import 原函数(模块级 import 拿到的就是真实函数,_block_real_exa 只 patch 命名空间)
    from dra.tools import exa_search as real_exa  # noqa: F811

    docs = real_exa("test query", top_k=3)

    assert len(docs) == 3
    # 字段映射
    assert docs[0].title == "Title 1"
    assert docs[0].source_url == "https://example.com/a"
    assert docs[0].raw_content.startswith("Full article text body")
    assert docs[0].snippet == "Query-related source excerpt with the useful fact."
    assert docs[0].score == 0.95
    assert docs[0].published_at is not None
    assert "2024-03-15" in docs[0].published_at
    # 第 2 条无 publishedDate
    assert docs[1].published_at is None
    assert docs[1].snippet == "Short text"  # 老响应无 highlights 时回退全文头部
    # 第 3 条短日期格式
    assert docs[2].published_at is not None
    assert "2024-01-01" in docs[2].published_at


def test_exa_search_requests_bounded_highlights_and_keeps_full_text(monkeypatch):
    """搜索列表使用 query-related highlights，全文仍留给 fetch_page。"""
    fake_resp = _fake_exa_response([{
        "title": "T",
        "url": "https://example.com/a",
        "text": "正文开头无关。" + "完整正文" * 300,
        "highlights": ["与查询直接相关的第一段。", "与查询直接相关的第二段。"],
        "publishedDate": None,
        "score": 1.0,
    }])
    captured = {}

    def fake_post(*args, **kwargs):
        captured.update(kwargs["json"])
        return fake_resp

    monkeypatch.setattr("dra.tools.requests.post", fake_post)
    from dra.tools import exa_search as real_exa  # noqa

    doc = real_exa("目标查询", top_k=1)[0]

    assert captured["contents"]["text"] is True
    assert captured["contents"]["highlights"] == {
        "query": "目标查询",
        "maxCharacters": 600,
    }
    assert doc.snippet == "与查询直接相关的第一段。\n与查询直接相关的第二段。"
    assert len(doc.raw_content) > len(doc.snippet)


def test_exa_search_malformed_highlights_falls_back_to_bounded_text(monkeypatch):
    """第三方响应 highlights 形态异常时不能逐字符拼接或撑爆 snippet。"""
    body = "正文" * 500
    fake_resp = _fake_exa_response([{
        "title": "T", "url": "https://example.com/a", "text": body,
        "highlights": "not-a-list", "publishedDate": None, "score": 1.0,
    }])
    monkeypatch.setattr("dra.tools.requests.post", lambda *a, **k: fake_resp)
    from dra.tools import exa_search as real_exa  # noqa

    doc = real_exa("q", top_k=1)[0]

    assert doc.snippet == body[:600]


# ---------------------------------------------------------------------------
# publishedDate 多格式容错(复用 _try_parse_date)
# ---------------------------------------------------------------------------


def test_exa_search_published_date_iso_long(monkeypatch):
    """ISO 长格式 "2024-03-15T00:00:00.000Z" → 非 None。"""
    fake_resp = _fake_exa_response([
        {"title": "T", "url": "https://x.com", "text": "t",
         "publishedDate": "2024-03-15T00:00:00.000Z", "score": 1.0}
    ])
    monkeypatch.setattr("dra.tools.requests.post", lambda *a, **k: fake_resp)
    from dra.tools import exa_search as real_exa  # noqa
    docs = real_exa("q", top_k=1)
    assert docs[0].published_at is not None
    assert "2024-03-15" in docs[0].published_at


def test_exa_search_published_date_iso_short(monkeypatch):
    """ISO 短格式 "2024-03-15" → 非 None。"""
    fake_resp = _fake_exa_response([
        {"title": "T", "url": "https://x.com", "text": "t",
         "publishedDate": "2024-03-15", "score": 1.0}
    ])
    monkeypatch.setattr("dra.tools.requests.post", lambda *a, **k: fake_resp)
    from dra.tools import exa_search as real_exa  # noqa
    docs = real_exa("q", top_k=1)
    assert docs[0].published_at is not None
    assert "2024-03-15" in docs[0].published_at


def test_exa_search_published_date_human(monkeypatch):
    """人类格式 "Mar 15, 2024" → 经 _try_parse_date 解析非 None。"""
    fake_resp = _fake_exa_response([
        {"title": "T", "url": "https://x.com", "text": "t",
         "publishedDate": "Mar 15, 2024", "score": 1.0}
    ])
    monkeypatch.setattr("dra.tools.requests.post", lambda *a, **k: fake_resp)
    from dra.tools import exa_search as real_exa  # noqa
    docs = real_exa("q", top_k=1)
    assert docs[0].published_at is not None
    assert "2024-03-15" in docs[0].published_at


def test_exa_search_published_date_garbage_falls_to_none(monkeypatch):
    """不可解析的日期字符串 → published_at=None(降级,不抛)。"""
    fake_resp = _fake_exa_response([
        {"title": "T", "url": "https://x.com", "text": "t",
         "publishedDate": "yesterday afternoon maybe?", "score": 1.0}
    ])
    monkeypatch.setattr("dra.tools.requests.post", lambda *a, **k: fake_resp)
    from dra.tools import exa_search as real_exa  # noqa
    docs = real_exa("q", top_k=1)
    assert docs[0].published_at is None


# ---------------------------------------------------------------------------
# 空 text / 空 results 边界
# ---------------------------------------------------------------------------


def test_exa_search_empty_text_falls_back(monkeypatch):
    """text="" 时 snippet="" 不炸,raw_content 也空。"""
    fake_resp = _fake_exa_response([
        {"title": "Empty", "url": "https://x.com", "text": "",
         "publishedDate": None, "score": 0.5}
    ])
    monkeypatch.setattr("dra.tools.requests.post", lambda *a, **k: fake_resp)
    from dra.tools import exa_search as real_exa  # noqa
    docs = real_exa("q", top_k=1)
    assert len(docs) == 1
    assert docs[0].snippet == ""
    assert docs[0].raw_content == ""


def test_exa_search_empty_results(monkeypatch):
    """results=[] 返 [],不抛。"""
    fake_resp = _fake_exa_response([])
    monkeypatch.setattr("dra.tools.requests.post", lambda *a, **k: fake_resp)
    from dra.tools import exa_search as real_exa  # noqa
    docs = real_exa("q", top_k=3)
    assert docs == []


# ---------------------------------------------------------------------------
# HTTP 错误 / 缺 key
# ---------------------------------------------------------------------------


def test_exa_search_api_error_422_raises_after_retry(monkeypatch):
    """422 抛 RuntimeError 带状态码(经 max_retries 指数退避后)。"""
    fake_resp = _fake_exa_error(422, "Unprocessable Entity")
    monkeypatch.setattr("dra.tools.requests.post", lambda *a, **k: fake_resp)
    # 防退避慢测试
    monkeypatch.setattr("dra.tools.time.sleep", lambda s: None)
    from dra.tools import exa_search as real_exa  # noqa
    with pytest.raises(RuntimeError, match="exa_search"):
        real_exa("q", top_k=1, max_retries=2)


def test_exa_search_network_error_raises_after_retry(monkeypatch):
    """requests.post 抛网络错 → 重试后仍失败抛 RuntimeError。"""
    def _boom(*args, **kwargs):
        raise ConnectionError("network unreachable")
    monkeypatch.setattr("dra.tools.requests.post", _boom)
    monkeypatch.setattr("dra.tools.time.sleep", lambda s: None)
    from dra.tools import exa_search as real_exa  # noqa
    with pytest.raises(RuntimeError, match="exa_search"):
        real_exa("q", top_k=1, max_retries=1)


def test_exa_search_missing_key_raises(monkeypatch):
    """缺 EXA_API_KEY 抛 RuntimeError。"""
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    from dra.tools import exa_search as real_exa  # noqa
    with pytest.raises(RuntimeError, match="缺少 EXA_API_KEY"):
        real_exa("q", top_k=1)


# ---------------------------------------------------------------------------
# Live 测试(--run-live 才跑)
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_exa_search_live():
    """真打 Exa API,验真实返回结构。"""
    from dra.tools import exa_search as real_exa  # noqa
    docs = real_exa("latest LLM agent benchmark 2026", top_k=3)
    assert len(docs) >= 1, "Exa 真 API 应至少返 1 结果"
    d = docs[0]
    assert d.source_url and d.source_url.startswith("http")
    assert d.title
    assert d.raw_content and len(d.raw_content) > 100, "Exa text 应含全文"
    assert d.snippet
