# 项目状态

最近验证：2026-08-07。

## 1. 概览

- 用户确认研究计划后，系统按依赖关系调度 Research Round，并在节点验收完成后生成 Report Plan 和带引用报告。
- Web、CLI 和 Python API 共用同一编排内核；各入口分别管理模型配置。
- Cross-Worker Audit 默认关闭。开启后只报告跨任务冲突和覆盖问题，不创建新任务。
- `review_runs/` 中保存了脱敏运行记录，包括成功、部分完成和失败案例。

## 2. 已实现功能

| 功能 | 状态 | 说明 |
|---|---|---|
| Typed plan-node DAG | 已实现 | `PlanNode` 带依赖和验收标准；Web/CLI 在执行前确认计划。 |
| Research Round 调度 | 已实现 | Ready Set 解锁节点，Ready Task Compiler 生成具体任务，Task Batch 受并发、轮次、总任务和墙钟约束。 |
| Research / Decision 分流 | 已实现 | research 节点由 Assessor 裁决；decision 节点由 Resolver 生成、确定性 Validator 校验，不再调用第二个 LLM Assessor。 |
| Grounded evidence | 已实现 | Worker 通过 `doc_id + excerpt_no` 选择连续原文；服务端回填 quote、标题、URL 与日期并生成 `EvidenceCard`。 |
| Final Research Pass | 已实现 | 正常轮次结束后至多一个并行批次；每个 eligible research 节点最多补一个 Worker。 |
| Report Plan / Writer | 已实现 | 研究冻结后生成报告蓝图；Writer 只消费节点正式授权的证据，引用列表由代码恢复。 |
| Cross-Worker Audit | 已实现，默认关 | 只补充跨 Worker 冲突和覆盖风险；不改变节点状态、不创建 Worker。 |
| Web 工作台 | 已实现 | 计划确认、实时事件、取消、历史回放、Worker/证据查看、报告、统计和本机模型配置。 |
| Checkpoint / resume | Python API 可用 | schema v11 + identity/hash 校验；CLI/Web 当前未接入恢复入口。 |
| 容器运行 | 已实现 | 多阶段 Dockerfile、Compose 持久化、非 root 运行和 `/healthz` 健康检查。 |

控制流和数据结构见 [EXPERIMENT_PLAN.md](EXPERIMENT_PLAN.md)，检索实现见 [docs/RETRIEVAL_SOURCES_DECISION.md](docs/RETRIEVAL_SOURCES_DECISION.md)。

## 3. 关键默认值

以下是 Python API 的代码默认值。CLI 会显式构造模型配置，Web 会读取本机 `runtime_config.json`。

- 调度：`max_initial_tasks=8`、`max_research_rounds=3`、`max_tasks_per_round=5`、`max_total_tasks=18`
- 并发：`max_concurrent=5`
- 总墙钟：`total_timeout_s=2400`，其中 `writer_reserve_s=360`
- Worker：`wall_timeout_s=900`、`max_tool_calls=12`、`max_invalid_calls=3`、`max_cards_total=45`
- 检索：`sources=["web"]`，Tavily + Exa 每源 `top_k=4`；两者都失败时默认尝试 DDG
- Web：`DRA_MAX_ACTIVE_RUNS=1`；这是全站同时运行的研究数，不是单次研究的 Worker 数
- 持久化：`DRA_DATA_DIR` 未设置时使用项目根；生产统一挂载到容器 `/data`
- 普通 LLM HTTP 请求单次上限 90 秒；Decision Resolver 与 Writer 单次上限 180 秒
- `enable_cross_worker_audit=False`

默认开发环境包含 NumPy + BM25 以运行离线索引测试，但不安装 `sentence-transformers` / Torch；只有本地 RAG 建库、向量检索或 cross-encoder 重排才使用 `uv sync --extra local-rag`。生产镜像使用 `uv sync --locked --no-dev`，不包含整组本地 RAG 重依赖。

## 4. 验证结果

- 后端：`uv run pytest` → **504 passed, 11 skipped, 1 warning**（2026-08-07）。
- 前端：`npm test` → **98 passed**；`npm run build` 通过；`npm run lint` 仅保留 `LlmCalls.tsx` 的既有 Fast Refresh warning（2026-08-07）。
- GitHub Actions：后端、前端和本地容器 smoke test 通过（2026-08-07）。
- 容器：镜像以非 root 用户运行；首页和 `/healthz` 返回 200；生产依赖与开发工具边界通过 smoke test。

默认测试不调用网络或付费模型；只有 `--run-live` 才执行真实 API 用例。

## 5. 限制

- `EvidenceCard` 能证明引用材料来自哪段原文，但不能自动证明 Writer 的每个综合推断都正确。
- 模型配置按入口分源是有意设计；切换 provider 后必须在对应入口重新验证，不能外推本机结果。
- Cross-Worker Audit 是 advisory，不是逐条事实审计器，也不替代节点验收。
- Web 容量门只约束单进程事件循环；多进程或多实例需要共享容量存储。
- 本地 RAG 是可选语料检索能力，不参与默认开放网络研究主线。
