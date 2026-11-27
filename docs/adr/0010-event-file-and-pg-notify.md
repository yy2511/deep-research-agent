# 0010：跨进程事件流沿用共享目录 events.jsonl，用 PostgreSQL LISTEN/NOTIFY 推送进度

- 状态：草稿（合并即接受）
- 日期：2026-11-28
- 作者：用户 / Antigravity
- 关联：[#82](https://github.com/yy2511/deep-research-agent/issues/82)、[阶段3-Worker与进度推送](../design/阶段3-Worker与进度推送.md)、[ADR 0002](0002-postgres-task-queue.md)、[ADR 0006](0006-sqlalchemy-alembic-sync.md)

## 背景

第 3 阶段把耗时数分钟的研究执行从 Web（API）进程拆到独立的 Worker 进程后，研究过程产生的实时进度事件在 Worker 进程内生成，而浏览器的 SSE 长连接挂在 API 进程上，不能再靠单进程内存队列（`src/dra/api/services/runs.py` `RunRegistry.runs`）直接推送。

需要确定跨进程事件存储与实时唤醒方案，约束如下：

1. **事件体量较大**（[后端扩展总体方案 §6.4](../design/后端扩展总体方案.md#64-进度事件与推送) 本机 49 份运行日志实测）：单次研究的 `events.jsonl` 中位数 2.7 MB，90% 在 17 MB 以内，最大 21 MB、约 4 万条事件。
2. **线上主机资源与备份策略**（2026-11-28 线上 `kc-edge-01` 只读采样）：2 核 CPU、1.9 GiB 物理内存（可用约 838 MiB），50 GB 系统盘剩余 19 GB；PostgreSQL 16 常驻内存约 25 MiB，每日北京时间 03:20 执行 `pg_dump -Fc` 全量备份并持续保留（[deployment.md](../deployment.md#数据库备份)）。一旦把数 MB 级的事件正文写进 PostgreSQL，数据库体积与每日备份归档都会迅速膨胀。
3. **现有读取合同**：历史回放接口 `GET /api/runs/{run_id}/events`（`src/dra/api/routers/runs.py` `api_run_events`）、单条调用详情 `api_run_event_detail` 及公开示例均直接读取 `runs/<run_id>/events.jsonl`。
4. **测试环境要求**（[ADR 0006](0006-sqlalchemy-alembic-sync.md)）：默认离线测试使用临时 SQLite，PostgreSQL 集成测试使用 `--run-postgres`；方案需在两种数据库下都能验证完整的事件补发与续传逻辑。

## 评估标准

1. **读取完整性（无半行撕裂）**：API 进程绝不能在 Worker 写文件写到一半时读出残缺的 JSON 行，也不能乱序或漏事件。
2. **存储与备份成本**：不显著增加 PostgreSQL 内存占用、表体积与每日 `pg_dump` 备份大小。
3. **实时性与断线续传**：正常网络下新事件百毫秒级推到浏览器；浏览器刷新、网络闪断或 API 容器重启后，能按最后收到的 `seq` 准确补发。
4. **组件复杂度与可测性**：不提前引入新常驻中间件；同时兼容 PostgreSQL 与离线 SQLite 测试。
5. **双向唤醒能力**：除“Worker 通知 API 有新进度”外，还能顺带解决“API 通知 Worker 有新任务入队 / 请求取消 / 收到补充消息”。

## 候选方案

| 标准 | A. 共享 `events.jsonl` + 库表 `last_event_seq` + PG `LISTEN/NOTIFY`（辅以轮询兜底） | B. 事件正文全量写入 PostgreSQL `run_events` 表 + `LISTEN/NOTIFY` | C. 共享 `events.jsonl` + 操作系统文件监听（`inotify` / `FSEvents`） | D. 引入 Redis Streams / Pub/Sub 做事件总线 |
| --- | --- | --- | --- | --- |
| 读取完整性 | 高：Worker 整行刷盘后才提交 `last_event_seq` 并触发 `NOTIFY`，API 只读到已确认序号为止 | 高：事务保证整行可见 | 低：文件刚写入部分字节即可能触发系统通知，易读到未写完的半截 JSON，需额外自研跨进程文件帧协议 | 高：单条消息原子发布 |
| 存储与备份成本 | 低：大体积事件仍在数据目录，数据库每条运行只存一个整数序号，不放大 `pg_dump` | 高：每次研究新增数 MB～20 MB 表数据与 WAL，每日持续保留的全量备份迅速膨胀 | 低：全部在文件系统 | 中：需限制 Stream 保留长度并额外占用常驻内存 |
| 实时性与续传 | 实时（毫秒级 `NOTIFY`）；重连直接从文件按 `seq` 顺序补发 | 实时；重连按主键范围查表 | 本机宿主机实时，但 macOS Docker 挂载卷或网络盘易漏事件；需额外定时轮询补救 | 实时；若 Stream 过期裁剪仍需回退读文件 |
| 组件复杂度与可测性 | 零新增中间件；SQLite 下仅把唤醒源退化为短轮询 `last_event_seq`，上层读取逻辑完全一致 | 零新增中间件，但需改写历史回放或双写文件与数据库 | 需引入文件监听依赖，按每个活跃运行动态注册句柄，容器跨平台行为不一致 | 需提前部署并运维 Redis，且存在数据库状态与 Redis 双写一致性问题 |
| 双向唤醒能力 | 强：同一套 `LISTEN/NOTIFY` 同时覆盖进度推送与入队/取消/补充消息唤醒 | 强 | 弱：`events.jsonl` 只能表达 Worker→API，API→Worker 仍需另建通道 | 强 |

*注：纯数据库定时轮询（完全不用 `NOTIFY`）作为方案 A 在 SQLite 和断线恢复窗口的子集保留，单独作为生产主方案会有 0.5～1 秒固定延迟与空转查询，故不单列。*

## 决定

选 **方案 A**：事件正文继续写在共享数据目录的 `runs/<run_id>/events.jsonl`，PostgreSQL 只存最新已落盘序号并用 `LISTEN/NOTIFY` 做跨进程唤醒，辅以低频轮询兜底。

1. **单一写者与提交顺序**：运行期间，`runs/<run_id>/events.jsonl` 仅由领走该执行尝试的 Worker 进程追加写入。Worker 先把完整 JSON 行（含换行符）写入文件并 `flush`（带 `event_id` 的控制事件继续 `fsync`），再在数据库短事务中更新 `run_attempts.last_event_seq` 并执行 `NOTIFY dra_events, '<run_id>:<last_event_seq>'`。PostgreSQL 保证通知只在事务提交成功后发出。
2. **有界安全读取**：API 进程的 SSE 流以数据库里的 `run_attempts.last_event_seq`（及运行终态）为读取上界，按行读取 `events.jsonl` 时只消费 `seq <= last_event_seq` 的完整行，不读取尚未在数据库确认的尾部字节。
3. **批量合并刷新**：普通高频观测事件允许按短窗口（如 50～100 ms）合并更新一次 `last_event_seq` 与 `NOTIFY`，避免每次子代理工具输出都打一次数据库事务；关键状态事件（开始、用户补充消息确认、取消、结束 `end`）立即刷新并通知。
4. **两条通知频道**：
   - `dra_events`（Worker → API）：进度序号推进与尝试进入终态；
   - `dra_tasks`（API → Worker）：新任务入队、取消请求、运行中补充消息到达，唤醒空闲或执行中的 Worker 立即处理。
5. **兜底与 SQLite 兼容**：`NOTIFY` 只负责“低延迟叫醒”，不承担持久化责任。API 进程维护单条专用 PostgreSQL `LISTEN` 连接（不占 SQLAlchemy 业务连接池），连接断开重连时以及每隔数秒的兜底检查都会重读 `run_attempts` 的状态与 `last_event_seq`，漏掉的通知会自动补齐；离线测试使用 SQLite 时不建 `LISTEN` 连接，直接走短间隔轮询同一套读取函数。

## 代价

- **单机共享目录约束**：API 容器与 Worker 容器必须挂载同一个宿主机数据目录（`shared/`），第一版不支持把 Worker 放到没有共享文件系统的另一台机器上。
- **专用监听连接维护**：API 与 Worker 在 PostgreSQL 下各需维护一条长连接用于 `LISTEN`，需要处理数据库重启后的自动重连与重连期间的状态追平。
- **小幅落盘节奏开销**：即使做 50～100 ms 窗口合并，运行期间仍会比纯内存模式多出每秒若干次对 `run_attempts.last_event_seq` 的轻量主键更新。

## 何时重新评估

- 需要将 Worker 独立部署到另一台机器（例如为了运行重型沙箱或本地大模型而分离计算节点），无法低成本共享同一块宿主机目录时；届时可将事件流切至 Redis Streams 或批量入库，对外 SSE 接口合同保持不变。
- 同时在线观看实时进度的并发连接数增长到数百以上，多连接反复读取同一 `events.jsonl` 产生明显磁盘读放大时（届时可在 API 进程内对同一活跃 `run_id` 做一层按 `seq` 共享的只读缓存扇出）。
