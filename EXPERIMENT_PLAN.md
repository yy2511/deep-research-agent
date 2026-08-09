# 架构与设计

本文描述编排流程、核心数据结构和运行约束。

## 1. 目标与边界

系统将研究计划表示为可执行的 typed plan-node DAG；Worker 保存的证据必须能回到连续原文；最终报告需要标明引用，并保留来源冲突和未解决问题。

范围限于开放网络研究和可选的本地语料检索，不试图覆盖所有研究题型。验证以运行时回归测试、前端检查和脱敏 Web 运行记录为准。

## 2. 执行流程

```text
问题
  → Planner（typed plan nodes + initial tasks）
  → 用户确认计划
  → Ready Set / Research Rounds（每轮以 Task Batch 并发 Worker）
  → Research Assessor（按验收标准裁决）/ Decision deterministic Validator
  → Final Research Pass（至多一次）
  → Report Plan（报告蓝图）
  → [可选] Cross-Worker Audit（冲突、覆盖风险；只 advisory，enable_cross_worker_audit 默认关）
  → Writer（带引用 Markdown）
```

Report Plan 是固定阶段，研究补查只由节点 gap、Ready Set、Research Rounds 与 Final Research Pass 控制；Report Plan 与 Cross-Worker Audit 都不创建新任务。矛盾呈现的主通道是 Worker 在 finish 里的申报（无锚透传给 Writer），Cross-Worker Audit 只补跨 Worker 检查、默认关。Final Research Pass 只允许一次并行批次（每个 eligible research node 至多一条任务）；eligible 包含 partial/blocked 缺口，以及因上游重试挤占 Research Round 而刚解锁、尚未首次执行的 research 节点。已经被 complete Decision 消费的直接或间接上游 research 不再 eligible：Decision 及 bindings 已冻结，当前系统没有级联重算，事后补上游不能改变既有选择。未关闭的 gap 进入局限而不是继续循环。Worker 只有原生 tool-calling loop；second opinion、pipeline、claim-grounding 和无 Report Plan Writer 分支均已删除。

Final Research Pass 的确定性 fallback 优先把 Assessor 第一条可执行 gap 原样作为 Worker
objective 与 search query，不再把全部祖先 bindings 放到 gap 前面，也不做机械字符截断。只有
刚解锁且尚无 gap 的首次执行节点，才加入最多 4 个上游 binding 值帮助解析研究范围；完整证据
授权仍保留在代码侧。

## 3. 数据与控制责任

| 对象 | 负责 | 不负责 |
|---|---|---|
| `PlanNode` | 研究语义、依赖、验收标准 | 报告章节或网页检索结果 |
| `ResearchTask` | 当前 Worker 的具体目标、起始查询与经代码授权的前置信息 | 全局调度真相 |
| `EvidenceCard` | 自包含 claim、原文 quote、来源标题/URL 与日期 | 自动保证整篇报告所有推断都正确 |
| `NodeAssessment` | research 的证据充分性状态、缺口、授权证据与 `node_digest`；decision 的确定性产物合法性状态 | Writer 的章节排版 |
| `DecisionOutput` | Decision 节点从已验收上游证据得出的选择与下游绑定 | 自行声明节点完成或证明选择客观最优 |
| `ReportPlan` | 报告章节与比较/表格提示 | 默认补派控制 |
| `Report` | 最终 Markdown 内容和引用标记 | 反向修改研究状态 |

plan-node DAG、任务账本和 `NodeAssessment` 共同记录调度状态；`EvidenceCard` 记录证据；最终引用列表由代码根据正文中的 `[n]` 和证据表生成。Worker 会收到本任务的验收标准，不接收无关节点或重复派生字段。

Worker 保存证据时使用 `doc_id + excerpt_no` 选择某篇已精读文档内的一段连续原文；序号
只在保存阶段定位 quote，不进入长期证据契约。`EvidenceCard.claim` 的“单一事实”指一项能被
所选连续原文直接支持、脱离当前对话仍可理解的判断：必须保留主体、时间、样本、场景和
适用范围，但同一主体与同一事件中的紧密信息不应被机械拆成失去语境的关键词。来源标题、
URL 与日期均由服务端从 `RetrievedDoc` 复制，不能要求 Worker 抄写。

`EvidenceCard.id` 是系统内部的全局证据主键，只用于去重、授权、节点血缘、checkpoint 与
代码侧映射，不进入 LLM prompt。Worker 只操作本轮 `DocRegistry` 的 `doc_id`；Resolver、
Assessor、Cross-Worker Audit 与 Writer 只接收各自清单中的 1-based `[n]` 局部编号；Report
Plan 只接收代码已经由全局 ID 解析出的 claim。模型返回局部编号后，代码再映射回全局 ID，
避免把同为短字符串但不可互换的标识符暴露给模型。

