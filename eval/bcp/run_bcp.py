"""用 BrowseComp-Plus 的题目跑研究 Agent，按官方格式保存结果，不打分。

    uv run python eval/bcp/run_bcp.py --sample 30                          # 只打印将要跑哪几题
    uv run python eval/bcp/run_bcp.py --sample 30 --workers 3 --run-live   # 真跑：同时跑 3 题

先启动本机检索服务（见 search_server.py）。抽题用固定随机种子，同样的 --sample/--seed 每次抽到同一批。
每题一个独立进程（各自替换读网页函数、各自计数）；同一批次里已有结果文件的题跳过，断了可接着跑。

评测环境与产品的区别（只在本脚本里改，产品代码不动）：
- 搜索换成查 BrowseComp-Plus 固定文档库（BM25，每次 5 条），正文随结果带回，read_source 直接读；
  摘要与官方一致（全文前 512 个 token，由检索服务截好），产品的 600 字符摘要上限在评测里放开；
- 按网址读网页一律拒绝，否则 Agent 可能上真实网络找到答案，结果作废；被拒次数记在 meta 里；
- 关闭代码执行；不接外部资料服务（MCP）、不用本地资料库（这条调用路径本来就不启用）；
- 跳过澄清，直接把题目交给研究；时间上限 900 秒，与 Question's Gambit 论文一致。
- 默认关闭长文压缩（读长文不再先压缩成摘要）和派子任务（主 Agent 独自完成），可用
  --condense / --subagents 打开，用于对比；设置记在每题 meta 里。
- 题目用官方的作答格式模板（不要求按文档编号引用，见 QUERY_TEMPLATE）。交给评分的正文里，
  证据卡引用 [c1] 换成卡片来源的文档编号 [5412]，官方脚本据此算引用精确率与召回率；
  完整报告（.md）保留原样。

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
from concurrent.futures import ProcessPoolExecutor, as_completed
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
SNIPPET_CHARS = 20_000  # 产品摘要上限放到足够大：检索服务已按官方截到 512 token（约 2000 字符）
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


_CARD_CITE = re.compile(r"\[(c\d+(?:\s*[,，;；]\s*c\d+)*)\]")


def cite_docids(draft: str, cards) -> str:
    """把正文里的证据卡引用 [c1] / [c1, c2] 换成来源文档编号 [5412] / [5412, 82002]。

    一张卡可能摘自多篇文档，全部列出；正文引用了不存在的卡就原样保留，不猜。"""
    docs = {c.card_id: [d for q in c.evidence_quotes() if (d := docid_of(q.source_url))] for c in cards}

    def swap(m):
        ids = [i.strip() for i in re.split(r"[,，;；]", m.group(1))]
        if any(i not in docs for i in ids):
            return m.group(0)
        found = list(dict.fromkeys(d for i in ids for d in docs[i]))
        return f"[{', '.join(found)}]" if found else m.group(0)

    return _CARD_CITE.sub(swap, draft)


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
                             snippet=h["snippet"], raw_content=h["text"], score=h["score"])
                for h in hits]


class FetchBlock:
    """替换按网址读网页：评测里一律拒绝，并记下 Agent 想读哪些网址。"""

    def __init__(self):
        self.attempts: list[str] = []

    def __call__(self, url, **_kw):
        self.attempts.append(url)
        raise UrlFetchError("评测环境不能读取网页；只能用 search_sources 检索文档库，再用 source_ref 阅读。")


def eval_config(model: str = MODEL, provider: str = PROVIDER, *, subagents: bool = False, condense: bool = False,
                **overrides) -> simple_research.SimpleConfig:
    """网页设置为底，只换研究模型；不改写网页的设置文件。"""
    settings = runtime_config.current()
    settings = settings.model_copy(update={
        "subagent": settings.subagent.model_copy(update={"model": model, "provider": provider})})
    config = runtime_config.to_simple_config(
        settings, execution=ExecutionConfig(enabled=False), wall_timeout_s=WALL_TIMEOUT_S,
        search_snippet_chars=SNIPPET_CHARS, delegation_enabled=subagents,
        **({} if condense else {"condense_over_chars": None}), **overrides)
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
        "result": [{"type": "output_text", "output": cite_docids(result.draft, result.cards)}],
        # 我们自己的记录，官方脚本会忽略
        "meta": {
            "run_id": run_id,
            "model": f"{config.adapter.model}@{config.adapter.provider}",
            "wall_timeout_s": config.wall_timeout_s,
            "subagents": config.delegation_enabled, "condense": config.condense_over_chars is not None,
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


def summary_line(record: dict) -> dict:
    meta = record["meta"]
    return {"query_id": record["query_id"], "status": record["status"],
            "wall_seconds": meta["wall_seconds"], "searches": record["tool_call_counts"]["search"],
            "evidence": f"搜到 {meta['evidence_retrieved']} / 读了 {meta['evidence_read']} / 共 {meta['evidence_total']}",
            "blocked_url_reads": len(meta["blocked_url_reads"])}


def _worker(question: dict, out: Path, model: str, provider: str, subagents: bool, condense: bool) -> dict:
    """子进程里跑一题。出错只记这一题、不写结果文件（下次同批次会重跑它），不拖垮整批。"""
    try:
        config = eval_config(model, provider, subagents=subagents, condense=condense)
        return summary_line(run_one(question, out, config=config))
    except Exception as exc:  # noqa: BLE001  整批评测里单题失败要可见、可重跑
        return {"query_id": question["query_id"], "status": "error", "error": f"{type(exc).__name__}: {exc}"}


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sample", type=int, default=3, help="随机抽几题")
    p.add_argument("--seed", type=int, default=20261002, help="抽题随机种子")
    p.add_argument("--ids", nargs="+", help="直接指定题号（不抽题）")
    p.add_argument("--batch", default=datetime.now().strftime("%Y%m%d-%H%M"), help="输出批次目录名")
    p.add_argument("--workers", type=int, default=1, help="同时跑几题（每题一个进程）")
    p.add_argument("--model", default=MODEL, help="研究模型（主、子 Agent 共用）")
    p.add_argument("--provider", default=PROVIDER, help="模型服务商，见网页设置里的连接")
    p.add_argument("--subagents", action="store_true", help="允许主 Agent 派子任务（默认关）")
    p.add_argument("--condense", action="store_true", help="开启长文压缩（默认关）")
    p.add_argument("--run-live", action="store_true", help="真跑（调用研究模型）")
    args = p.parse_args(argv)
    questions = pick(load_questions(), args.sample, args.seed, args.ids)
    out = HERE / "runs" / args.batch
    todo = [q for q in questions if not (out / f"{q['query_id']}.json").exists()]
    for q in questions:
        mark = "" if q in todo else "（已有结果，跳过）"
        print(f"{q['query_id']}{mark}: {q['query'][:100]}…", file=sys.stderr)
    try:
        requests.get(f"{SEARCH_URL}/search", params={"q": "test", "k": 1}, timeout=10).raise_for_status()
    except requests.RequestException as exc:
        raise SystemExit(f"检索服务没起来（{exc}）；先按 search_server.py 的说明启动") from exc
    config = eval_config(args.model, args.provider, subagents=args.subagents, condense=args.condense)
    print(f"共 {len(questions)} 题，待跑 {len(todo)} 题；模型 {config.adapter.model}@{config.adapter.provider}，"
          f"时间上限 {config.wall_timeout_s:.0f} 秒，同时跑 {args.workers} 题；"
          f"子任务{'开' if args.subagents else '关'}，长文压缩{'开' if args.condense else '关'}", file=sys.stderr)
    if not args.run_live:
        print("未加 --run-live，不运行。", file=sys.stderr)
        return
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_worker, q, out, args.model, args.provider, args.subagents, args.condense)
                   for q in todo]
        for done, future in enumerate(as_completed(futures), 1):
            print(json.dumps({"done": f"{done}/{len(todo)}", **future.result()}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
