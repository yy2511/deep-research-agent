"""V2-1 经典 RAG 管道:local 语料向量化 + 暴力检索。

模块边界:
- indexer.py:文档加载 → chunking → embedding → 落盘(.npy 向量 + chunks.json 元数据)
- retriever.py:加载落盘 index → search(query, top_k) numpy 暴力检索

设计取舍（详见各模块 docstring）:
- 向量存储用 numpy 不上 FAISS/chromadb:语料小(几百~几千),暴力检索毫秒级,
  可读性强，也便于说明「规模破万再换 FAISS」的工程取舍。
- embedding:V2-3 已从 text2vec 换 bge-m3(多语言密集检索)——text2vec 无跨语言对齐,
  「中文 query → 英文摘要」失效;模型名落 meta.json,retriever 据此选同款 encoder。
  models.py 去重仍用 text2vec(中文论点聚类无需跨语言)。
- chunking 固定窗口 512+overlap50:最简能讲清原理;摘要多为单 chunk passthrough,
  真正考验 chunking 留 V2-3 PDF 全文。

V2-1 先不抽 VectorIndex 类(CLAUDE.md §2:等 V2-2 ResearchMemory 第二个实现再抽)。
"""
