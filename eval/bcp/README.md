# BrowseComp-Plus 评测

计划与背景见 `eval/README.md` 第 4 节。这里只记怎么跑和跑出了什么。

## 准备（一次性，不花钱）

数据放在 `eval/bcp/data/`（不入库，约 4GB）：

- 题目：Hugging Face `Tevatron/browsecomp-plus` 的 6 个 parquet 用 curl 下载（官方下载器在本机网络下极慢），再用官方 `decrypt_dataset.py` 的解密函数得到 `browsecomp_plus_decrypted.jsonl`（830 题，带答案与证据文档）。
- 索引：`Tevatron/browsecomp-plus-indexes` 的 `bm25/` 目录，同样用 curl 逐个下载，大小与官方列表逐个核对。
- Java 21：`brew install openjdk@21`。Pyserini 不进项目依赖，检索服务用 uv 临时环境启动。

## 运行

```bash
# 1. 检索服务（另开终端，常驻）
JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \
  uv run --no-project --python 3.11 --with pyserini==1.2.0 python eval/bcp/search_server.py

# 2. 跑题（不加 --run-live 只打印抽到的题）
uv run python eval/bcp/run_bcp.py --sample 3 --batch <批次> --run-live

# 3. 评分（Qwen3-32B，走 .env 的 BCP_BASE_URL / BCP_API_KEY）
uv run --no-project --with numpy --with tqdm --with requests --with python-dotenv \
  python eval/bcp/judge.py runs/<批次>
```

结果：`runs/<批次>/<题号>.json`（官方格式 + meta）、`<题号>.md`（完整报告）；评分在 `evals/<批次>/`。

## 与官方设置的差别

| 项目 | 官方 | 我们 | 原因 |
|---|---|---|---|
| Agent 框架 | 官方标准 Agent（search + get_document） | 我们的研究框架（主 Agent + 子 Agent + 证据卡） | 要测的就是框架 |
| 搜索 | BM25，前 5 条，摘要 512 token | 同一 BM25 索引、前 5 条；摘要按产品原样截 600 字符 | 产品行为不为评测改 |
| 读全文 | get_document | read_source 读同一篇全文 | 等价 |
| 联网 | 无 | 按网址读网页一律拒绝（次数记在 meta.blocked_url_reads） | 防止上真实网络找答案 |
| 题目模板 | 官方 QUERY_TEMPLATE 系列 | 官方 `QUERY_TEMPLATE_NO_GET_DOCUMENT_NO_CITATION` 原文 | 我们按证据卡引用，不按文档编号 |
| 时间 | 无统一上限 | 900 秒（同 Question's Gambit） | |
| 评分 | 本地 vLLM 跑 Qwen3-32B，enable_thinking=False | 官方 evaluate_run.py 原样运行，模型改走 API；用 `/no_think` 关思考 | 本机无显卡；该 API 不认 enable_thinking 参数 |

向量检索（Qwen3-Embedding-8B）暂未接入；已验证同一 API 的向量与官方索引一致（6 篇相似度 ≥ 0.9999）。

## 试跑记录

### 2026-10-02 pilot（3 题，种子 20261002）

模型 deepseek-v4.1-flash@opencode（主、子 Agent 共用，思考开、强度 low），BM25。

| 题号 | 结果 | 耗时 | 搜索次数 | 证据文档：搜到 / 读了 / 共 | 输入 / 输出 token | 拦下的网址读取 |
|---|---|---|---|---|---|---|
| 263 | 对（Simerly，置信 92%） | 209 秒 | 38 | 6 / 6 / 7 | 118 万 / 2 万 | 0 |
| 1072 | 对（Iranian Ministry of Culture and Higher Education，85%） | 415 秒 | 44 | 8 / 6 / 8 | 148 万 / 6 万 | 0 |
| 237 | 对（Edward Winslow，68%） | 483 秒 | 148 | 2 / 2 / 3 | 467 万 / 11 万 | 4 |

- 正确率 3/3，证据召回率 84.1%（官方口径：搜索返回过的文档覆盖多少标注证据）。3 题只说明流程跑通，不能代表水平（误差极大）。
- 搜索次数明显多于论文里的 GPT-5（约 22 次）；237 派了子任务，搜索 148 次、用量是另两题的 3 倍。
- 237 有 4 次试图读维基百科、DuckDuckGo 等真实网址，均被拦下：断网拦截是必要的。
- 题目来自公开的 BrowseComp，模型可能凭记忆答对；要区分需先跑闭卷（B0）。
