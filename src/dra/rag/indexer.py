"""V2-1 RAG indexer:文档加载 → chunking → embedding → 落盘。

管道四步（经典 RAG）:
1. 加载:从 docs.jsonl 读(arxiv 摘要;V2-3 加 PDF)。
2. chunking:固定窗口 512 字符 + overlap 50。
   - 为什么字符窗口不是 token:摘要中英混合，字符窗口实现最简、可解释、无分词依赖。
   - 为什么 overlap 50:防把一句话/一个论点切两半,丢语义。50 字符 ≈ 半句,够兜底。
   - 摘要多 <1500 字符 → 多为单 chunk passthrough;真正多 chunk 切在 V2-3 PDF 全文。
3. embedding:使用 bge-m3 多语言向量模型。
4. 落盘:embeddings.npy(向量矩阵)+ chunks.json(每个 chunk 的元数据 + 原文)。
   - 为什么不存进向量库:语料小,numpy+json 透明可核验;chromadb 对此规模是负债。

落盘结构(供 retriever.py 加载):
  <index_dir>/embeddings.npy   # shape (N, D) float32
  <index_dir>/chunks.json      # list[{id, doc_id, text, title, source_url, ...}]
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np

# RAG 默认 embedding 模型。V2-3 从 text2vec 换成 bge-m3:
# - 为什么换:text2vec-base-chinese 只在中文 paraphrase 上训练,无跨语言对齐信号,
#   「中文 query → 英文 arxiv 摘要」检索失效(llm_hallucination 三指标全 0,见 DEVLOG)。
# - 为什么 bge-m3:多语言密集检索专用,中英向量同空间;且自带 sparse,为 V2-4 hybrid 留路。
# 直接指向本地下载路径(ModelScope 拉的,见 DEVLOG 2026-06-23):离线确定可加载,
# 不依赖 HF 在线解析;若路径不存在则回退到 hub id,触发联网/缓存加载。
# indexer.py 在 src/dra/rag/ 下,parents[3] 才是项目根(rag→dra→src→root)。
_LOCAL_BGE_M3 = Path(__file__).resolve().parents[3] / "data" / "models" / "ms_cache" / "BAAI" / "bge-m3"
_DEFAULT_MODEL = str(_LOCAL_BGE_M3) if _LOCAL_BGE_M3.exists() else "BAAI/bge-m3"
_encoders: dict[str, object] = {}  # 按模型名缓存，避免重复加载。


def _get_encoder(model_name: str | None = None):
    """惰性加载并按模型名缓存 sentence-transformers encoder。

    按名缓存(非单例):换 embedding 后进程内可能同时持有 bge-m3(RAG)与
    各 embedding 模型各自缓存，避免查询与建库模型混用。
    首次加载先试离线缓存,缺失再联网下载(bge-m3 ~2.3GB,只下一次),
    兼顾「测试默认不联网」与「换模型免手动 pull」(CLAUDE.md §6/§7)。
    """
    name = model_name or _DEFAULT_MODEL
    if name not in _encoders:
        from sentence_transformers import SentenceTransformer
        try:
            _encoders[name] = SentenceTransformer(name, local_files_only=True)
        except Exception:
            # 本地无缓存 → 联网下载一次(后续走离线分支)
            _encoders[name] = SentenceTransformer(name)
    return _encoders[name]


# ---------------------------------------------------------------------------
# chunking
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# V2-4 BM25 分词:中英混合最简策略
# ---------------------------------------------------------------------------
# - 英文:lower + 按非字母数字切,保留专有名词(BGE-M3 → ['bge', 'm3'])。
# - 中文:每个汉字作为独立 token(字符级),不上 jieba 等新依赖。
#   BM25 对中文字符级 token 仍能跑(词频统计照样有意义),只是对"长词"
#   命中弱;但跨语言专有名词(MIRACL/LLaMA-2-7B 等)字面命中是核心收益。
# - 这是简化版,真生产可换 jieba(中文)+ subword(英文);留 V2-4 后续视评测决定。
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[一-鿿]")


def tokenize(text: str) -> list[str]:
    """BM25 用的简易 tokenizer:英文按非字母数字切 + 中文字符级。

    空/None → []。lower 后用一个正则同时抓「英文/数字连续段」和「单个汉字」。
    """
    if not text:
        return []
    return _TOKEN_PATTERN.findall(text.lower())


def chunk_text(text: str, *, window: int = 512, overlap: int = 50) -> list[str]:
    """固定字符窗口切块,带 overlap。

    - text 短于 window → 单 chunk(原文,不截断)。
    - 步长 = window - overlap,保证窗口间有 overlap 防切散语义。
    - 空文本/纯空白 → 返回 [](不产空 chunk)。
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= window:
        return [text]
    step = max(1, window - overlap)
    chunks: list[str] = []
    for start in range(0, len(text), step):
        chunk = text[start : start + window]
        if chunk.strip():
            chunks.append(chunk)
        if start + window >= len(text):
            break
    return chunks


