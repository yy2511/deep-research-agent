"""V2-1 local_rag_search 工具 + subagent 融合路由测试。

local_rag_search 真实检索标 live（依赖建好的 index + 模型缓存）；
_retrieve 路由的「默认 sources / 融合精确去重」用 mock web_search/local_rag_search 测纯逻辑。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from dra.models import RetrievedDoc
from dra.subagent import SubAgentConfig, _retrieve


def _web_doc(url: str, snippet: str) -> RetrievedDoc:
    return RetrievedDoc(source_url=url, title="t", snippet=snippet, score=0.5)


def _local_doc(url: str, snippet: str) -> RetrievedDoc:
    return RetrievedDoc(source_url=url, title="t", snippet=snippet, score=0.6)


class TestSubAgentConfigDefault:
    def test_default_sources_is_web_only(self):
        """默认 sources=['web']，不破坏现有 5 seed 回归基线。"""
        cfg = SubAgentConfig()
        assert cfg.sources == ["web"]

    def test_sources_local_only(self):
        cfg = SubAgentConfig(sources=["local"])
        assert cfg.sources == ["local"]

    def test_sources_fusion(self):
        cfg = SubAgentConfig(sources=["web", "local"])
        assert cfg.sources == ["web", "local"]


class TestRetrieveRouting:
    """用 mock 测 _retrieve 路由：调对了工具、融合精确去重。"""

    def test_web_only_calls_web_search(self):
        cfg = SubAgentConfig(sources=["web"], search_top_k=3)
        with patch("dra.subagent.web_search", return_value=[_web_doc("https://a.com", "s1")]) as mw, \
             patch("dra.subagent.local_rag_search", return_value=[]) as ml:
            docs = _retrieve("q", cfg)
        assert mw.called and not ml.called
        assert len(docs) == 1

    def test_local_only_calls_local_rag(self):
        cfg = SubAgentConfig(sources=["local"], search_top_k=3)
        with patch("dra.subagent.web_search", return_value=[]) as mw, \
             patch("dra.subagent.local_rag_search", return_value=[_local_doc("https://arxiv.org/abs/1", "s2")]) as ml:
            docs = _retrieve("q", cfg)
        assert ml.called and not mw.called

    def test_fusion_merges_both_sources(self):
        cfg = SubAgentConfig(sources=["web", "local"], search_top_k=3)
        with patch("dra.subagent.web_search", return_value=[_web_doc("https://a.com", "web snippet")]), \
             patch("dra.subagent.local_rag_search", return_value=[_local_doc("https://arxiv.org/abs/1", "local snippet")]):
            docs = _retrieve("q", cfg)
        assert len(docs) == 2

    def test_fusion_exact_dedup_same_url_same_snippet(self):
        """融合精确去重：同 (url, snippet 前缀) 只保留先出现的。"""
        cfg = SubAgentConfig(sources=["web", "local"], search_top_k=3)
        # web 和 local 返回同 url 同 snippet → 去重成 1 条
        with patch("dra.subagent.web_search", return_value=[_web_doc("https://x.com", "same snippet content")]), \
             patch("dra.subagent.local_rag_search", return_value=[_local_doc("https://x.com", "same snippet content")]):
            docs = _retrieve("q", cfg)
        assert len(docs) == 1  # 精确去重生效

    def test_fusion_keeps_different_sources_same_topic(self):
        """同主题但不同 url 的 web+local 文档都保留（不上语义去重，不传 P2-2 缺陷）。"""
        cfg = SubAgentConfig(sources=["web", "local"], search_top_k=3)
        with patch("dra.subagent.web_search", return_value=[_web_doc("https://a.com", "RAG reduces hallucination")]), \
             patch("dra.subagent.local_rag_search", return_value=[_local_doc("https://arxiv.org/abs/1", "RAG reduces hallucination")]):
            docs = _retrieve("q", cfg)
        # 不同 url → 都保留（语义相同但来源不同，是合理证据，不该被去重）
        assert len(docs) == 2

    def test_empty_sources_defaults_to_web(self):
        """sources=[] 兜底走 web（防御）。"""
        cfg = SubAgentConfig(sources=[])
        with patch("dra.subagent.web_search", return_value=[_web_doc("https://a.com", "s")]) as mw, \
             patch("dra.subagent.local_rag_search", return_value=[]) as ml:
            _retrieve("q", cfg)
        assert mw.called and not ml.called


# ---------------------------------------------------------------------------
# local_rag_search：真实检索标 live
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestLocalRagSearchLive:
    """依赖 data/corpus/arxiv/index 已建库（V2-1a/b 产物）。"""

    def test_returns_local_retrieved_docs(self):
        from dra.tools import local_rag_search
        docs = local_rag_search("RAG retrieval augmented generation", top_k=3)
        assert len(docs) <= 3
        assert len(docs) > 0
        # raw_content 非空（save_evidence 的 quote 核验靠它）
        assert all(d.raw_content for d in docs)
        # 分数降序
        scores = [d.score for d in docs]
        assert scores == sorted(scores, reverse=True)

    def test_missing_index_raises(self, tmp_path):
        """索引不存在 → 抛 RuntimeError，不静默降级（诚实优于假装成功）。"""
        from dra.tools import local_rag_search
        with pytest.raises(RuntimeError, match="未建库"):
            local_rag_search("q", top_k=3, corpus_dir=tmp_path / "nonexistent")