Decision 的控制面交接以意图保真为准：`downstream_bindings` 中每个值必须在同一
`DecisionOutput.decision_summary` 中逐字出现，代码以此对账“正文宣布的决策”与
“传给下游的控制值”。它不要求综合选择逐字出现在单条网页证据中；上游 Research
Assessor 负责证据充分性，Decision deterministic Validator 只检查产物结构、引用授权、
必需 binding 与正文/控制值一致性。Decision `complete` 表示产物合法且可消费，不表示
另一个模型证明了选择客观最优。

Research Assessor 在已经通读本节点授权证据的同一次调用中产出 `node_digest`：
2–4 句核心结论、关键数据和未解缺口，使用当前局部证据编号。它的消费者是下游
Decision Resolver、Ready Task Compiler 和后续 Worker；Writer 不直接消费。它与面向调度的
验收理由 `summary` 分开，不新增摘要 agent 或额外 LLM 调用。Decision 节点的同等结论投影
直接使用 `DecisionOutput.decision_summary`。

Ready Task Compiler 只接收当前目标、验收要求、当前节点 gaps、上游 `node_digest` /
`decision_summary` 和 `downstream_bindings`，不重复注入全部祖先 EvidenceCard claims。完整
evidence IDs 继续保留在代码侧血缘字段。派给 Worker 时再按 task 中实际命中的 binding 值投影
最小的前置约束，只传当前 task 命中的对象/参数；不再把 Decision summary 投影给 Worker，
因此不依赖自由文本切分，也不会把上游局部编号带入 Worker。多对象 Decision 的 task 必须逐字
点名至少一个主要对象，不能只命中价格或阈值；同一 node 的并行 Worker 不共享无关对象。binding 中未逐字出现在
本轮 task 的描述性属性不伪装成正式 gaps，业务覆盖仍由当前节点 acceptance criteria 与 Assessor
判断。

Report Plan 在研究冻结后同时消费：已完成节点经全局 EvidenceCard ID 解析出的 cited claims、
`downstream_bindings`，以及已完成 Decision 的权威 `DecisionOutput.decision_summary`。终端
Decision 即使不需要给下游 research 传 binding，其选择、排序、对象关系与取舍也必须进入报告
蓝图，Report Planner 不得从散落 claims 重新决策或自行改序；其中事实理由仍须由 cited claims
支撑。未完成节点除业务目标和验收标准外，还传入该节点最新 `NodeAssessment.summary` 与
`gaps`；这些内容只能成为局限或未来研究提示，不能被包装成已完成结论。实际执行任务只渲染
一份，不再以“初始任务”和“实际任务”重复注入。

Writer 只接收所有 `NodeAssessment.evidence_ids` 的并集，不重新通读 Worker 保存但未被节点验收
正式授权的卡；证据仍保留 `state.evidence` 中的原始全局 1-based 位置，过滤只决定哪些编号进入
Writer prompt，不重排、不压缩。Writer 若返回未展示编号，代码在构造 Report 时删除该引用；有锚
Conflict 同样不得用未授权卡绕过过滤。证据行保留日期、来源域名、服务端标题、claim 和单行有界
`quote_excerpt`，完整 URL 不进入 Writer，而由最终 References 根据 `[n]` 从 EvidenceCard
确定性恢复。价格、比例、日期、排名等表格事实允许且要求在对应单元格就近引用；确定性后处理
保留合法表格引用，只删除越界引用。结构化 Report 解析时同时清理流程内部占位符。Report Plan
已是 Writer 的选择与结构输入，因此不再重复注入 completed 节点的扁平 bindings 账本；未完成
目标还会携带最新验收说明与具体 gaps 进入局限提示。

Decision Resolver 不再默认通读全部授权卡片全文。其输入分三层：直接上游的
research `node_digest` / Decision `decision_summary`；最多 30 条关键证据全文；其余证据的
单行 claim 索引。全文集先对直接上游分支轮转保底最多 5 条，再依 node objective +
acceptance criteria 的关键词重叠度填满；全局 30 条上限优先，上游超过 6 支时用轮转公平
而不伪造“每支仍有 5 条”的不可能保证。截断只决定渲染全文还是索引，所有卡片始终保留
原 scoped 1-based 编号；代码把 Resolver 返回的编号映射回全局证据 ID 后再做授权校验。

## 4. 运行时约束

- 计划受初始任务数、Research Round、单轮 Task Batch 和总任务预算限制。
- Worker 默认使用 tool-calling loop，并有工具调用、无效调用、墙钟和证据数量熔断。
- `DocRegistry` 保存 Worker 的完整运行状态；模型每轮只接收固定任务、确定性状态投影和
  最近一组完整工具交互，不把历史对话当状态数据库。
