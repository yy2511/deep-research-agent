"""V2-1 RAG retriever:加载落盘 index → numpy 暴力检索。

检索 = query_emb @ chunks_emb.T 取 top_k(暴力,无近似索引)。
为什么 numpy 暴力不上 FAISS:
- 语料 239 摘要 × 768 维,矩阵乘法毫秒级,暴力完全够。
- FAISS 价值在 N>100k 近似搜索(IVF/HNSW),当前规模不需要,且多一层依赖与黑盒。
- 可读性强，也便于说明「先用 numpy 暴力检索，规模破万再换 FAISS」的工程取舍。
  FAISS 留到 N 破万再上。

V2-1 先不抽 VectorIndex 类(CLAUDE.md §2:等 V2-2 ResearchMemory 第二个实现再抽,
届时 LocalCorpusIndex / ResearchMemoryIndex 共用同一套 numpy infra)。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from dra.rag.indexer import _get_encoder, tokenize


class LocalCorpusIndex:
    """local 语料的向量索引:加载落盘的 embeddings.npy + chunks.json,提供 search。

    惰性加载:构造只存路径,首次 search 时才读盘 + 加载 encoder。
    索引在进程内缓存(模块级 _INDEX_CACHE),同一 corpus_name 多次 local_rag_search
    不重复读盘——239 篇不大,缓存省的是 encoder 加载(秒级)与读盘。
    """

    def __init__(self, index_dir: str | Path):
        self.index_dir = Path(index_dir)
        self._embeddings: np.ndarray | None = None
        self._chunks: list[dict] | None = None
        self._normed: np.ndarray | None = None  # L2 归一化后的矩阵,cosine 用
        self._model: str | None = None  # 建库用的 embedding 模型,查询须同款
        # V2-4 BM25 sparse:与 dense 完全独立的惰性加载
        self._bm25 = None  # rank_bm25.BM25Okapi 实例
        self._has_bm25: bool = False

    def _load(self) -> None:
        if self._embeddings is not None:
            return
        self._embeddings = np.load(self.index_dir / "embeddings.npy")
        with open(self.index_dir / "chunks.json", encoding="utf-8") as f:
            self._chunks = json.load(f)
        # meta.json 记建库模型，保证查询 encoder 与建库 encoder 同款。
        meta_path = self.index_dir / "meta.json"
        if not meta_path.exists():
            raise RuntimeError(
                f"local 索引缺少 {meta_path.name}，无法确认 embedding 契约；请用当前 bge-m3 indexer 重建。"
            )
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        self._model = meta.get("model")
        if not isinstance(self._model, str) or "bge-m3" not in self._model.lower():
            raise RuntimeError(
                f"local 索引不是当前 bge-m3 格式（model={self._model!r}）；请重建。"
            )
        self._has_bm25 = bool(meta.get("has_bm25"))
        # L2 归一化:cosine 相似度 = 归一化向量内积。零向量兜底防除零。
        if self._embeddings.shape[0] == 0:
            self._normed = self._embeddings
        else:
            norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
            norms = np.where(norms == 0, 1.0, norms)
            self._normed = self._embeddings / norms

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """向量检索 top_k:返回 [{chunk 元数据..., score}],按相似度降序。

        空 index → 返回 []。top_k 截到实际 chunk 数。
        score = cosine 相似度(归一化内积,范围 [-1, 1])。
        """
        self._load()
        assert self._chunks is not None and self._normed is not None
        if not self._chunks or self._normed.shape[0] == 0:
            return []

        encoder = _get_encoder(self._model)
        q_vec = encoder.encode([query], show_progress_bar=False, convert_to_numpy=True)
        q_vec = q_vec.astype(np.float32)
        q_norm = np.linalg.norm(q_vec)
        if q_norm == 0:
            return []
        q_normed = q_vec / q_norm

        sims = (self._normed @ q_normed.reshape(-1)).ravel()  # (N,)
        k = min(top_k, len(self._chunks))
        # argpartition 取 top-k 再排序,比全 argsort 快;N 小其实差不多,但写法对。
        top_idx = np.argpartition(-sims, k - 1)[:k]
        top_idx = top_idx[np.argsort(-sims[top_idx])]

        return [
            {**self._chunks[i], "score": float(sims[i])}
            for i in top_idx
        ]

    # ---------- V2-4 BM25 sparse 检索 ----------

    def _load_bm25(self) -> None:
        """惰性加载 BM25Okapi:首次 search_sparse 才读 tokenized_chunks.json + 构造。

        与 dense 完全独立——纯 dense 调用零额外开销。
        无 has_bm25 / 缺文件 → 抛 RuntimeError(显式失败,不静默 fallback),
        遵守 V2-1 已有的「索引不存在直接 raise」契约。
        """
        if self._bm25 is not None:
            return
        self._load()  # 复用 dense 加载的 meta 检查
        if not self._has_bm25:
            raise RuntimeError(
                f"local 索引未建 BM25:{self.index_dir}/meta.json 无 has_bm25。"
                f"用 V2-4a 后的 indexer 重建。"
            )
        tok_path = self.index_dir / "tokenized_chunks.json"
        if not tok_path.exists():
            raise RuntimeError(
                f"local 索引 BM25 文件缺失:{tok_path}。"
                f"meta 标了 has_bm25 但文件不在,重建。"
            )
        with open(tok_path, encoding="utf-8") as f:
            tokenized = json.load(f)
        # 局部 import 避免无 BM25 需求时加载 rank_bm25
        from rank_bm25 import BM25Okapi
        self._bm25 = BM25Okapi(tokenized)

    def search_sparse(self, query: str, top_k: int = 5) -> list[dict]:
        """BM25 sparse 检索 top_k:返回 [{chunk 元数据..., score}],按分数降序。

        空 index / 空 query → 返回 []。score 是 BM25 原始分(无界,仅供排序),
        与 dense 的 cosine 不同量纲——RRF 融合用 rank 不用 score,量纲无关。
        """
        self._load_bm25()
        assert self._chunks is not None and self._bm25 is not None
        if not self._chunks:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scores = self._bm25.get_scores(q_tokens)  # np.ndarray (N,)
        k = min(top_k, len(self._chunks))
        top_idx = np.argpartition(-scores, k - 1)[:k]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        return [
            {**self._chunks[i], "score": float(scores[i])}
            for i in top_idx
        ]

    # ---------- V2-4c RRF hybrid 融合 ----------

    def search_hybrid(
        self,
        query: str,
        top_k: int = 5,
        *,
        dense_k: int = 20,
        sparse_k: int = 20,
        rrf_k: int = 60,
    ) -> list[dict]:
        """Reciprocal Rank Fusion 融合 dense + sparse 检索。

        公式(Cormack et al. 2009 SIGIR,k=60 是论文 TREC 调出的经验值,
        Elasticsearch/OpenSearch/Weaviate/Qdrant 默认都是 60):

            score(doc) = Σ 1 / (rrf_k + rank_i)

        为什么取 dense_k/sparse_k=20 而非 top_k=5:给 RRF 留候选池,让
        「dense 排 8、sparse 排 1」的文档仍能进融合;经验值,与 ES RRF retriever
        默认 window_size 一致。

        为什么只用 rank 不用 score:dense cosine ∈ [0,1] 与 BM25 无界正数量纲不同,
        归一化引入超参且不稳。RRF 用 rank 完全绕开量纲问题。

        返回 [{chunk 元数据..., score=RRF分}],score 是融合分(0.02 左右的小数),
        与 dense/sparse 的原始 score 不同语义——只用于排序。
        """
        dense_results = self.search(query, top_k=dense_k)
        sparse_results = self.search_sparse(query, top_k=sparse_k)

        rrf_scores: dict[str, float] = {}
        chunk_by_id: dict[str, dict] = {}
        # 两路结果一起处理:每条贡献 1/(rrf_k + rank);同 doc 出现两次自然累加
        for results in (dense_results, sparse_results):
            for rank, hit in enumerate(results, start=1):
                cid = hit["id"]
                rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (rrf_k + rank)
                # 元数据用第一次见到的,两路 chunk 内容相同(同一索引)
                if cid not in chunk_by_id:
                    chunk_by_id[cid] = {k: v for k, v in hit.items() if k != "score"}

        sorted_ids = sorted(rrf_scores, key=lambda c: rrf_scores[c], reverse=True)
        return [
            {**chunk_by_id[cid], "score": rrf_scores[cid]}
            for cid in sorted_ids[:top_k]
        ]


# ---------------------------------------------------------------------------
# 进程内索引缓存(按 index_dir 路径去重)
# ---------------------------------------------------------------------------

_INDEX_CACHE: dict[str, LocalCorpusIndex] = {}


def get_index(index_dir: str | Path) -> LocalCorpusIndex:
    """按路径缓存 LocalCorpusIndex 单例:同一语料多次检索不重复读盘。"""
    key = str(Path(index_dir).resolve())
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = LocalCorpusIndex(index_dir)
    return _INDEX_CACHE[key]
