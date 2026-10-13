"""闭卷基线（B0）：不给任何工具，只问模型一次，看凭记忆能答对多少。

    uv run python eval/bcp/experiments/devtest-20261012/closed_book.py --split dev test --run-live

题目用 BrowseComp 原版作答格式（去掉搜索说明），模型与档位同研究配置。每题输出官方格式 JSON 到
``eval/bcp/runs/<批次>/<题号>.json``（retrieved_docids 为空），再用 ``eval/bcp/judge.py`` 以 Qwen3-32B 评分。
已有结果的题跳过，断了可接着跑。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import time

from dra import llm
from dra.durable import atomic_json

HERE = Path(__file__).resolve().parent
BCP = HERE.parents[1]
TEMPLATE = """Question: {question}

You have no tools; answer from your own knowledge.

Your response should be in the following format:
Explanation: {{your explanation for your final answer}}
Exact Answer: {{your succinct, final answer}}
Confidence: {{your confidence score between 0% and 100% for your answer}}"""


def ask(row, out, *, model, provider, effort):
    path = out / f"{row['query_id']}.json"
    if path.exists():
        return json.loads(path.read_text())
    meter = llm.UsageMeter()
    started = time.monotonic()
    error = None
    text = ""
    with llm.execution_scope(meter=meter):
        try:
            text = llm.chat([{"role": "user", "content": TEMPLATE.format(question=row["query"])}],
                            model=model, provider=provider, reasoning=True, effort=effort,
                            max_tokens=32768, on_chunk=lambda _: None)
        except Exception as exc:  # 失败如实记录，不当成答案
            error = f"{type(exc).__name__}: {exc}"[:500]
    record = {"query_id": row["query_id"], "status": "completed" if text.strip() else "error",
              "tool_call_counts": {}, "retrieved_docids": [],
              "result": [{"type": "output_text", "output": text}],
              "meta": {"protocol": "closed_book", "model": model, "provider": provider, "effort": effort,
                       "wall_seconds": round(time.monotonic() - started, 1), "usage": meter.snapshot(),
                       "error": error}}
    if error is None:
        atomic_json(path, record)
    return record


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--split", nargs="+", choices=["dev", "test"], required=True)
    p.add_argument("--batch", default="closedbook-devtest-20261012")
    p.add_argument("--model", default="deepseek-v4.1-flash")
    p.add_argument("--provider", default="custom_781f04cba39a466e")
    p.add_argument("--effort", default="low")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--run-live", action="store_true")
    args = p.parse_args(argv)
    split = json.loads((HERE / "split.json").read_text())
    ids = {str(i) for s in args.split for i in split[s]["ids"]}
    rows = []
    with (BCP / "data/browsecomp_plus_decrypted.jsonl").open(encoding="utf-8") as source:
        for line in source:  # 文件约 2GB（含证据文档全文），只解析选中的题
            qid = json.loads(line[:line.index(',')] + "}")["query_id"]
            if qid in ids:
                rows.append(json.loads(line))
    print(json.dumps({"questions": len(rows), "model": args.model, "provider": args.provider,
                      "effort": args.effort, "batch": args.batch, "run_live": args.run_live}), flush=True)
    if not args.run_live:
        return
    out = BCP / "runs" / args.batch
    out.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(ask, r, out, model=args.model, provider=args.provider, effort=args.effort): r
                   for r in rows}
        for future in as_completed(futures):
            rec = future.result()
            print(json.dumps({"id": rec["query_id"], "status": rec["status"],
                              "seconds": rec["meta"]["wall_seconds"], "error": rec["meta"]["error"]}), flush=True)


if __name__ == "__main__":
    main()