- 搜索列表只投影查询相关的有界片段：Tavily 使用结果 `content`，Exa 使用针对当前 query 的
  `highlights`，统一最多 600 字；完整 `raw_content` 不在搜索列表展开，只能通过 `fetch_page`
  进入精读上下文和服务端逐字核验。
- 保存成功后，精读全文退出热上下文，只留下 claim、来源与日期台账；尚未落账的精读
  材料数量不设额外流程限制，完整原文始终由服务端保留并负责引用校验。
- 搜索和 Worker 失败以部分结果继续；没有证据时报告应写入局限，不能补造事实。
- 结构化 LLM 节点先校验 JSON 语法与顶层字段。Decision Resolver 用同一个确定性 validator
  校验单项 `node_id / decision_summary / evidence_ids / downstream_bindings`、授权编号、必需
  binding 与 summary/binding 一致性；首次生成非法时把全部具体错误退回模型完整修复一次，
  仍失败即以 `contract_error` blocked，不再调用 Decision Assessor 或重新激活节点，也禁止
  从自由文本引用反推控制字段。
- 单节点 Resolver/Research Assessor 的系统契约动态写入真实 `expected_node_id`。Research
  Assessor 校验单条结果、精确 ID、状态、摘要、授权证据号、digest 与 gaps；回执协议错误只在
  原 Assessor 调用内修复一次，不重跑 Worker，仍失败以 `assessment_contract_error` 显式 blocked。
- Ready Task Compiler 只接受当前 Ready Set ID 白名单。Final Research Pass 在名额不足时先给
  尚未获得任何 Worker 的新解锁节点，再按 Planner 顺序补已有 partial/blocked；协议失败节点
  不重新派 Worker。终态为每个 unresolved 节点记录协议失败、依赖阻塞、重试/轮次/任务/时限等原因。
- 外部网页文字是不可信数据；读取原文的模型提示必须防范间接 prompt injection。
- checkpoint 使用 schema 与 hash 防止不兼容恢复；新版不承诺迁移旧实验状态。

## 5. 可观测性

Web 将编排事件、任务卡、节点状态和报告按时间顺序显示，并支持计划确认、取消和历史回放。调试模式可以展开 Planner 与 Worker 的 LLM 输入输出；普通运行只保存截断后的调用摘要。`review_runs/` 保存脱敏的 Web 运行记录。CLI 与 Python API 复用同一编排内核。

验证以 `tests/` 的确定性回归测试、前端测试/build/lint 和 Web 手动演示为准。只有标记为 `live` 的测试才会调用网络或付费模型；默认测试不产生费用。

## 6. 设计约束

1. 每个字段必须有控制消费者或解释消费者；两者都没有就删除。
2. 程序已经知道的常量、ID 和派生投影不要求 LLM 回显。
3. 默认路径只有一套语义，不为旧 checkpoint、旧事件或旧 planner 保留兼容分支。
4. 优先减少噪声和解释成本，不再用新增 agent、循环或 judge 掩盖质量问题。
5. 历史实现不参与当前运行路径。

## 7. 生产交付边界

生产服务器只拉取构建完成的镜像，不从 Git checkout 构建。公开 GitHub Actions 运行后端测试、
前端 test/lint/build，并在 runner 本地构建和 smoke test `linux/amd64` 镜像；工作流没有包写权限，
也不部署生产。生产候选镜像由单独的私有发布通道生成。每个候选版本同时记录不可变的
`sha-<完整 Git SHA>` tag 与 `sha256` digest，运行配置使用 `tag@digest`，防止标签移动后静默换包。

运行状态、密钥与镜像版本分离：应用密钥仍由服务器受控文件注入，`runs/`、`plans/` 与
`runtime_config.json` 只存在于宿主机 shared 数据目录，release 文件只记录非秘密的镜像坐标
和状态。容器的运行 uid/gid 可由 Compose 映射到宿主机数据所有者；不能为了适配镜像默认
UID 而把数据交给宿主机上碰巧使用同一数字的其他服务账号。

发布控制状态只有 staged/verified、active、previous 与 pending：候选镜像先在非公网端口
使用真实数据权限通过 canary，才允许占用生产端口；首次切换的 previous 是旧 systemd 服务，
后续切换则是上一容器 release。activate 失败必须自动恢复切换前状态，rollback 只接受已记录
previous，不猜测目录或 tag。Caddy 不参与版本切换，始终反代本机 8765。

生产端口和开机启动权也必须单一归属：activate 容器时同时 disable/stop 旧 systemd，容器由
Docker 的 restart policy 随宿主机恢复；回滚 systemd 时先删除生产 Compose，再 enable/start
旧服务。禁止只停进程却保留两套开机自启，否则服务器重启后会争抢 8765。
