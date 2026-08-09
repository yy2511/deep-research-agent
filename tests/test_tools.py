"""P2-1 工具层测试：日期解析 + 时间衰减权重 + 重排。

本地（默认跑）：
- parse_published_at：仅从 Tavily published_date 解析（不含 snippet）
- _detect_recency_tau：时效关键词 → τ；无关键词 → None
- _time_weight：新/旧/无日期
- _combined_score：score × weight
- web_search：时效 query 重排 / 非时效 query 保持原序

live（--run-live 才跑）：
- 真实检索，检查 published_at 是否被填充
"""

from unittest.mock import MagicMock

import pytest

from dra.models import RetrievedDoc
from dra.tools import (
    _combined_score,
    _detect_recency_tau,
    _is_conn_error,
    _parse_published_at,
    _time_weight,
    web_search,
)


# ---------------------------------------------------------------------------
# _parse_published_at：仅从 Tavily published_date 解析
# ---------------------------------------------------------------------------


def test_parse_iso_date_from_tavily():
    """Tavily 返回 ISO 日期 → 直接采用。"""
    result = _parse_published_at("2024-01-15T10:30:00Z")
    assert result is not None
    assert "2024-01-15" in result


def test_parse_date_from_tavily_with_z_suffix():
    """Tavily 返回 UTC Z 后缀日期。"""
    result = _parse_published_at("2024-06-01T00:00:00Z")
    assert result is not None
    assert "2024-06-01" in result


def test_parse_date_slash_format():
    """Tavily 返回 YYYY/MM/DD 格式。"""
    result = _parse_published_at("2024/03/15")
    assert result is not None
    assert "2024-03-15" in result


def test_parse_date_english_format():
    """Tavily 返回英文完整月份。"""
    result = _parse_published_at("March 15, 2024")
    assert result is not None
    assert "2024-03-15" in result


def test_parse_date_none_returns_none():
    """Tavily 未给 published_date → None（不尝试从 snippet 解析）。"""
    result = _parse_published_at(None)
    assert result is None


def test_parse_date_empty_string_returns_none():
    """空字符串 → None。"""
    result = _parse_published_at("")
    assert result is None


def test_parse_date_invalid_string_returns_none():
    """无法解析的字符串 → None，不崩。"""
    result = _parse_published_at("not-a-valid-date")
    assert result is None


# ---------------------------------------------------------------------------
# _detect_recency_tau：仅有时效关键词才启用
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected_tau"),
    [
        # 强时效 → τ=7
        ("最新的 RAG 研究进展", 7),
        ("最近有什么新框架", 7),
        ("刚刚发布的模型", 7),
        ("今天发生了什么", 7),
        ("近期 AI 新闻", 7),
        # 年时效 → τ=30
        ("今年最好的框架", 30),
        # 无时效诉求 → None（不重排）
        ("什么是 RAG", None),
        ("Python 和 JavaScript 的区别", None),
        ("2026 年 AI 趋势", None),     # 明确年份不是相对时效，不衰减
        ("2025 年综述", None),
        ("COVID-19 死亡人数", None),
        ("张伟的成就", None),
    ],
)
def test_detect_recency_tau(query, expected_tau):
    assert _detect_recency_tau(query) == expected_tau


# ---------------------------------------------------------------------------
# _time_weight
# ---------------------------------------------------------------------------


def test_time_weight_no_date_is_neutral():
    """无日期 → 0.5（中性）。"""
    assert _time_weight(None, tau=7) == 0.5


def test_time_weight_invalid_date_is_neutral():
    """无效日期字符串 → 0.5。"""
    assert _time_weight("not-a-date", tau=7) == 0.5


def test_time_weight_today_is_one():
    """今天发布的文章权重 ≈ 1.0。"""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).isoformat()
    w = _time_weight(today, tau=7)
    assert 0.99 < w <= 1.0, f"today weight={w}"


def test_time_weight_7_days_old_with_tau_7():
    """τ=7 天时，7 天前的权重 ≈ exp(-1) ≈ 0.368。"""
    from datetime import datetime, timedelta, timezone
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    w = _time_weight(week_ago, tau=7)
    assert 0.36 < w < 0.38, f"7d weight={w}"


