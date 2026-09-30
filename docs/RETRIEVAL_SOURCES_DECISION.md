# 多源检索决策

> 当前有效的检索边界。历史供应商调研、价格和离线基准结果不再作为决策依据。

## 当前实现

- 网页来源由 Tavily 与 Exa 并发检索，分别失败不互相阻塞；两者都失败时尝试 DuckDuckGo，兜底仍失败才向上抛错。默认来源模式见 [STATUS](../STATUS.md)。
- 私域资料通过 Web 的“仅资料 / 混合研究”入口选择文档，研究固定文档版本并在主子任务间继承范围；仅资料模式不访问网页。旧 arXiv 本地管线及 `sources=["local"]` 接口已退役。导入、混合召回和文件引用的合同见 [EXPERIMENT_PLAN](../EXPERIMENT_PLAN.md#私域资料研究2026-09-19)。
- 现役研究把取得的正文原样保存为不可变快照，再阅读和存证；检索摘要仅作线索。旧 Worker 的清洗不属于当前快照合同，详见 [EXPERIMENT_PLAN §3](../EXPERIMENT_PLAN.md#3-数据与控制责任)。

## 取舍

选择双源 Web 检索的原因不是“多接 API”，而是降低单一服务故障对整次研究的影响，同时保留不同检索排序的候选集。代价是更多重复 URL、调试面和成本，因此只维持两个主源与一个无密钥兜底，不做题型路由、供应商矩阵或复杂缓存层。

本地 RAG 保留为可选能力：当用户有稳定私有语料时，它能提供可复现的文档检索；对普通开放网络问题，默认仍由 Web 搜索负责时效性。

## 不做

- 不维护 Serper、Jina、SEC EDGAR、Europe PMC 等专项 provider 的预埋接口。
- 不维护 BEIR/SciFact 数据、检索排行榜或为公开基准调参。
- 不把“来源数量更多”当作报告质量更高的证据。

## 验证

网页检索的故障与 fallback 回归在 `tests/test_tools.py`、`tests/test_tools_exa.py`；私域导入、固定版本、范围隔离、资料模式和文件引用在 `tests/test_document_library.py`、`tests/test_library_research.py`、`tests/test_library_web.py`。有限公开资料实测与未通过的质量边界见 [STATUS §4](../STATUS.md#4-已验证2026-09-15-起累积)，不将可定位或少量样本通过视为通用报告质量。
