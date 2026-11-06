# 0006：数据库访问用 SQLAlchemy（同步）+ Alembic，离线测试默认用 SQLite

- 状态：草稿
- 日期：2026-11-06
- 作者：Claude Code
- 关联：#25、ADR 0002、[阶段1-数据库与用户](../design/阶段1-数据库与用户.md)

## 背景

ADR 0002 定了用 PostgreSQL。第 1 阶段开始真正接入，需要定三件事：

1. 程序用什么方式读写数据库、管理表结构变更。
2. 用同步还是 async 写法。
3. 测试怎么连数据库。

约束：

- **研究引擎是同步代码，跑在线程里。** 调模型用同步的 OpenAI 客户端（`src/dra/llm.py` `_get_client`）；整个研究在 `run_in_executor` 派生的线程里执行（`src/dra/api/services/runs.py` `RunRegistry._run_simple`）；子 Agent 用线程池（`src/dra/research_delegation.py`）。第 2 阶段扣额度、第 3 阶段 Worker 写进度，都发生在这些线程里。总体方案要求研究引擎保持不变。
- **接口层是 FastAPI**，已有 `async def` 和普通 `def` 两种接口（如 `routers/research.py` 的 `api_research` 与 `api_cancel`）。
- **现有离线测试大量直接调用 Web 接口**：`tests/` 里有 116 处 `TestClient(`。第 1 阶段之后，这些接口都要查数据库（当前用户、归属）。
- 已和用户确认：不加开关的 `uv run pytest` 保持离线、不需要另外启动服务；数据库地址从环境变量读，以便本机（Docker Compose）、CI（GitHub 的 PostgreSQL 服务）、云端开发环境（系统自带 PostgreSQL）都能跑。
- 开发规范 §3：表结构只用迁移脚本改；已合并的迁移不改只增；迁移要能在空库和已有数据的库上跑通。

## 评估标准

1. 与现有线程模型的配合，对研究引擎的改动量。
2. 迁移管理是否现成、可靠。
3. 离线测试：不启动外部服务能否覆盖接口行为；与生产数据库的差异有多大。
4. 学习价值与资料多少。

## 候选方案

### 读写方式

| 标准 | A. SQLAlchemy 2 ORM + Alembic | B. 手写 SQL（psycopg 3 直连） | C. SQLModel |
| --- | --- | --- | --- |
| 迁移 | Alembic 现成，可按模型自动生成草稿 | 要自己写版本表和执行器 | 底层仍是 SQLAlchemy + Alembic |
| 连接池 | 自带 | psycopg_pool 可用，但事务、会话边界自己管 | 自带 |
| 可读性 | Python 对象；生成的 SQL 需要时可打印 | 每句 SQL 一目了然 | 与 Pydantic 合一，写法最短 |
| 生态与资料 | Python 后端最常见 | 通用 | 较新，复杂查询仍要落到 SQLAlchemy |

### 同步还是 async

| 标准 | 同步（`Session` + psycopg 3） | async（`AsyncSession` + asyncpg/psycopg 3 async） |
| --- | --- | --- |
| 研究线程里调用 | 直接调用 | 线程里没有事件循环，每处都要转换；或把引擎改成 async（大改） |
| 接口层 | 查库的接口写成普通 `def`，FastAPI 自动放到线程池执行 | `async def` 接口可直接 `await` |
| 并发能力 | 受线程池大小限制，内测规模够用 | 单进程可撑更多连接 |

### 离线测试连什么

| 标准 | T1. 默认 SQLite，加开关改连 PostgreSQL | T2. 只用 PostgreSQL，没有就跳过 | T3. 只用 PostgreSQL，没有就失败 | T4. 内存假仓储 |
| --- | --- | --- | --- | --- |
| 不加开关的 `uv run pytest` | 离线、全部接口测试照跑 | 离线，但 116 处接口测试大多被跳过，覆盖大幅下降 | 必须先起 PostgreSQL，违背已确认的第 1 条 | 离线、照跑 |
| 与生产差异 | 有方言差异，靠 CI 在 PostgreSQL 上全量再跑兜底 | 无 | 无 | 大：测的是另一份实现 |
| 额外维护 | 迁移要同时兼容两种数据库 | 无 | 无 | 每个仓储写两遍 |

## 决定

- **读写**：A，SQLAlchemy 2 ORM + Alembic，驱动 psycopg 3。生产和开发只支持 PostgreSQL（ADR 0002），程序启动时检查连接地址，不接受其他数据库。
- **写法**：同步。配套规则：
  - 需要数据库的接口写成普通 `def`；`async def` 接口不依赖数据库会话。`tests/test_api_layering.py` 增加检查。
  - 数据库会话按请求创建、请求结束关闭；研究线程里的读写各自开短事务，不在事务里调用模型、搜索等外部服务（开发规范 §3 已有）。
- **测试**：T1。
  - 默认：每个测试一个临时 SQLite 文件，用同一套模型和 Alembic 迁移建表。
  - `--run-postgres`：从 `DRA_TEST_DATABASE_URL` 读地址，同一批测试改连 PostgreSQL，另外跑只适用于 PostgreSQL 的测试（标 `postgres`，如以后的 `SKIP LOCKED`、`LISTEN/NOTIFY`）。没给地址时报错说明，不静默改回 SQLite。
  - CI 两种都跑：现有测试步骤保持默认方式；另加一个带 PostgreSQL 服务的步骤跑 `--run-postgres`。
  - SQLite 只用于测试，不作为可选的运行数据库。

## 代价

- 同步写法下，查库的接口占用线程池里的线程。FastAPI 的线程池默认 40 个线程，内测够用；用户量大时要调大或改 async（见“何时重新评估”）。
- SQLAlchemy 连接池沿用默认：常驻 5 条、可临时多开 10 条、取不到连接时等 30 秒后报错。这是沿用的上限，实测后再调。
- SQLite 与 PostgreSQL 有差异，默认测试通过不代表 PostgreSQL 上也通过：
  - 带时区的时间：SQLite 不保存时区。约定库里一律存 UTC，读出后由代码补上时区。
  - 修改表结构：SQLite 的 `ALTER TABLE` 能力有限，Alembic 开启批量模式（`render_as_batch`）。
  - 锁与并发：SQLite 没有行锁，并发相关的测试只在 PostgreSQL 上跑。
  这些差异由 CI 的 PostgreSQL 步骤兜底；本机或云端改了数据库相关代码时，也应跑一次 `--run-postgres`。
- 新增依赖：`sqlalchemy`、`alembic`、`psycopg[binary]`。

## 何时重新评估

- 研究引擎因其他原因改成 async。
- 同时在线用户多到线程池或连接池成为瓶颈（监控里出现等待连接、等待线程）。
- 需要用到 SQLite 无法模拟、又必须在默认测试里覆盖的 PostgreSQL 功能，使 T1 的两套跑法维护成本过高。
