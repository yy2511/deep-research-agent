# Run Traces

这里保存八次经过脱敏的 Web 运行。前四次使用同一个研究问题，后四次覆盖不同题型。
目录同时包含 `done`、`partial` 和 `failed` 结果。
原始运行位于本地 gitignored 的 `runs/`；这里只包含 `meta.json`、`events.jsonl` 和存在时
的 `report.md`。`manifest.json` 记录导出文件 SHA-256 与脱敏计数。

## 运行列表

| Run ID | 终态 | 墙钟 | save_evidence | 内容 |
| --- | --- | ---: | ---: | --- |
| `20260720-144346-9395403b` | partial | 1102.7s | 接受 131 / 拒绝 62 | 旧 quote 协议下拒收较多，可检查证据保存失败如何影响后续覆盖 |
| `20260722-230100-a7d9209e` | partial | 1176.2s | 接受 115 / 拒绝 1 | `excerpt_id` 后拒收显著下降，但 milestone 仍未全部完成；适合看“证据够多但完成语义仍有问题” |
| `20260723-113032-cc28ac2e` | failed | 265.3s | 接受 43 / 拒绝 2 | summarize 已独立统计；首批 worker 后 Assessor 两次 90s 超时，运行失败 |
| `20260723-120957-cc50f0ff` | done | 847.1s | 接受 127 / 拒绝 3 | 同题成功运行，直连 DeepSeek；可与前一条 provider/超时故障对照 |
| `20260723-205315-716a677f` | partial | 1066.7s | 接受 177 / 拒绝 4 | “美股存储板块近期为何大跌”；Research Assessor 复用 3K 输出上限后多次截断，证据很多但完成账本丢失，是当前 10K headroom 修复的直接失败样例 |
| `20260723-221026-75c7efe1` | done | 1124.9s | 接受 169 / 拒绝 1 | “普通人如何实现财富自由”；包含 research → decision → dependent research 链，可审查依赖编译、lineage 与最终写作 |
| `20260723-224942-8dbf0bf0` | done | 619.7s | 接受 111 / 拒绝 2 | “中美大模型现状与趋势”；三个 root milestone 全并行，Global Audit 判定证据不足且 22 个提示未覆盖但仍成稿，适合审查时效性、代表对象选择、来源质量与 advisory 审计价值 |
| `20260726-232556-9f2b8383` | done | 701.2s | 接受 154 / 拒绝 0 | “2026 人形机器人出货量口径与厂商进展”；research → decision(选厂商) → 逐厂商 research 全链路，7 个 worker 在 finish 里申报口径矛盾并透传成稿（报告含【关键数据矛盾与口径辨析】节），也是编年研究流 UI 与矛盾管线修复后的验收 run |

这些数字来自各目录的 `meta.json.stats`，不是人工估算。

## 旧协议术语对照

这些记录使用旧事件 schema，不能在当前 UI 回放。文件内容和 manifest 哈希保持导出时的状态；阅读时可按下列术语对应：

| 旧轨迹术语 | 当前术语 |
|---|---|
| ResearchBrief / milestone DAG | ResearchPlan / plan-node DAG |
| SubQuestion | ResearchTask |
| Frontier / Wave | Ready Set / Research Round（当轮并发单位是 Task Batch） |
| MilestoneResult | NodeAssessment |
| DecisionArtifact | DecisionOutput |
| Recovery | Final Research Pass |
| DraftReport / Draft | ReportPlan / Report Plan |
| Global Audit | Cross-Worker Audit |
| lineage bindings | downstream bindings |

## 文件说明

- `meta.json`：问题、计划、模型配置、终态和聚合统计。
- `events.jsonl`：按 `seq` 排序的运行事件，包括节点耗时、工具调用摘要以及调试模式下
  的 LLM 输入输出。
- `report.md`：成功进入写作阶段时生成的报告；失败运行可能没有。
- `manifest.json`：导出清单、文件哈希和脱敏类型计数。

## 阅读建议

1. 先比较八次 `meta.json` 的 `status`、`stats.llm` 和 `stats.tools`。
2. 在 `events.jsonl` 中按 `type`、`step`、`sid`、`seq` 追踪因果链。
3. 对证据拒收问题搜索 `tool=save_evidence`、`rejected`、`reject_reasons`。
4. 对超时问题搜索 `type=error` 和 `assess_research_milestones`。

## 脱敏边界

导出脚本是 `scripts/export_review_runs.py`。它：

- 只允许导出三个已知文件；
- 逐行解析并重新序列化 JSONL，坏行会直接失败；
- 清理常见 credential 形态和本机 home 绝对路径；
- 保留研究问题、模型输入输出和公开网页摘要，以便检查一次运行的完整上下文。

快速正则扫描最初把英文 `task-...` 中间的 `sk-` 误认为 key；上下文复核后确认是误报。
真正的凭据扫描使用 ASCII 前后边界，测试同时覆盖凭据后紧跟中文的情况。
