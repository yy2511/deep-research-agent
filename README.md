# 深度研究 Agent

一个以 Web 为主要入口的深度研究 Agent。主 Agent 自己搜索、阅读、保存证据，按需异步派发子任务并继续工作，交付前过一道确定性引用门（正文里的 `[cN]` 必须指向真实存在的证据卡）。Web 只有这一条研究主线；旧“计划研究”执行与兼容代码已删除。

当前能力、默认值与验证边界见 [STATUS.md](STATUS.md)；研究过程中每一道检查的说明见 [docs/研究规则.md](docs/研究规则.md)；控制流、数据责任与后续方向见 [EXPERIMENT_PLAN.md](EXPERIMENT_PLAN.md)（**恢复当前不支持**，见该文 §6）。本目录只包含现役代码和可控测试样本，历史运行记录保留在原项目中。

当前主线如下。

## 核心流程

```text
问题 → 按需澄清与确认计划 → 主 Agent：搜索 / 阅读 / 存证 / 异步协作 / 更新任务清单
  → 交稿或有界预算收尾 → 引用门 → 带引用答案或明确标记的受限结果
```

工作台按“对话 / 证据 / 运行记录”切换（运行记录可在“设置 → 通用”里隐藏），支持查看子 Agent 过程、文件预览与下载，并在历史研究中继续追问。
主线不支持中断恢复；DAG 执行与历史兼容代码已删除，旧设计见 Git 和架构归档。

## 设计要点

- **材料先保存再引用**：来源保存为不可变快照，一张证据卡可包含多段原文，逐段由服务端匹配并保留位置；Lead 自己调研同样需要存证。
- **存证时反馈引文支持情况**：可在设置中启用 Jev，联合判断主张、各段原话及程序补充的上下文，将结果返回 Agent 修正并显示在卡片上。交付时仍检查引用编号；判定范围和验证边界见 [STATUS.md](STATUS.md)。
- **Lead 决定研究动作**：查什么、如何修正证据、何时收尾都由它判断。
- **完成含义分开**：研究覆盖、停止原因、交付状态分别显示，预算用完不会冒充通过。
- **预算内收尾**：模型、工具和时间都有上限，并给收尾留出空间。
- **结果可追溯**：历史答案、证据卡和已保存原文可读；取消后保留材料，仍在执行的请求退出后才释放运行名额。

## 快速开始

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 Node.js 22。

```bash
# 安装后端与开发依赖
uv sync

# 配置 LLM 与检索 provider
cp .env.example .env

# Web 工作台：两个终端分别启动后端和前端
uv run python -m dra.web
cd web && npm ci && npm run dev
```

Web 后端默认监听 `http://127.0.0.1:8765`，Vite 开发服务器默认监听 `http://127.0.0.1:5173`。生产形态先运行 `cd web && npm run build`，随后只启动后端，由 FastAPI 直接服务 `web/dist/`。

默认 Web 检索会并发尝试 Tavily 与 Exa，建议同时配置两者；两者都失败时会尝试无密钥的 DuckDuckGo 兜底。完整环境变量见 [.env.example](.env.example)。

### API 连接与模型设置

点左下角“设置 → API 连接”，添加、编辑或删除自定义 OpenAI Chat Completions 兼容来源，填写 API 根地址和 Key。
点击“获取模型”，再到“设置 → 模型”为规划、研究与写作两个环节选择模型、设置思考档位，并用“测试调用”验证连接。
地址使用服务商提供的 API 根路径（常见为以 `/v1` 结尾），不要填模型列表或聊天接口的完整路径。

模型接口可能只返回 ID；设置页会展示可获得的上下文窗口、最大输出和思考档位，标明资料来源，缺失时可手动补充。
自定义 Key 保存在后端本地配置中；已有 `.env` 来源继续由环境变量管理。具体支持范围见 [STATUS.md](STATUS.md)。

### 研究技能

打开“设置 → 技能”，新建、编辑或启停技能，可同时编辑 `references/` 下的文本参考材料。
新研究默认让 Agent 按需选择；点输入框下方的“技能”按钮并勾选，可让指定技能从澄清和规划开始生效。
研究结束后，回答下方的“本轮用到的技能”可查看主线或子任务实际加载的技能、参考正文与版本。

也可在后端数据目录放置 `skills/<name>/SKILL.md`，然后刷新目录并启用。
文件使用 YAML frontmatter：`name` 与目录名一致，`description` 说明适用任务，
可选 `metadata.display-name` 提供中文名称；其余正文写方法和判断原则。
修改用于后续新研究，已有会话的追问保留原版本。支持范围与默认值见 [STATUS.md](STATUS.md)。

### 计算与文件交付

需要 Agent 执行计算、制作图表或生成文件时，先启动本机 Docker，再构建执行镜像：

```bash
docker build -t dra-sandbox:local sandbox_image
```

