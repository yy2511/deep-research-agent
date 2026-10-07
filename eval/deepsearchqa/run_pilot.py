"""固定抽样的真实 Web 试跑。默认只预览；--run-live 才调用模型和搜索。"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import csv
import random
import re
import subprocess
import threading
import time

from dra import llm, research, runtime_config, simple_research, subagent, file_sources
from dra.durable import atomic_json
from dra.execution import ExecutionConfig, ExecutionError
from dra.evidence_judge import JudgeConfig
from dra.vision import VisionConfig
from dra.paths import RUNS_DIR
from dra.site_filter import provider_exclude_domains
from dra.tools import UrlFetchError

HERE = Path(__file__).resolve().parent
# 本次对话展示过的例题；题号为固定 CSV 的零起始行号。
EXPOSED_IDS = {12, 17, 51, 61, 79, 268}
BLOCKED_SITES = ["huggingface.co/datasets/google/deepsearchqa",
                 "kaggle.com/benchmarks/google/dsqa",
                 "kaggle.com/code/andrewmingwang/deepsearchqa-starter-code"]
LEAKAGE_MARKER = re.compile(r"deep[\s_-]*search[\s_-]*qa|DSQA-full\.csv", re.I)


def load_questions():
    from prepare import DATA_SHA256
    path = HERE / "data/DSQA-full.csv"
    if hashlib.sha256(path.read_bytes()).hexdigest() != DATA_SHA256:
        raise ValueError("Dataset checksum mismatch; run prepare.py")
    with path.open(encoding="utf-8-sig", newline="") as source:
        return [{"id": i, **row} for i, row in enumerate(csv.DictReader(source))]


def pick(rows, sample=5, seed=20261002, *, ids=None):
    if ids is not None:
        by_id = {r["id"]: r for r in rows}
        if not ids or len(ids) != len(set(ids)) or any(i not in by_id for i in ids):
            raise ValueError("Question ids must be nonempty, unique, and present in the dataset")
        return [by_id[i] for i in ids]
    return random.Random(seed).sample([r for r in rows if r["id"] not in EXPOSED_IDS], sample)


def research_config(settings, seconds):
    return runtime_config.to_simple_config(
        settings, wall_timeout_s=seconds, execution=ExecutionConfig(enabled=False), vision=VisionConfig(enabled=False),
        evidence_judge=JudgeConfig(enabled=False),
        blocked_sites=BLOCKED_SITES, source_mode="web", mcp_servers=[])


class WebSearch:
    """仍用产品的搜索与全文阅读，只屏蔽可识别的基准答案页面。"""

    def __init__(self, config):
        self.config = config
        self.searches = 0
        self.blocked = []
        self.blocked_originals = []
        self.lock = threading.Lock()

    def leaked(self, url, *texts):
        match = any(LEAKAGE_MARKER.search(t or "") for t in texts)
        if match:
            with self.lock:
                self.blocked.append(url)
        return bool(match)

    def search(self, query):
        with self.lock:
            self.searches += 1
        docs = subagent._retrieve(query, self.config.adapter,
                                  exclude_domains=provider_exclude_domains(self.config.blocked_sites) or None)
        return [d for d in docs if not self.leaked(d.source_url, d.title, d.snippet, d.raw_content)]

    def wrap_fetch(self, original):
        def fetch(url, **kwargs):
            result = original(url, **kwargs)
            if self.leaked(url, result.get("title"), result.get("content")):
                raise UrlFetchError("Benchmark answer page excluded from evaluation")
            return result
        return fetch

    def block_original(self, url, **kwargs):
        with self.lock:
            self.blocked_originals.append(url)
        raise ExecutionError('benchmark_download_disabled', '本评测仅使用受泄题过滤保护的文字阅读；原件下载未启用。')


def delivered_answer(result):
    return "" if result.draft_rejected else result.draft.strip()


def run_one(question, out, config, *, run_fn=None):
    """question 只含 id/problem/category，标准答案与答案类型不进入研究进程。"""
    run_fn = run_fn or research.run_minimal_research
    config = config.model_copy(update={'vision': config.vision.model_copy(update={'enabled': False})})
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    run_id = f"dsqa-{question['id']}-{datetime.now():%Y%m%d-%H%M%S-%f}"
    guard = WebSearch(config)
    old_fetch = simple_research.fetch_url_content
    old_download = file_sources.fetch_original
    simple_research.fetch_url_content = guard.wrap_fetch(old_fetch)
    file_sources.fetch_original = guard.block_original
    trace_token = llm.set_trace_full_io(True)
    started = time.monotonic()
    record = {**question, "run_id": run_id, "response": "", "status": "error",
              "prompt_profile": config.prompt_profile}
    atomic_json(out / "progress.json", {**record, "status": "running"})
    try:
        result = run_fn(question["problem"], config=config, run_id=run_id,
                        checkpoint_dir=RUNS_DIR / run_id, search_fn=guard.search)
        answer = delivered_answer(result)
        record.update(response=answer, status="completed" if answer else "no_answer",
                      delivery_status=result.delivery_status, stop_reason=result.stop_reason,
                      draft_rejected=result.draft_rejected, cards=len(result.cards),
                      sources=len(result.sources), usage=result.usage,
                      llm_calls=len(result.calls), warnings=result.warnings)
        record["provider_balance_error"] = any(
            "insufficient balance" in (call.error or "").lower() for call in result.calls)
        (out / "answer.md").write_text(record["response"] + "\n", encoding="utf-8")
        (out / "report.md").write_text(simple_research.render_markdown(result), encoding="utf-8")
    except Exception as exc:
        record.update(error=type(exc).__name__, http_status=getattr(exc, "status_code", None))
    finally:
        simple_research.fetch_url_content = old_fetch
        file_sources.fetch_original = old_download
        llm.reset_trace_full_io(trace_token)
    record.update(wall_seconds=round(time.monotonic() - started, 2), searches=guard.searches,
                  blocked_benchmark_pages=guard.blocked, blocked_original_downloads=guard.blocked_originals)
    atomic_json(out / "result.json", record)
    atomic_json(out / "progress.json", record)
    return record


def _worker(question, out, config_data):
    return run_one(question, out, simple_research.SimpleConfig.model_validate(config_data))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261002)
    parser.add_argument("--ids", type=int, nargs="+", help="显式指定零起始题号，用新批次做定向复跑")
    parser.add_argument("--batch", default="pilot-20261002")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--model", help="仅覆盖本批研究模型，不修改网页设置")
    parser.add_argument("--provider", help="仅覆盖本批连接，不修改网页设置")
    parser.add_argument("--run-live", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.batch) or args.workers < 1:
        parser.error("Invalid batch name or workers")
    try:
        rows = pick(load_questions(), args.sample, args.seed, ids=args.ids)
    except ValueError as exc:
        parser.error(str(exc))
    # 这里主动丢弃 gold 与 answer_type，再传给 worker。
    questions = [{k: r[k] for k in ("id", "problem", "problem_category")} for r in rows]
    settings = runtime_config.current()
    node_overrides = {k: v for k, v in {"model": args.model, "provider": args.provider}.items() if v}
    if node_overrides:
        settings = settings.model_copy(update={"subagent": settings.subagent.model_copy(update=node_overrides)})
    config = research_config(settings, args.seconds)
    config_data = config.model_dump(mode="json")
    protocol = {"seed": args.seed, "excluded_ids": sorted(EXPOSED_IDS),
                "questions": questions, "config": config_data,
                "provenance": json.loads((HERE / "data/provenance.json").read_text()),
                "preflight_planning": "none; research agent receives original question"}
    if args.ids is not None:
        protocol["selection"] = {"method": "explicit_ids", "ids": args.ids}
    print(json.dumps({"ids": [r["id"] for r in questions], "model": settings.subagent.model,
                      "provider": settings.subagent.provider, "reasoning": settings.subagent.reasoning,
                      "effort": settings.subagent.effort, "seconds": args.seconds,
                      "workers": args.workers, "run_live": args.run_live}), flush=True)
    if not args.run_live:
        return
    batch = HERE / "runs" / args.batch
    batch.mkdir(parents=True, exist_ok=True)
    manifest = batch / "manifest.json"
    if manifest.exists():
        if json.loads(manifest.read_text())["protocol"] != protocol:
            raise SystemExit("Existing batch protocol differs; choose a new batch name")
    else:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip()
        diff = subprocess.check_output(["git", "diff", "--binary"], cwd=HERE)
        atomic_json(manifest, {"protocol": protocol, "started_at": datetime.now(timezone.utc).isoformat(),
                               "git_head": head, "tracked_diff_sha256": hashlib.sha256(diff).hexdigest()})
    todo = [q for q in questions if not (batch / str(q["id"]) / "result.json").exists()]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_worker, q, batch / str(q["id"]), config_data): q for q in todo}
        for future in as_completed(futures):
            record = future.result()
            print(json.dumps({k: record.get(k) for k in ("id", "status", "delivery_status", "stop_reason",
                              "wall_seconds", "searches", "cards", "error")}), flush=True)
    records = [json.loads((batch / str(q["id"]) / "result.json").read_text()) for q in questions]
    atomic_json(batch / "run_summary.json", {
        "sample_size": len(questions), "answers_produced": sum(bool(r["response"]) for r in records),
        "statuses": dict(Counter(r["status"] for r in records)),
        "ids": [q["id"] for q in questions], "research_seconds_sum": sum(r["wall_seconds"] for r in records)})


if __name__ == "__main__":
    main()
