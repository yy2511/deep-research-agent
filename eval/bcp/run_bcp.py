"""用 BrowseComp-Plus 的题目跑研究 Agent，按官方格式保存结果，不打分。

    uv run python eval/bcp/run_bcp.py --sample 3               # 只打印将要跑哪几题
    uv run python eval/bcp/run_bcp.py --sample 3 --run-live    # 真跑：调用研究模型

先启动本机检索服务（见 search_server.py）。抽题用固定随机种子，同样的 --sample/--seed 每次抽到同一批。

评测环境与产品的区别（只在本脚本里改，产品代码不动）：
- 搜索换成查 BrowseComp-Plus 固定文档库（BM25，每次 5 条），正文随结果带回，read_source 直接读；
- 按网址读网页一律拒绝，否则 Agent 可能上真实网络找到答案，结果作废；被拒次数记在 meta 里；
- 关闭代码执行；不接外部资料服务（MCP）、不用本地资料库（这条调用路径本来就不启用）；
- 跳过澄清，直接把题目交给研究；时间上限 900 秒，与 Question's Gambit 论文一致。
- 题目用官方的作答格式模板（不要求按文档编号引用，见 QUERY_TEMPLATE）。

其他照产品原样，包括搜索结果摘要截到 600 字符（官方是 512 token），记录时需注明。

每题输出到 ``eval/bcp/runs/<批次>/``：``<题号>.json``（官方评分脚本要的格式，另附 meta 字段）
和 ``<题号>.md``（完整报告）。完整运行轨迹在数据目录的 runs/ 下，run_id 记在 meta 里。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

from dra import research, runtime_config, simple_research
from dra.execution import ExecutionConfig
from dra.models import RetrievedDoc
from dra.paths import RUNS_DIR
from dra.tools import UrlFetchError

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "data" / "browsecomp_plus_decrypted.jsonl"
SEARCH_URL = "http://127.0.0.1:8790"
DOC_URL = "https://bcp.invalid/doc/"  # 文档身份：.invalid 是保留域名，不会解析到真实网站
TOP_K = 5
WALL_TIMEOUT_S = 900.0
MODEL, PROVIDER = "deepseek-v4.1-flash", "opencode"  # 主、子 Agent 共用；思考开关与强度沿用网页设置

# 官方 search_agent/prompts.py 的 QUERY_TEMPLATE_NO_GET_DOCUMENT_NO_CITATION，原文照搬。
# 不用要求 [docid] 引用的版本：我们的 Agent 按证据卡引用，正确率评分只看最终答案。
QUERY_TEMPLATE = """
You are a deep research agent. You need to answer the given question by interacting with a search engine, using the search tool provided. Please perform reasoning and use the tool step by step, in an interleaved manner. You may use the search tool multiple times.

Question: {Question}