def test_time_weight_old_content_penalized():
    """10 年前的内容权重极低。"""
    from datetime import datetime, timedelta, timezone
    decade_ago = (datetime.now(timezone.utc) - timedelta(days=3650)).isoformat()
    w = _time_weight(decade_ago, tau=7)
    assert w < 0.01, f"decade old weight should be near-zero: {w}"


# ---------------------------------------------------------------------------
# _combined_score
# ---------------------------------------------------------------------------


def test_combined_score():
    """score × time_weight 计算正确。"""
    from datetime import datetime, timedelta, timezone

    fresh_date = datetime.now(timezone.utc).isoformat()
    old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()

    fresh_doc = RetrievedDoc(
        title="新鲜文章", snippet="s", score=0.9, published_at=fresh_date
    )
    old_doc = RetrievedDoc(
        title="旧文章", snippet="s", score=0.9, published_at=old_date
    )

    fresh_score = _combined_score(fresh_doc, tau=7)
    old_score = _combined_score(old_doc, tau=7)

    print(f"\n[combined] fresh={fresh_score:.4f} old={old_score:.4f}")
    assert fresh_score > old_score, "同时效 τ=7 下新文章应排在前面"


# ---------------------------------------------------------------------------
# web_search 重排（mock Tavily）
# ---------------------------------------------------------------------------


def test_web_search_reranks_by_time_for_recency_query(monkeypatch):
    """时效 query（"最新"）→ 按 combined score 重排。"""
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc)
    yesterday = (today - timedelta(days=1)).isoformat()
    last_month = (today - timedelta(days=30)).isoformat()

    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {
                "url": "https://a.example/old",
                "title": "旧文章",
                "content": "...",
                "score": 0.95,
                "published_date": last_month,
            },
            {
                "url": "https://b.example/new",
                "title": "新文章",
                "content": "...",
                "score": 0.5,
                "published_date": yesterday,
            },
            {
                "url": "https://c.example/nodate",
                "title": "无日期文章",
                "content": "...",
                "score": 0.8,
            },
        ]
    }
    monkeypatch.setattr("dra.tools._get_client", lambda: mock_client)

    docs = web_search("最新的 RAG 研究", top_k=3)
    urls = [d.source_url for d in docs]
    print(f"\n[时效重排] urls={urls}")

    assert len(docs) == 3
    # 昨天的新文章即使 Tavily score 低也应排最前面（τ=7 时时效权重大）
    assert docs[0].source_url == "https://b.example/new"


def test_web_search_preserves_original_order_for_non_recency_query(monkeypatch):
    """非时效 query（"什么是 RAG"）→ 不重排，保持 Tavily 原始顺序。"""
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {
                "url": "https://a.example/first",
                "title": "第一篇",
                "content": "...",
                "score": 0.95,
                "published_date": "2020-01-01T00:00:00Z",  # 很旧
            },
            {
                "url": "https://b.example/second",
                "title": "第二篇",
                "content": "...",
                "score": 0.6,
                "published_date": "2026-06-15T00:00:00Z",  # 很新
            },
            {
                "url": "https://c.example/third",
                "title": "第三篇",
                "content": "...",
                "score": 0.5,
            },
        ]
    }
    monkeypatch.setattr("dra.tools._get_client", lambda: mock_client)

    docs = web_search("什么是 RAG", top_k=3)
    urls = [d.source_url for d in docs]
    print(f"\n[非时效保持原序] urls={urls}")

    assert len(docs) == 3
    # 非时效 query 不重排：第一篇虽旧但 Tavily score 最高，应保持第一
    assert docs[0].source_url == "https://a.example/first"


