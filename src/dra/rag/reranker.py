"""V2-5 cross-encoder 重排序：检索三件套的第三件（dense + sparse + rerank）。

为什么要 rerank：
- dense/sparse 召回是「双塔」——query 和 doc 各自编码成向量再算相似度，快但糙
  （两边独立编码，看不到对方）。
- cross-encoder 把 (query, doc) 拼一起喂进模型，让两者在注意力层充分交互，
  判别力强得多，但慢——只能对召回的 top-N 精排，不能扫全库。
- 标准两阶段：粗筛（dense+sparse 出 top-N，要快要全）→ 精排（cross-encoder 重排，要准）。

为什么选 bge-reranker-v2-m3：
- 跨语言最佳（MIRACL nDCG@10 69.32，> jina-v3 的 66.50）——我们真实任务是中文 query。
- 与 bge-m3 同家（dense/sparse/rerank 全 BGE 系，一致）。
- 能用已装的 sentence-transformers CrossEncoder 直接加载——零新依赖
  （不碰 FlagEmbedding 的 26 个传染依赖）。0.6B / 2.3GB。
"""
from __future__ import annotations

from pathlib import Path

# 本地权重路径（ModelScope 下载，离线确定加载）；不存在则回退 hub id。
_LOCAL_RERANKER = (
    Path(__file__).resolve().parents[3]
    / "data" / "models" / "ms_cache" / "BAAI" / "bge-reranker-v2-m3"
)
_DEFAULT_RERANKER = str(_LOCAL_RERANKER) if _LOCAL_RERANKER.exists() else "BAAI/bge-reranker-v2-m3"

_rerankers: dict[str, object] = {}  # 按模型名缓存（与 encoder 同套路，惰性单例）


def _get_reranker(model_name: str | None = None):
    """惰性加载并按名缓存 CrossEncoder。

    max_length=512：bge-reranker-v2-m3 推荐微调长度（model card），超长截断。
    首次试离线缓存，缺失再联网下载一次。
    """
    name = model_name or _DEFAULT_RERANKER
    if name not in _rerankers:
        from sentence_transformers import CrossEncoder
        try:
            _rerankers[name] = CrossEncoder(name, max_length=512, local_files_only=True)
        except Exception:
            _rerankers[name] = CrossEncoder(name, max_length=512)
    return _rerankers[name]


def rerank(
    query: str,
    candidates: list[dict],
    *,
    top_k: int = 10,
    text_key: str = "text",
    model_name: str | None = None,
) -> list[dict]:
    """对召回候选用 cross-encoder 精排，返回 top_k（按 rerank 分降序）。

    candidates: 召回阶段（dense/sparse/hybrid）返回的 [{..., text, ...}] 列表。
    每条取 candidates[i][text_key] 与 query 组成对，CrossEncoder 打分。
    返回的每条加 "rerank_score" 字段（sigmoid 后的相关性，越大越相关）；
    原 "score"（召回分）保留供对照。

    空候选 / 空 query → 返回 []（不崩）。top_k 截到候选数。
    """
    if not candidates or not query:
        return []
    ce = _get_reranker(model_name)
    pairs = [(query, c.get(text_key, "")) for c in candidates]
    # CrossEncoder.predict 返回每对的相关性 logit（bge-reranker 已 sigmoid 到 [0,1]）
    scores = ce.predict(pairs, show_progress_bar=False)
    scored = [
        {**c, "rerank_score": float(s)}
        for c, s in zip(candidates, scores)
    ]
    scored.sort(key=lambda x: x["rerank_score"], reverse=True)
    return scored[:top_k]