Your response should be in the following format:
Explanation: {{your explanation for your final answer}}
Exact Answer: {{your succinct, final answer}}
Confidence: {{your confidence score between 0% and 100% for your answer}}
""".strip()


def load_questions() -> list[dict]:
    """只取题号、题目、答案和标注文档编号；原文件每行带全部证据文档正文，很大。"""
    rows = []
    with QUESTIONS.open(encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            rows.append({"query_id": row["query_id"], "query": row["query"], "answer": row["answer"],
                         "evidence_docids": [d["docid"] for d in row["evidence_docs"]],
                         "gold_docids": [d["docid"] for d in row["gold_docs"]]})
    return rows


def pick(questions: list[dict], sample: int | None, seed: int, ids: list[str] | None) -> list[dict]:
    if ids:
        by_id = {q["query_id"]: q for q in questions}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise SystemExit(f"题号不存在：{missing}")
        return [by_id[i] for i in ids]
    order = sorted(questions, key=lambda q: int(q["query_id"]))
    return random.Random(seed).sample(order, sample)


def _title(text: str, docid: str) -> str:
    """文档开头的 front matter 里有 title；没有就用第一行。"""
    m = re.match(r"---\n(?:.*\n)*?title:\s*(.+)\n", text)
    if m:
        return m.group(1).strip()
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:120] or f"文档 {docid}"


def docid_of(url: str | None) -> str | None:
    return url[len(DOC_URL):] if url and url.startswith(DOC_URL) else None


class Corpus:
    """接本机检索服务；记下搜索次数和搜索返回过的全部文档编号（主 Agent 与子 Agent 共用）。"""

    def __init__(self, base: str = SEARCH_URL, k: int = TOP_K):
        self.base, self.k = base, k
        self.searches = 0
        self.retrieved: list[str] = []
        self._lock = threading.Lock()

    def search(self, query: str) -> list[RetrievedDoc]:
        r = requests.get(f"{self.base}/search", params={"q": query, "k": self.k}, timeout=60)
        r.raise_for_status()
        hits = r.json()["hits"]
        with self._lock:
            self.searches += 1
            self.retrieved.extend(h["docid"] for h in hits if h["docid"] not in self.retrieved)
        return [RetrievedDoc(source_url=DOC_URL + h["docid"], title=_title(h["text"], h["docid"]),
                             snippet=h["text"][:2000], raw_content=h["text"], score=h["score"])
                for h in hits]


class FetchBlock:
    """替换按网址读网页：评测里一律拒绝，并记下 Agent 想读哪些网址。"""

    def __init__(self):
        self.attempts: list[str] = []

    def __call__(self, url, **_kw):
        self.attempts.append(url)
        raise UrlFetchError("评测环境不能读取网页；只能用 search_sources 检索文档库，再用 source_ref 阅读。")


def eval_config(model: str = MODEL, provider: str = PROVIDER, **overrides) -> simple_research.SimpleConfig:
    """网页设置为底，只换研究模型；不改写网页的设置文件。"""
    settings = runtime_config.current()
    settings = settings.model_copy(update={
        "subagent": settings.subagent.model_copy(update={"model": model, "provider": provider})})
    config = runtime_config.to_simple_config(
        settings, execution=ExecutionConfig(enabled=False), wall_timeout_s=WALL_TIMEOUT_S, **overrides)
    if config.mcp_servers or config.source_mode != "web" or config.execution.enabled:
        raise SystemExit("评测配置不对：必须不接外部资料服务、不用资料库、不开代码执行")
    return config


def run_one(question: dict, out: Path, *, corpus: Corpus | None = None, model_call=None,
            config: simple_research.SimpleConfig | None = None) -> dict:
    corpus = corpus or Corpus()
    config = config or eval_config()
    block = FetchBlock()
    run_id = f"bcp-{question['query_id']}-{datetime.now():%Y%m%d-%H%M%S}"
    original_fetch = simple_research.fetch_url_content
    simple_research.fetch_url_content = block
    started = time.monotonic()
    try:
        result = research.run_minimal_research(
            QUERY_TEMPLATE.format(Question=question["query"]), config=config, run_id=run_id,
            checkpoint_dir=RUNS_DIR / run_id, search_fn=corpus.search, model_call=model_call)
    finally:
        simple_research.fetch_url_content = original_fetch
    read_docids = sorted({d for s in result.sources if s.snapshot_kind == "full" and (d := docid_of(s.url))},
                         key=int)
    cited_docids = sorted({d for c in result.cards for q in c.evidence_quotes() if (d := docid_of(q.source_url))},
                          key=int)
    evidence = set(question["evidence_docids"])
    record = {
        # 官方评分脚本读的字段
        "query_id": question["query_id"],
        "tool_call_counts": {"search": corpus.searches, "get_document": len(read_docids)},
        "status": "completed" if result.delivery_status == "delivered" and result.draft.strip() else result.delivery_status,
        "retrieved_docids": corpus.retrieved,
        "result": [{"type": "output_text", "output": result.draft}],
        # 我们自己的记录，官方脚本会忽略
        "meta": {
            "run_id": run_id,
            "model": f"{config.adapter.model}@{config.adapter.provider}",
            "wall_timeout_s": config.wall_timeout_s,
            "wall_seconds": round(time.monotonic() - started, 1),
            "stop_reason": result.stop_reason, "delivery_status": result.delivery_status,
            "read_docids": read_docids, "cited_docids": cited_docids,
            "evidence_retrieved": len(evidence & set(corpus.retrieved)),
            "evidence_read": len(evidence & set(read_docids)),
            "evidence_total": len(evidence),
            "cards": len(result.cards),
            "blocked_url_reads": block.attempts,
            "usage": result.usage, "warnings": result.warnings,
        },
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{question['query_id']}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / f"{question['query_id']}.md").write_text(simple_research.render_markdown(result), encoding="utf-8")
    return record


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sample", type=int, default=3, help="随机抽几题")
    p.add_argument("--seed", type=int, default=20261002, help="抽题随机种子")
    p.add_argument("--ids", nargs="+", help="直接指定题号（不抽题）")
    p.add_argument("--batch", default=datetime.now().strftime("%Y%m%d-%H%M"), help="输出批次目录名")
    p.add_argument("--model", default=MODEL, help="研究模型（主、子 Agent 共用）")
    p.add_argument("--provider", default=PROVIDER, help="模型服务商，见网页设置里的连接")
    p.add_argument("--run-live", action="store_true", help="真跑（调用研究模型）")
    args = p.parse_args(argv)
    questions = pick(load_questions(), args.sample, args.seed, args.ids)
    for q in questions:
        print(f"{q['query_id']}: {q['query'][:100]}…", file=sys.stderr)
    try:
        requests.get(f"{SEARCH_URL}/search", params={"q": "test", "k": 1}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        raise SystemExit(f"检索服务没起来（{exc}）；先按 search_server.py 的说明启动") from exc
    config = eval_config(args.model, args.provider)
    if not args.run_live:
        print(f"未加 --run-live，不运行。将使用模型 {config.adapter.model}@{config.adapter.provider}，"
              f"时间上限 {config.wall_timeout_s:.0f} 秒", file=sys.stderr)
        return
    out = HERE / "runs" / args.batch
    for q in questions:
        record = run_one(q, out, config=config)
        meta = record["meta"]
        print(json.dumps({"query_id": q["query_id"], "status": record["status"],
                          "wall_seconds": meta["wall_seconds"], "searches": record["tool_call_counts"]["search"],
                          "evidence": f"搜到 {meta['evidence_retrieved']} / 读了 {meta['evidence_read']} / 共 {meta['evidence_total']}",
                          "blocked_url_reads": len(meta["blocked_url_reads"])}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