在后端 `.env` 中设置 `DRA_EXECUTION_ENABLED=true`，然后重启后端。
也可以在网页「设置 → 研究能力 → 运行代码」里开关并保存：网页上的选择会覆盖 `.env` 的开关，只影响之后开始的研究；
没选过时跟随 `.env`。镜像、网络模式、时限和资源上限仍只按 `.env` 配置。同一页面会显示 Docker 实际是否可用。
需要下载公开数据时，再设置 `DRA_EXECUTION_NETWORK_MODE=bridge`。主 Agent 可用 `download_source`
直接下载 CSV/TSV/JSON/文本原文件，保留 URL、原始字节、编码和 SHA-256，导入只读 input 后用 Python 处理。
下载与 input 导入不设文件大小上限；input 从磁盘只读挂载，不再使用原有的 32 MiB 输入区。
下载目前仍有 45 秒总时限（且不超过研究剩余时间）、10 秒单次 socket 等待和最多 4 次重定向；仅接收公开文本数据，二进制 PDF/XLSX 不由此工具读取。
原文件须完整下载并按声明编码或 BOM 解码；CSV 链接返回 HTML、压缩传输未遵守 identity 请求或解码失败时会明确报错。
用户显式配置的 `storage_max_chars` 仍生效，默认不限制；work/output 的写入与附件交付配额独立于原件下载。
表格可直接交给 `bash` 中的 Python 按行列提取和计算；用 `source_refs` 声明输入来源（须是本轮已登记的来源编号，填错会在执行前退回）时，成功命令的非空 stdout 返回独立的计算结果 `source_ref`（标题写明输入文件），已展示部分可直接 `save_evidence`，不用重读原 CSV 的字符片段。未声明输入的输出（查看目录、调试打印等）只作工作参考，不登记为来源、不能存证。未展示输出按结果编号 `read_source` 续读；引用原文件原话时仍须实际读取原文件。
计算证据与原始资料分开标注，详情保留输入文件哈希、命令、工作目录及本轮脚本写入/修改记录；提示词要求输出行标识、字段、单位和计算方法。它提供过程追溯，不自动验证计算逻辑或建立单元格级血缘。执行预览和日志仍使用已有上限，日志不完整时有标记。
将网络配置改回 `none` 并重启即可恢复禁网模式；纯资料研究始终禁用 Web 下载和容器联网。
启用后，主、子 Agent 可按需使用文件和 Bash 工具，共用会话工作区；子任务交回文件及来源，主线统一交付。没有指定文件格式时照常交付站内正文。
要求 HTML、PDF 或导图时，交付文件会出现在答案下方，可预览或下载。HTML 可包含脚本交互、SVG、canvas 和外部资源；
只要求 HTML 不会自动附带 PDF。制作环境与文件支持范围见 [STATUS.md](STATUS.md)。

草稿、脚本和数据跟随研究会话保留，后续追问可以继续修改；已交付文件按轮保存独立副本。
容器回收后可重建并接回工作文件，后台进程和内存不会保留。

### 浏览器和图片检查

“设置 → 研究能力 → 浏览器”控制按需启动的 Playwright 浏览器。Web 默认开启，Python API 需显式设置 `BrowserConfig(enabled=True)`。
本机安装依赖（在项目根目录）：

```bash
npm ci --prefix web
node web/node_modules/playwright/cli.js install chromium
```

主、子 Agent 各自保持连续的页面会话，共用工作区文件。可读取动态网页、定位原图，也可打开实际报告检查断图、排版和交互。
截图通过与 `view_image` 相同的原生/辅助视觉通道传给模型；关闭看图时只保存图片，不声称已检查。
官方 `api.deepseek.com` 的已确认 Flash 型号可原生读图；同名中转模型仍以该连接的 API 或人工声明为准，未知能力继续使用配置的备用视觉模型。
自制报告、截图和工作文件只作交付检查，不能作为外部事实证据。浏览器使用本轮独立会话，不连接个人登录浏览器。
应用 Dockerfile 已包含 Node、Playwright MCP 和 Chromium 安装步骤；Docker 部署更新时需重建应用镜像，执行沙箱镜像无需因浏览器功能另行修改。

### 引文判断与上下文设置

使用 Jev 时在后端 `.env` 配置 `TYPESAFE_API_KEY`，然后启用设置页的“保存证据时用 Jev 判断引文支持”。
它会把主张、选中引文、补充上下文及来源标题/日期发送到 TypeSafe；设置作用于新研究和追问。
设置页也提供“自动整理长上下文”，启用前需填写模型实际支持的容量；当前默认与验收边界见 [STATUS.md](STATUS.md)。

### 本地资料研究

本地 RAG 是可选能力，会安装 `sentence-transformers` / Torch：

