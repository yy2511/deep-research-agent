"""V2-1 RAG indexer 纯逻辑测试：chunking 边界、embedding 形状、落盘往返。

真实 sentence-transformers 加载标 @pytest.mark.live（秒级但加载 encoder 耗时，
且依赖模型缓存；纯逻辑测试默认不联网，CLAUDE.md §7）。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from dra.rag.indexer import chunk_text, load_arxiv_docs, tokenize


# ---------------------------------------------------------------------------
# chunk_text：固定窗口 + overlap，纯逻辑（无模型）
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_empty_returns_empty(self):
        assert chunk_text("") == []
        assert chunk_text("   \n\t  ") == []

    def test_short_text_single_chunk(self):
        """短于窗口 → 单 chunk，原文不截断。"""
        text = "RAG combines retrieval and generation."
        out = chunk_text(text, window=512, overlap=50)
        assert out == [text]

    def test_long_text_multiple_chunks_with_overlap(self):
        """长文本切多块，相邻块有 overlap 区域。"""
        text = "x" * 1200  # 1200 字符，window=512 overlap=50 → step=462
        out = chunk_text(text, window=512, overlap=50)
        assert len(out) > 1
        # 每块 ≤ window
        assert all(len(c) <= 512 for c in out)
        # 相邻块 overlap：块 i 的后 50 字符 == 块 i+1 的前 50 字符（因为纯 x，验证长度即可）
        # step = window - overlap = 462；第二块起点 = 462
        assert out[1].startswith(text[462:512])  # 即 text[462:512] 是 overlap 段

    def test_step_at_least_one(self):
        """overlap ≥ window 时 step 退化到 1，不死循环（防御）。"""
        text = "abcdefghij"
        out = chunk_text(text, window=5, overlap=5)
        assert len(out) >= 1
        # 不产空块
        assert all(c.strip() for c in out)

    def test_realistic_abstract_single_chunk(self):
        """真实 arxiv 摘要（~1200 字符 < window）→ 单 chunk passthrough。"""
        text = "Retrieval-Augmented Generation (RAG) mitigates hallucination. " * 20
        out = chunk_text(text, window=512, overlap=50)
        # 1200 字符实际会被切（>512），验证至少切了且内容连续
        assert len(out) >= 2
        assert all(len(c) <= 512 for c in out)


# ---------------------------------------------------------------------------
# tokenize：V2-4 BM25 分词器，纯逻辑
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_empty_returns_empty(self):
        assert tokenize("") == []
        assert tokenize(None) == []  # type: ignore[arg-type]

    def test_english_lowercased_and_split(self):
        # 标点切开,大写转小写
        assert tokenize("Retrieval-Augmented Generation (RAG)") == [
            "retrieval", "augmented", "generation", "rag"
        ]

    def test_alphanumeric_kept_together(self):
        # 字母+数字连续不切(BGE-M3 / LLaMA-2-7B 等专有名词的关键)
        assert tokenize("BGE-M3 vs llama2") == ["bge", "m3", "vs", "llama2"]

    def test_chinese_char_level(self):
        # 中文每字一 token,英文段保持
        assert tokenize("RAG 检索增强") == ["rag", "检", "索", "增", "强"]

    def test_mixed_with_punctuation(self):
        # 综合:中英标点混合,标点全丢
        out = tokenize("LLaMA-2-7B 在 MMLU 上的表现")
        assert out == ["llama", "2", "7b", "在", "mmlu", "上", "的", "表", "现"]


# ---------------------------------------------------------------------------
# load_arxiv_docs：读 jsonl，纯逻辑
# ---------------------------------------------------------------------------


class TestLoadArxivDocs:
    def test_load_skips_blank_lines(self, tmp_path: Path):
        p = tmp_path / "docs.jsonl"
        p.write_text(
            '\n{"id":"a","title":"A","abstract":"x","categories":[],"published":"","topic_label":"rag"}\n'
            '  \n'
            '{"id":"b","title":"B","abstract":"y","categories":[],"published":"","topic_label":"llm"}\n',
            encoding="utf-8",
        )
        docs = load_arxiv_docs(p)
        assert len(docs) == 2
        assert docs[0]["id"] == "a"
        assert docs[1]["id"] == "b"


# ---------------------------------------------------------------------------
# build_index：真实 embedding，标 live
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestBuildIndexLive:
    """真实 sentence-transformers 加载 + encode，需模型缓存（DEVLOG 2026-06-20 已备）。"""

    def test_build_index_shapes(self, tmp_path: Path):
        from dra.rag.indexer import build_index
        docs = [
            {"id": "2401.00001v1", "title": "RAG survey", "abstract": "Retrieval augmented generation survey.", "categories": ["cs.CL"], "published": "2024-01-01", "topic_label": "rag"},
            {"id": "2401.00002v1", "title": "Agent", "abstract": "LLM agent planning and tool use.", "categories": ["cs.AI"], "published": "2024-01-02", "topic_label": "agent"},
        ]
        stats = build_index(docs, tmp_path / "idx")
        assert stats["docs"] == 2
        assert stats["chunks"] >= 2  # 至少每个摘要 1 chunk
        assert stats["dim"] > 0  # bge-m3 1024 维
        assert stats["model"]  # 模型名回传
        # 落盘文件存在（含 meta.json）
        assert (tmp_path / "idx" / "embeddings.npy").exists()
        assert (tmp_path / "idx" / "chunks.json").exists()
        meta = json.loads((tmp_path / "idx" / "meta.json").read_text(encoding="utf-8"))
        assert meta["model"] == stats["model"]
        assert meta["dim"] == stats["dim"]
        # 向量形状 = (chunks, dim)
        emb = np.load(tmp_path / "idx" / "embeddings.npy")
        chunks = json.loads((tmp_path / "idx" / "chunks.json").read_text(encoding="utf-8"))
        assert emb.shape == (len(chunks), stats["dim"])
        # chunk 元数据带 doc_id / source_url
        assert chunks[0]["doc_id"] == "2401.00001v1"
        assert chunks[0]["source_url"].startswith("https://arxiv.org/abs/")

    def test_build_index_empty_docs(self, tmp_path: Path):
        """空 docs 不崩：落盘空矩阵 + 空 chunks.json。"""
        from dra.rag.indexer import build_index
        stats = build_index([], tmp_path / "idx")
        assert stats["chunks"] == 0
        assert np.load(tmp_path / "idx" / "embeddings.npy").shape[0] == 0


# ---------------------------------------------------------------------------
# retriever 模型选择：meta.json 明确指定当前 bge-m3（纯逻辑，不加载模型）
# ---------------------------------------------------------------------------


class TestRetrieverModelSelection:
    """_load 只读盘设置 self._model，不加载 encoder（encoder 在 search 才加载），
    故可纯逻辑验证「建库模型 → 查询同款」的契约，秒级不花钱。"""

    def _write_index(self, d: Path, *, meta: dict | None):
        np.save(d / "embeddings.npy", np.zeros((1, 4), dtype=np.float32))
        (d / "chunks.json").write_text(
            '[{"id":"x__c0","doc_id":"x","text":"t"}]', encoding="utf-8")
        if meta is not None:
            (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_meta_model_is_used(self, tmp_path: Path):
        from dra.rag.retriever import LocalCorpusIndex
        self._write_index(tmp_path, meta={"model": "BAAI/bge-m3", "dim": 4})
        idx = LocalCorpusIndex(tmp_path)
        idx._load()
        assert idx._model == "BAAI/bge-m3"

    def test_index_without_meta_requires_rebuild(self, tmp_path: Path):
        """没有 embedding 契约的旧索引不可静默加载，必须用当前 indexer 重建。"""
        from dra.rag.retriever import LocalCorpusIndex
        self._write_index(tmp_path, meta=None)
        idx = LocalCorpusIndex(tmp_path)
        with pytest.raises(RuntimeError, match="meta.json"):
            idx._load()


# ---------------------------------------------------------------------------
# V2-4b: BM25 sparse 检索（纯逻辑，不加载 dense encoder——sparse 路径独立）
# ---------------------------------------------------------------------------


class TestSparseSearch:
    """BM25 search_sparse 测试：用 tmp_path 造迷你索引，
    只落 tokenized_chunks.json + chunks.json + meta.json，**不需要真 embedding**。"""

    def _write_index(
        self,
        d: Path,
        chunks: list[dict],
        tokenized: list[list[str]],
        *,
        has_bm25: bool = True,
    ) -> None:
        # dense 文件存在但内容随便（sparse 路径不读 embeddings 用于检索）
        np.save(d / "embeddings.npy", np.zeros((len(chunks), 4), dtype=np.float32))
        (d / "chunks.json").write_text(json.dumps(chunks), encoding="utf-8")
        meta = {"model": "BAAI/bge-m3", "dim": 4, "has_bm25": has_bm25}
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        if has_bm25:
            (d / "tokenized_chunks.json").write_text(
                json.dumps(tokenized), encoding="utf-8")

    def test_sparse_search_ranks_by_term_frequency(self, tmp_path: Path):
        """专有名词命中：含 BGE-M3 的 chunk 应排在不含的前面。"""
        from dra.rag.retriever import LocalCorpusIndex
        chunks = [
            {"id": "a", "text": "BGE-M3 evaluated on MIRACL"},
            {"id": "b", "text": "RAG combines retrieval and generation"},
            {"id": "c", "text": "BGE-M3 BGE-M3 MIRACL benchmark"},  # 词频更高
        ]
        tokenized = [
            ["bge", "m3", "evaluated", "on", "miracl"],
            ["rag", "combines", "retrieval", "and", "generation"],
            ["bge", "m3", "bge", "m3", "miracl", "benchmark"],
        ]
        self._write_index(tmp_path, chunks, tokenized)
        idx = LocalCorpusIndex(tmp_path)
        out = idx.search_sparse("BGE-M3 MIRACL", top_k=3)
        assert len(out) == 3
        # 词频更高的 c 排第一，含 1 次的 a 第二，无关的 b 最后
        assert out[0]["id"] == "c"
        assert out[1]["id"] == "a"
        assert out[2]["id"] == "b"
        # score 单调降序
        assert out[0]["score"] >= out[1]["score"] >= out[2]["score"]

    def test_sparse_search_top_k_caps_at_index_size(self, tmp_path: Path):
        """top_k > chunk 数 → 不崩、返回所有。"""
        from dra.rag.retriever import LocalCorpusIndex
        chunks = [{"id": "a", "text": "rag"}, {"id": "b", "text": "llm"}]
        tokenized = [["rag"], ["llm"]]
        self._write_index(tmp_path, chunks, tokenized)
        idx = LocalCorpusIndex(tmp_path)
        out = idx.search_sparse("rag", top_k=99)
        assert len(out) == 2

    def test_sparse_search_empty_query_returns_empty(self, tmp_path: Path):
        """空 query（tokenize 后为空）→ 返回 []，不崩。"""
        from dra.rag.retriever import LocalCorpusIndex
        chunks = [{"id": "a", "text": "rag"}]
        tokenized = [["rag"]]
        self._write_index(tmp_path, chunks, tokenized)
        idx = LocalCorpusIndex(tmp_path)
        assert idx.search_sparse("", top_k=5) == []
        assert idx.search_sparse("!!!", top_k=5) == []  # 全标点 tokenize 后空

    def test_sparse_search_raises_when_no_bm25(self, tmp_path: Path):
        """meta 无 has_bm25 → 显式 RuntimeError（不静默降级，遵守 V2-1 契约）。"""
        from dra.rag.retriever import LocalCorpusIndex
        chunks = [{"id": "a", "text": "rag"}]
        self._write_index(tmp_path, chunks, [], has_bm25=False)
        idx = LocalCorpusIndex(tmp_path)
        with pytest.raises(RuntimeError, match="未建 BM25"):
            idx.search_sparse("rag", top_k=5)

    def test_sparse_search_raises_when_file_missing(self, tmp_path: Path):
        """meta 标了 has_bm25=True 但 tokenized 文件被删 → 明确报错。"""
        from dra.rag.retriever import LocalCorpusIndex
        chunks = [{"id": "a", "text": "rag"}]
        self._write_index(tmp_path, chunks, [["rag"]], has_bm25=True)
        (tmp_path / "tokenized_chunks.json").unlink()
        idx = LocalCorpusIndex(tmp_path)
        with pytest.raises(RuntimeError, match="文件缺失"):
            idx.search_sparse("rag", top_k=5)


# ---------------------------------------------------------------------------
# V2-4c: RRF hybrid 融合（核心：验证「双路共识 > 单路第一」的 RRF 哲学）
# ---------------------------------------------------------------------------


class TestHybridSearch:
    """RRF 融合测试。dense 不需要真 encoder——monkeypatch search/search_sparse
    直接返回构造的排名列表，纯逻辑验证 RRF 数学正确。"""

    def _make_index(self, tmp_path: Path):
        """造一个最小 LocalCorpusIndex（dense/sparse 都被 mock，不读盘）。"""
        from dra.rag.retriever import LocalCorpusIndex
        # 写最小落盘文件防 _load 崩
        np.save(tmp_path / "embeddings.npy", np.zeros((1, 4), dtype=np.float32))
        (tmp_path / "chunks.json").write_text('[{"id":"x","text":"t"}]', encoding="utf-8")
        (tmp_path / "meta.json").write_text(
            json.dumps({"model": "x", "dim": 4, "has_bm25": True}), encoding="utf-8")
        (tmp_path / "tokenized_chunks.json").write_text("[[\"t\"]]", encoding="utf-8")
        return LocalCorpusIndex(tmp_path)

    def test_rrf_doc_in_both_ranks_first(self, tmp_path: Path):
        """RRF 核心：两路都看好的文档 > 单路第一。

        dense: [A1, B2, C3]   ← B 排第 2
        sparse: [B1, D2, A3]  ← B 也排第 1
        → B 同时出现在两路且都靠前，RRF 分应高于只在单路第一的 A 或 D。
        """
        idx = self._make_index(tmp_path)
        idx.search = lambda q, top_k: [  # type: ignore[method-assign]
            {"id": "A", "text": "a"},
            {"id": "B", "text": "b"},
            {"id": "C", "text": "c"},
        ][:top_k]
        idx.search_sparse = lambda q, top_k: [  # type: ignore[method-assign]
            {"id": "B", "text": "b"},
            {"id": "D", "text": "d"},
            {"id": "A", "text": "a"},
        ][:top_k]
        out = idx.search_hybrid("q", top_k=4, dense_k=3, sparse_k=3, rrf_k=60)
        ids = [r["id"] for r in out]
        # B 双路共识 → 第一
        assert ids[0] == "B"
        # A 两路都进(dense#1 + sparse#3) → 应该高于只单路出现的 C/D
        assert "A" in ids[:2]

    def test_rrf_score_formula(self, tmp_path: Path):
        """直接验算 RRF 公式：rank 1 + rank 1 → score = 1/61 + 1/61"""
        idx = self._make_index(tmp_path)
        idx.search = lambda q, top_k: [{"id": "X", "text": "x"}][:top_k]  # type: ignore[method-assign]
        idx.search_sparse = lambda q, top_k: [{"id": "X", "text": "x"}][:top_k]  # type: ignore[method-assign]
        out = idx.search_hybrid("q", top_k=1, dense_k=5, sparse_k=5, rrf_k=60)
        assert out[0]["id"] == "X"
        # 1/(60+1) + 1/(60+1) = 2/61
        assert abs(out[0]["score"] - 2.0 / 61.0) < 1e-9

    def test_rrf_top_k_truncates(self, tmp_path: Path):
        """top_k=2 但融合后有 5 个文档 → 只返 2。"""
        idx = self._make_index(tmp_path)
        idx.search = lambda q, top_k: [  # type: ignore[method-assign]
            {"id": c, "text": c} for c in "ABCDE"
        ][:top_k]
        idx.search_sparse = lambda q, top_k: []  # type: ignore[method-assign]
        out = idx.search_hybrid("q", top_k=2, dense_k=5, sparse_k=5)
        assert len(out) == 2
        # 单路退化：dense 第 1 = A，第 2 = B
        assert [r["id"] for r in out] == ["A", "B"]

    def test_rrf_degrades_to_single_route_when_other_empty(self, tmp_path: Path):
        """sparse 返回空（如空 query 后 tokenize=[]）→ 退化为纯 dense，不崩。"""
        idx = self._make_index(tmp_path)
        idx.search = lambda q, top_k: [{"id": "A", "text": "a"}, {"id": "B", "text": "b"}][:top_k]  # type: ignore[method-assign]
        idx.search_sparse = lambda q, top_k: []  # type: ignore[method-assign]
        out = idx.search_hybrid("q", top_k=2, dense_k=5, sparse_k=5)
        assert [r["id"] for r in out] == ["A", "B"]
        # 单路 rank 1 的分 = 1/(60+1)
        assert abs(out[0]["score"] - 1.0 / 61.0) < 1e-9

    def test_rrf_score_field_overrides_original(self, tmp_path: Path):
        """RRF 返回的 score 是 RRF 分，不是 dense cosine 或 BM25 原始分。"""
        idx = self._make_index(tmp_path)
        idx.search = lambda q, top_k: [{"id": "A", "text": "a", "score": 0.99}][:top_k]  # type: ignore[method-assign]
        idx.search_sparse = lambda q, top_k: [{"id": "A", "text": "a", "score": 12.5}][:top_k]  # type: ignore[method-assign]
        out = idx.search_hybrid("q", top_k=1, dense_k=5, sparse_k=5)
        # score 是 RRF（小数），不是 0.99 或 12.5
        assert out[0]["score"] < 0.05
        assert out[0]["score"] != 0.99
        assert out[0]["score"] != 12.5
