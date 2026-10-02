# BrowseComp-Plus 评测

计划与背景见 `eval/README.md` 第 4 节。这里只记怎么跑和跑出了什么。

## 准备（一次性，不花钱）

数据放在 `eval/bcp/data/`（不入库，约 4GB）：

- 题目：Hugging Face `Tevatron/browsecomp-plus` 的 6 个 parquet 用 curl 下载（官方下载器在本机网络下极慢），再用官方 `decrypt_dataset.py` 的解密函数得到 `browsecomp_plus_decrypted.jsonl`（830 题，带答案与证据文档）。
- 索引：`Tevatron/browsecomp-plus-indexes` 的 `bm25/` 目录，同样用 curl 逐个下载，大小与官方列表逐个核对。
- 分词器：`Qwen/Qwen3-0.6B` 的 tokenizer.json、tokenizer_config.json、vocab.json、merges.txt 等用 curl 下到 `data/qwen3-tokenizer/`，检索服务用它把摘要截到 512 token（与官方相同）。
- Java 21：`brew install openjdk@21`。Pyserini 不进项目依赖，检索服务用 uv 临时环境启动。

## 运行

```bash
# 1. 检索服务（另开终端，常驻）
JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \
  uv run --no-project --python 3.11 --with pyserini==1.2.0 python eval/bcp/search_server.py

# 2. 跑题（不加 --run-live 只打印抽到的题；同批次已有结果的题自动跳过，断了可接着跑）
uv run python eval/bcp/run_bcp.py --sample 30 --workers 3 --batch <批次> --run-live

# 3. 评分（Qwen3-32B，走 .env 的 BCP_BASE_URL / BCP_API_KEY）
uv run --no-project --with numpy --with tqdm --with requests --with python-dotenv \
  python eval/bcp/judge.py runs/<批次>
```

结果：`runs/<批次>/<题号>.json`（官方格式 + meta）、`<题号>.md`（完整报告）；评分在 `evals/<批次>/`。

跑题脚本默认关闭派子任务和长文压缩，`--subagents` / `--condense` 可打开；设置记在每题 meta 里。

### 对照组：官方标准 Agent 接同一模型

```bash
JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home \
  uv run --no-project --python 3.11 --with pyserini==1.2.0 --with rich --with python-dotenv \
  python eval/bcp/run_official.py --ids 263 1072 237 --threads 3 --max-tokens 200000 --batch official-<批次>
```

用官方 `search_agent/glm_zai_client.py`（通用 OpenAI 兼容函数调用 Agent，GLM-4.6 基线所用），只换服务地址
（opencode，带会话头）、分词器来源（本地同一份）和向量检索模块（空壳）。官方框架没有时间上限，靠输出总预算
（思考也计入）与最多 100 轮收尾；deepseek 思考较长，官方建议的 4 万曾在没交答案前用完，故放宽到 20 万。

## 与官方设置的差别

| 项目 | 官方 | 我们 | 原因 |
|---|---|---|---|
| Agent 框架 | 官方标准 Agent（search + get_document） | 我们的研究框架（主 Agent + 子 Agent + 证据卡） | 要测的就是框架 |
| 搜索 | BM25，前 5 条，摘要为全文前 512 token | 相同（同一索引、同一分词器；为提速只对前 2 万字符分词，300 篇抽查与整篇分词逐字一致） | 2026-10-02 起对齐；pilot 批次是产品原样的 600 字符 |
| 读全文 | get_document | read_source 读同一篇全文 | 等价 |
| 联网 | 无 | 按网址读网页一律拒绝（次数记在 meta.blocked_url_reads） | 防止上真实网络找答案 |
| 题目模板 | 官方 QUERY_TEMPLATE 系列 | 官方 `QUERY_TEMPLATE_NO_GET_DOCUMENT_NO_CITATION` 原文 | 我们按证据卡引用，不按文档编号 |
| 引用 | Agent 自己写 [docid] | 交评分的正文里把证据卡 [c1] 换成卡片来源文档编号 [5412]；.md 报告保留原样 | 官方脚本据此算引用精确率与召回率 |
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
- 事后按证据卡换算引用：引用精确率平均 84%、召回率 76%（263：86%/86%，1072：100%/75%，237：67%/67%）。
- 本批摘要是产品原样的 600 字符（官方 512 token 约 2000 字符），之后的批次已对齐；之后的批次里，搜索结果还附带全文字数（产品改动，见 `search_snippet_chars` / `total_chars`）。两者不直接可比。
- 搜索次数明显多于论文里的 GPT-5（约 22 次）；237 派了子任务，搜索 148 次、用量是另两题的 3 倍。
- 237 有 4 次试图读维基百科、DuckDuckGo 等真实网址，均被拦下：断网拦截是必要的。
- 题目来自公开的 BrowseComp，模型可能凭记忆答对；要区分需先跑闭卷（B0）。

### 2026-10-02 同 3 题的后续批次（deepseek-v4.1-flash@opencode，BM25，摘要 512 token）

| 批次 | 设置 | 263 | 1072 | 237 | 输入合计 | 平均搜索 | 引用精确率 / 召回率 |
|---|---|---|---|---|---|---|---|
| parallel3 | 我们的框架，子任务开、压缩开 | 对（75 万） | 错（626 万） | 对（719 万） | 1420 万 | 97 | 67% / 50% |
| solo | 我们的框架，子任务关、压缩关 | 对（190 万） | 对（597 万） | 错（384 万） | 1170 万 | 65 | 92% / 61% |
| official-t1 | 官方框架，输出预算 4 万 | 预算用完未作答（328 万，搜 67 次） | | | | | |
| official-t2 | 官方框架，输出预算 20 万 | 错，自称无法确定（206 万，90% 命中缓存，搜 52 次，证据召回 0） | | | | | |

- 每道题都在某次运行中答错过：3 题内的差别主要是运行间波动，比较设置需更多题、多次运行。
- 关掉子任务后不再出现多个 Agent 重复检索；关掉压缩后没有出现整篇读入长文。
- 摘要变长后输入随搜索次数累积：solo 的 1072 单次调用输入达 33.5 万 token。
