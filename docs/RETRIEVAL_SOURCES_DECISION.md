# 检索

## Web 检索

- 默认 `sources=["web"]`：Tavily 与 Exa 并发检索，分别失败不互相阻塞；两者都失败时默认尝试 DuckDuckGo，兜底仍失败才向上抛错。
- Worker 取得的页面必须经过正文清洗、摘录和 quote 校验，检索结果本身不是可直接写入报告的事实。

## 取舍

选择双源 Web 检索的原因不是“多接 API”，而是降低单一服务故障对整次研究的影响，同时保留不同检索排序的候选集。代价是更多重复 URL、调试面和成本，因此只维持两个主源与一个无密钥兜底，不做题型路由、供应商矩阵或复杂缓存层。

## 本地语料

在 `sources` 中加入 `local` 可以查询本地 RAG 索引。该路径适合稳定的本地语料；开放网络问题默认仍使用 Web 搜索。

## 验证

测试覆盖单源失败、双源失败后的 fallback、统一文档字段和本地/Web 路由。对应文件包括 `tests/test_tools.py`、`tests/test_tools_exa.py`、`tests/test_search_fallback.py` 和 `tests/test_local_rag_search.py`。