def test_web_search_fills_published_at_from_tavily(monkeypatch):
    """验证 web_search 把 Tavily 的 published_date 填进 RetrievedDoc。"""
    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {
                "url": "https://x.example",
                "title": "T",
                "content": "c",
                "score": 0.9,
                "published_date": "2025-12-01T00:00:00Z",
            },
        ]
    }
    monkeypatch.setattr("dra.tools._get_client", lambda: mock_client)

    docs = web_search("q", top_k=1)
    assert len(docs) == 1
    assert docs[0].published_at is not None
    assert "2025-12-01" in docs[0].published_at


# ---------------------------------------------------------------------------
# SSL EOF 韧性：连接级故障 → 重置 client → 新连接重试（根治 Tavily SSL EOF）
# ---------------------------------------------------------------------------


def test_is_conn_error_detects_ssl_eof():
    """Tavily 真实 SSL EOF 的异常文本应被识别为连接级故障。"""
    e = Exception(
        "HTTPSConnectionPool(host='api.tavily.com', port=443): Max retries exceeded "
        "with url: /search (Caused by SSLError(SSLEOFError(8, "
        "'[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol')))"
    )
    assert _is_conn_error(e) is True


def test_is_conn_error_follows_exception_chain():
    """根因在异常链深处（requests 层层包裹）也要能识别。"""
    try:
        try:
            raise OSError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred")
        except OSError as inner:
            raise RuntimeError("search failed") from inner
    except RuntimeError as e:
        assert _is_conn_error(e) is True


def test_is_conn_error_false_for_non_conn_error():
    """参数错 / KeyError 等非连接故障不应触发重置。"""
    assert _is_conn_error(ValueError("invalid api key")) is False
    assert _is_conn_error(KeyError("results")) is False


def test_web_search_resets_and_recovers_on_ssl_eof(monkeypatch):
    """连接级故障 → 重置 client → 下一轮拿新连接成功返回（不再连试都挂同一断连）。"""
    bad = MagicMock()
    bad.search.side_effect = Exception(
        "HTTPSConnectionPool(...): Max retries exceeded "
        "(Caused by SSLError(SSLEOFError(8, 'UNEXPECTED_EOF_WHILE_READING')))"
    )
    good = MagicMock()
    good.search.return_value = {
        "results": [{"url": "https://ok.example", "title": "T", "content": "c", "score": 0.9}]
    }
    seq = [bad, good]
    calls = {"n": 0}

    def fake_get_client():
        c = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return c

    reset = {"n": 0}
    monkeypatch.setattr("dra.tools._get_client", fake_get_client)
    monkeypatch.setattr("dra.tools._reset_client", lambda: reset.__setitem__("n", reset["n"] + 1))
    monkeypatch.setattr("dra.tools.time.sleep", lambda *a, **k: None)

    docs = web_search("q", top_k=1, max_retries=2)
    assert len(docs) == 1 and docs[0].source_url == "https://ok.example"  # 恢复成功
    assert reset["n"] >= 1   # 连接故障触发了 client 重置
    assert calls["n"] >= 2   # 用了新 client 重试


def test_web_search_non_conn_error_does_not_reset(monkeypatch):
    """非连接错误（参数错）→ 不重置 client，重试耗尽后上抛 RuntimeError。"""
    bad = MagicMock()
    bad.search.side_effect = ValueError("invalid max_results")
    reset = {"n": 0}
    monkeypatch.setattr("dra.tools._get_client", lambda: bad)
    monkeypatch.setattr("dra.tools._reset_client", lambda: reset.__setitem__("n", reset["n"] + 1))
    monkeypatch.setattr("dra.tools.time.sleep", lambda *a, **k: None)

    with pytest.raises(RuntimeError):
        web_search("q", top_k=1, max_retries=1)
    assert reset["n"] == 0   # 非连接错误不应重置连接


# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


@pytest.mark.live
def test_web_search_real_results_have_published_at():
    """真实检索：至少部分结果应能解析出 published_at。"""
    docs = web_search("2026年6月 AI 新闻", top_k=5)
    print(f"\n[live·published_at]")
    for d in docs:
        print(f"  url={d.source_url} | published_at={d.published_at}")

    filled = [d for d in docs if d.published_at]
    assert filled, "至少应有部分结果能解析出发布日期"
