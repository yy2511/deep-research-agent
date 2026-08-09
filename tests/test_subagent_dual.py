"""tests/test_subagent_dual.py — _retrieve 双源并发测试(Plan C 第一刀)。

覆盖 Plan Step 6 的 4 个 case:
1. 双源合并去重:Tavily + Exa 各返 N 篇,URL 重复部分合并后正确去重
2. 单源故障容错:Exa raise 时 Tavily 仍能跑通,不阻塞业务
3. 两源全挂:抛 RuntimeError(对齐 web_search 单源契约)
4. search_top_k_per_source 参数透传:各源以正确 top_k 被调用
"""

import pytest

from dra.models import RetrievedDoc
from dra.subagent import SubAgentConfig, _retrieve


def _doc(url: str, title: str = "t", snippet: str = "s") -> RetrievedDoc:
    return RetrievedDoc(
        source_url=url,
        title=title,
        snippet=snippet,
    )


# ---------------------------------------------------------------------------
# 1. 双源合并去重
# ---------------------------------------------------------------------------


def test_subagent_dual_source_merges_and_dedupes(monkeypatch):
    """Tavily 返 3 篇,Exa 返 3 篇,1 URL+snippet 头重复 → 合并后 5 篇。

    去重 key 是 (source_url, snippet[:80]),tavily 的 https://shared.com 和 exa
    的 https://shared.com 同 snippet 头 → 视为同文档,后到的 Exa 那条被丢。
    """
    tavily_docs = [
        _doc("https://t1.com", "T1", "snippet text for t1 lorem ipsum"),
        _doc("https://shared.com", "TS", "shared content begins with this text"),
        _doc("https://t2.com", "T2", "snippet text for t2 different content"),
    ]
    exa_docs = [
        # 这条跟 tavily 的 shared.com snippet 头一样,会被精确去重丢掉
        _doc("https://shared.com", "ES", "shared content begins with this text"),
        _doc("https://e1.com", "E1", "exa first result text"),
        _doc("https://e2.com", "E2", "exa second result text"),
    ]
    monkeypatch.setattr("dra.subagent.web_search", lambda *a, **k: tavily_docs)
    monkeypatch.setattr("dra.subagent.exa_search", lambda *a, **k: exa_docs)

    config = SubAgentConfig()  # 默认 sources=["web"], search_top_k_per_source=4
    docs = _retrieve("test query", config, verbose=False)

    # 6 篇原始 - 1 重复 = 5 篇
    assert len(docs) == 5
    urls = [d.source_url for d in docs]
    # Tavily 顺序在前(_retrieve 用 futures list 顺序,Tavily 先 submit)
    assert "https://t1.com" in urls
    assert "https://shared.com" in urls
    assert "https://t2.com" in urls
    assert "https://e1.com" in urls
    assert "https://e2.com" in urls
    # shared.com 应该只出现 1 次(tavily 的那条)
    assert urls.count("https://shared.com") == 1


# ---------------------------------------------------------------------------
# 2. 单源故障容错:Exa raise,Tavily 继续
# ---------------------------------------------------------------------------


def test_subagent_exa_fails_tavily_still_runs(monkeypatch):
    """Exa 抛 ConnectionError,Tavily 返 3 篇 → _retrieve 返 3 篇(不阻塞)。"""
    tavily_docs = [_doc(f"https://t{i}.com") for i in range(3)]
    def _exa_boom(*a, **k):
        raise ConnectionError("exa network down")
    monkeypatch.setattr("dra.subagent.web_search", lambda *a, **k: tavily_docs)
    monkeypatch.setattr("dra.subagent.exa_search", _exa_boom)

    config = SubAgentConfig()
    docs = _retrieve("test query", config, verbose=False)

    assert len(docs) == 3  # Tavily 那 3 篇被采用
    assert all("t" in (d.source_url or "") for d in docs)