# ---------------------------------------------------------------------------
# 加载 + 建库
# ---------------------------------------------------------------------------


def load_arxiv_docs(docs_jsonl: str | Path) -> list[dict]:
    """从 docs.jsonl 读 arxiv 摘要记录。每条含 id/title/abstract/categories/published/topic_label。"""
    docs: list[dict] = []
    with open(docs_jsonl, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                docs.append(json.loads(line))
    return docs


def build_index(
    docs: list[dict],
    index_dir: str | Path,
    *,
    window: int = 512,
    overlap: int = 50,
    model_name: str | None = None,
) -> dict:
    """把 docs 建成向量索引,落盘 embeddings.npy + chunks.json + meta.json。

    每个 doc 的 abstract 走 chunk_text 切块;每块作为一个可检索单元,
    元数据带 doc_id(回溯原 doc)+ title + published + source_url(arxiv 链接)。

    meta.json 记 embedding model_name:retriever 据此选同款 encoder 查询,
    杜绝「建库用 A、查询用 B」的模型/维度错配。

    返回统计信息(供 DEVLOG/日志):{docs, chunks, dim, model, index_dir}。
    """
    model_name = model_name or _DEFAULT_MODEL
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)

    chunks: list[dict] = []
    for d in docs:
        text = d.get("abstract") or ""
        aid = d.get("id") or ""
        title = d.get("title") or ""
        published = d.get("published") or ""
        topic_label = d.get("topic_label") or ""
        # arxiv abs 页(供 RetrievedDoc.source_url,可点开核验)
        source_url = f"https://arxiv.org/abs/{aid.split('v')[0]}" if aid else None
        for ci, piece in enumerate(chunk_text(text, window=window, overlap=overlap)):
            chunks.append({
                "id": f"{aid}__c{ci}",          # chunk 唯一 id
                "doc_id": aid,
                "chunk_index": ci,
                "text": piece,
                "title": title,
                "published": published,
                "topic_label": topic_label,
                "source_url": source_url,
            })

    if not chunks:
        # 空 chunk 仍落盘空矩阵,retriever 加载时不崩
        embeddings = np.zeros((0, 1), dtype=np.float32)
    else:
        encoder = _get_encoder(model_name)
        vectors = encoder.encode(
            [c["text"] for c in chunks], show_progress_bar=False, convert_to_numpy=True,
        )
        embeddings = vectors.astype(np.float32)

    dim = int(embeddings.shape[1]) if embeddings.size else 0
    np.save(index_dir / "embeddings.npy", embeddings)
    with open(index_dir / "chunks.json", "w", encoding="utf-8") as f:
        json.dump(chunks, f, ensure_ascii=False, indent=2)

    # V2-4 BM25 sparse:落盘分词后的 chunks 供 retriever 重建 BM25Okapi
    # 单独存一个文件,与 dense 完全解耦——retriever 可选读;旧索引(无此文件)纯 dense。
    tokenized = [tokenize(c["text"]) for c in chunks]
    with open(index_dir / "tokenized_chunks.json", "w", encoding="utf-8") as f:
        json.dump(tokenized, f, ensure_ascii=False)

    with open(index_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump({"model": model_name, "dim": dim, "chunks": len(chunks),
                   "docs": len(docs), "has_bm25": True}, f, ensure_ascii=False, indent=2)

    return {
        "docs": len(docs),
        "chunks": len(chunks),
        "dim": dim,
        "model": model_name,
        "has_bm25": True,
        "index_dir": str(index_dir),
    }


if __name__ == "__main__":
    # 手动建库入口:uv run python -m dra.rag.indexer
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default="data/corpus/arxiv/docs.jsonl")
    ap.add_argument("--index-dir", default="data/corpus/arxiv/index")
    ap.add_argument("--model", default=None,
                    help="embedding 模型名或本地路径;缺省走 _DEFAULT_MODEL")
    args = ap.parse_args()
    stats = build_index(load_arxiv_docs(args.docs), args.index_dir, model_name=args.model)
    print(f"✅ 建库完成: {stats}")