```bash
uv sync --extra local-rag
# 默认读取 data/models/ms_cache/BAAI/bge-m3；也可指定已有本地目录：
# export DRA_EMBEDDING_MODEL=/absolute/path/to/bge-m3
uv run --extra local-rag python -m dra.web
```

点输入框下方的“我的资料”，上传文件或点“导入示例资料”，待状态变为可用后勾选；勾选后来源会自动变成“网页 + 我的资料”，也可以在“来源”按钮里改成“仅我的资料”。外部 MCP 服务在“设置 → 外部资料”里添加，新研究时在“来源”里勾选。
“试查”只运行本地检索，不调用研究模型。完整研究沿用当前模型设置，读取片段会发送给该模型。
文件类型、限额与功能边界见 [STATUS](STATUS.md)。模型缺失会明确报错，不自动下载大模型；
可选重排读取同级 `bge-reranker-v2-m3`，也可用 `DRA_RERANKER_MODEL` 指定已有本地目录。

事件、来源快照、报告与本机模型配置写入 `DRA_DATA_DIR` 对应目录，不进入 Git。现役研究不支持安全续跑，`resume` 入口已删除；自主研究历史会话仍可查看、追问。

### 自动化入口

非交互自动化入口默认离线；批量 JSONL 每行是问题字符串或 `{"query": "研究问题"}`：

```bash
uv run python scripts/run_research.py "研究问题" --mode simple --dry-run
uv run python scripts/run_research.py --questions q.jsonl --limit 5 --mode simple
# 明确开启真实检索与付费模型；逐行输出状态、用量与目标覆盖
uv run python scripts/run_research.py "研究问题" --mode simple --run-live
```

脚本默认运行自主研究，`--mode simple` 可省略；旧 `full` 模式已删除。预算参数与默认值见 [STATUS.md](STATUS.md#3-关键默认值)。

## 验证

```bash
# 后端：默认跳过联网和付费用例
uv run pytest

# 本机 Chromium + 自建本机测试页面；不访问外部网页或调用模型
uv run pytest tests/test_delivery_browser.py --run-browser

# 仅在明确需要真实 API 验证时运行
uv run pytest --run-live

# 前端
cd web
npm test
npm run lint
npm run build
```

本副本的实际验证结果记录在 [STATUS.md](STATUS.md)。默认测试使用可控样本，不调用真实模型。

## 运行入口

| 入口 | 配置来源 | 用途 |
|---|---|---|
| 自动化 `scripts/run_research.py` | 本机模型配置 + 显式预算参数 | 默认离线、单问题或 JSONL 批量评测 |
| Web `python -m dra.web` | 本机 `runtime_config.json` | 主要使用与演示入口 |
| Python API `dra.research.run_minimal_research` | `SimpleConfig` | 主 Agent 自主研究 / 异步协作 + 引用门，拒绝恢复 |

Web 与自动化读取本机模型配置；Python API 接受显式配置。开发 live 测试使用用户指定的模型，见 STATUS；不会因此改写本机 Web 配置。旧编排引擎与交互 CLI 已删除。

每次研究的 `runs/<run_id>/search_calls.jsonl` 始终记录 Tavily、Exa 和触发时的 DDG 逐服务诊断，包含搜索词、主/子 Agent、模型与工具调用 ID、耗时、结果数、HTTP 状态和错误分类。`started/done` 是服务调用，`request_started/request_done` 是一次 HTTP 或 SDK 尝试；重试按 `attempt` 编号。缺密钥时只有服务失败记录，没有请求开始记录。Tavily/DDG SDK 未暴露的 HTTP 状态保持 `null`；尝试次数不代表服务商计费次数。记录不含请求头、密钥或上游错误原文，也不加入模型回执；历史运行无法补回这些诊断。

## 项目结构

```text
src/dra/        自主 Agent、工具、证据、持久化与 Web API
web/            React + Vite + TypeScript 工作台
tests/          默认离线的运行时回归测试
fixtures/       本地资料与演示运行的可控样本
docs/           检索来源决策
```

## 文档

- [当前状态](STATUS.md)：能力、默认值、验证结果、已知边界与退役范围
- [架构与决策](EXPERIMENT_PLAN.md)：数据责任、控制流和生产交付边界
- [检索来源决策](docs/RETRIEVAL_SOURCES_DECISION.md)：Tavily、Exa、DDG 与本地 RAG 的边界

## 技术栈

Python 3.12 · `asyncio` · Pydantic v2 · OpenAI-compatible SDK · Tavily / Exa / DDG · FastAPI · React / Vite / TypeScript · Vitest · Docker Compose

项目刻意不引入 LangGraph；旧离线排行榜、整篇报告 LLM judge、RACE/FACT、BEIR、gold set 与 A/B 评测脚手架均已退役，不再作为质量主张。现役 Jev 用于存证时的引文支持反馈。