def test_subagent_tavily_fails_exa_still_runs(monkeypatch):
    """对称:Tavily 抛错,Exa 返 3 篇 → 拿到 3 篇。"""
    exa_docs = [_doc(f"https://e{i}.com") for i in range(3)]
    def _tavily_boom(*a, **k):
        raise RuntimeError("tavily SSL EOF")
    monkeypatch.setattr("dra.subagent.web_search", _tavily_boom)
    monkeypatch.setattr("dra.subagent.exa_search", lambda *a, **k: exa_docs)

    config = SubAgentConfig()
    docs = _retrieve("test query", config, verbose=False)

    assert len(docs) == 3
    assert all("e" in (d.source_url or "") for d in docs)


# ---------------------------------------------------------------------------
# 3. 两源全挂:抛 RuntimeError
# ---------------------------------------------------------------------------


def test_subagent_both_sources_fail_raises(monkeypatch):
    """两家都抛错、DDG 第三级兜底也挂 → _retrieve 抛 RuntimeError("all sources failed: ...")。

    Task 7 引入 DDG 第三级 fallback（默认开）：双源全挂后会先试 DDG 保底，
    仍失败才真正 raise。这里连 DDG 一并 mock 失败，保住本测试「彻底无源可用
    才硬失败」的原意，同时避免真连网（DDG 未 mock 会打真实网络请求）。
    """
    def _tavily_boom(*a, **k):
        raise ConnectionError("tavily down")
    def _exa_boom(*a, **k):
        raise RuntimeError("exa 503")
    def _ddg_boom(*a, **k):
        raise RuntimeError("ddg down")
    monkeypatch.setattr("dra.subagent.web_search", _tavily_boom)
    monkeypatch.setattr("dra.subagent.exa_search", _exa_boom)
    monkeypatch.setattr("dra.subagent.ddg_search", _ddg_boom)

    config = SubAgentConfig()
    with pytest.raises(RuntimeError, match="all sources failed"):
        _retrieve("test query", config, verbose=False)


# ---------------------------------------------------------------------------
# 4. search_top_k_per_source 参数透传
# ---------------------------------------------------------------------------


def test_subagent_top_k_per_source_respected(monkeypatch):
    """search_top_k_per_source=2 时,Tavily/Exa 各以 top_k=2 调用。"""
    captured_tavily = {}
    captured_exa = {}
    def _track_tavily(query, top_k=None, **k):
        captured_tavily["query"] = query
        captured_tavily["top_k"] = top_k
        return []
    def _track_exa(query, top_k=None, **k):
        captured_exa["query"] = query
        captured_exa["top_k"] = top_k
        return []
    monkeypatch.setattr("dra.subagent.web_search", _track_tavily)
    monkeypatch.setattr("dra.subagent.exa_search", _track_exa)

    config = SubAgentConfig(search_top_k_per_source=2)
    _retrieve("test query", config, verbose=False)

    assert captured_tavily == {"query": "test query", "top_k": 2}
    assert captured_exa == {"query": "test query", "top_k": 2}


def test_subagent_explicit_sources_local_only_skips_web(monkeypatch):
    """sources=["local"] 不应调 web_search / exa_search。"""
    web_called = []
    exa_called = []
    monkeypatch.setattr("dra.subagent.web_search",
                        lambda *a, **k: web_called.append(1) or [])
    monkeypatch.setattr("dra.subagent.exa_search",
                        lambda *a, **k: exa_called.append(1) or [])
    monkeypatch.setattr("dra.subagent.local_rag_search",
                        lambda *a, **k: [_doc("local://doc1")])

    config = SubAgentConfig(sources=["local"])
    docs = _retrieve("q", config, verbose=False)

    assert len(docs) == 1
    assert docs[0].source_url == "local://doc1"
    assert web_called == []
    assert exa_called == []


# 注:sources=[] 被 `or ["web"]` 兜底成默认 web 路径(防忘配语义安全),
# 不视为"显式空"——这是 SubAgentConfig 设计的有意为之。
