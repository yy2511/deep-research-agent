"""引用深链：一半靠我们自己的数据（本地快照高亮），一半靠浏览器特性（原网页 text fragment）。

覆盖点：
1. 原网页深链的生成规则（两端 `start,end`、百分号编码、去掉旧 fragment）；
2. 不适用的三种情况（非 http(s)、引用太短、全是空白）→ 返回 None，由前端退回普通链接；
3. 本地快照高亮：`?hl=a-b` 返回带 `<mark>` 的 HTML 页，且**内容与快照逐字一致**；
4. 不带 `hl` 时行为不变（纯文本）——既有消费者不受影响；
5. 非法 `hl` 明确拒收，不静默返回一个错的页面。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dra import simple_research as sr
from dra import web
from dra.deeplink import text_fragment_url

from test_web_research_e2e import (  # noqa: F401  pytest fixture + 真实 HTTP 入口夹具
    QUERY,
    _install_boundaries,
    _wait_done,
    isolated_web,
)
from test_simple_research import _run, _turn


def test_text_fragment_uses_both_ends_and_percent_encodes():
    url = "https://example.org/a?b=1"
    quote = "".join(
        f"第{index}段：sqlite3_rsync 通过 SSH 同步数据库，这一段用于测试两端截取与编码。"
        for index in range(6)
    )
    link = text_fragment_url(url, quote)
    assert link is not None
    assert link.startswith("https://example.org/a?b=1#:~:text=")
    directive = link.split("#:~:text=", 1)[1]
    start, end = directive.split(",")
    assert "%" in start, "必须百分号编码（否则逗号/破折号会被当成语法）"
    # 两端形态：start 是开头片段、end 是结尾片段，且各自有内容
    from urllib.parse import unquote

    assert quote.startswith(unquote(start))
    assert quote.endswith(unquote(end))
    assert len(unquote(start)) < len(quote) and len(unquote(end)) < len(quote), (
        "两端各取一段，不是整段塞进 URL"
    )


def test_short_quote_uses_a_single_directive():
    """引用短于两端阈值时不拼 `start,end`（整段就是 start）——语法上仍然合法。"""
    link = text_fragment_url("https://example.org/a", "一段刚好够长的引用")
    assert link is not None
    assert "," not in link.split("#:~:text=", 1)[1]


def test_text_fragment_drops_existing_fragment():
    link = text_fragment_url("https://example.org/a#section", "一段足够长的引用文字，用于测试。")
    assert link is not None
    assert "#section" not in link
    assert link.startswith("https://example.org/a#:~:text=")


@pytest.mark.parametrize(
    "url,quote",
    [
        ("ftp://example.org/a", "一段足够长的引用文字"),
        ("", "一段足够长的引用文字"),
        ("https://example.org/a", "短"),
        ("https://example.org/a", "   \n  "),
    ],
)
def test_text_fragment_returns_none_when_not_applicable(url, quote):
    assert text_fragment_url(url, quote) is None


def test_card_carries_deep_link_when_quote_is_long_enough(tmp_path):
    """卡片自带原网页深链；引用太短时留空，前端退回普通链接。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
        _turn(
            (
                "save_evidence",
                {"cards": [{"claim": "c", "source_ref": "s2", "excerpt_no": 1}]},
            )
        ),
    ]
    result, _script = _run(turns, run_dir=tmp_path / "run")
    card = result.cards[0]
    assert card.deep_link.startswith("https://sqlite.org/rsync.html#:~:text=")
    assert card.quote[:20] in card.deep_link or "%" in card.deep_link


def _finished_run(client) -> tuple[str, dict]:
    """走真实 HTTP 入口跑完一次 simple 运行，返回 (run_id, simple 结果)。"""
    run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()["run_id"]
    _wait_done(client, run_id)
    return run_id, client.get(f"/api/runs/{run_id}/simple").json()


def test_card_links_go_to_the_source_and_highlight_the_cited_range(isolated_web, monkeypatch):  # noqa: F811
    """端到端：卡片给两个链接，且"本地快照高亮"的区间与卡片 quote **逐字一致**。"""
    import html as _html

    _install_boundaries(monkeypatch)
    with TestClient(web.app) as client:
        run_id, payload = _finished_run(client)
        card = payload["cards"][0]
        ref = card["source_ref"]
        # ① 原网页深链（依赖浏览器特性，失败会静默降级，所以只是"给了"）
        assert card["deep_link"].startswith(card["source_url"].split("#")[0] + "#:~:text=")
        # ② 本地快照高亮（我们自己的数据，必须精确）
        start, stop = map(int, card["location"][6:].split("-"))
        plain = client.get(f"/api/runs/{run_id}/sources/{ref}")
        assert plain.text[start:stop] == card["quote"], "高亮区间必须就是卡片引用的那段原文"
        highlighted = client.get(f"/api/runs/{run_id}/sources/{ref}?hl={start}-{stop}")
        assert highlighted.status_code == 200
        assert "<mark" in highlighted.text
        assert _html.escape(card["quote"]) in highlighted.text
        # 不带 hl：行为不变（纯文本），既有消费者不受影响
        assert "text/html" not in plain.headers["content-type"]


def test_snapshot_endpoint_rejects_bad_highlight_range(isolated_web, monkeypatch):  # noqa: F811
    _install_boundaries(monkeypatch)
    with TestClient(web.app) as client:
        run_id, payload = _finished_run(client)
        ref = payload["cards"][0]["source_ref"]
        assert client.get(f"/api/runs/{run_id}/sources/{ref}?hl=oops").status_code == 400
        assert client.get(f"/api/runs/{run_id}/sources/{ref}?hl=0-999999").status_code == 400
