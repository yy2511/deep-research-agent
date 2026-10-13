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

from dra import events, llm, research, research_brief, runtime_config, simple_research, subagent, file_sources
from dra.durable import atomic_json
from dra.execution import ExecutionError
from dra.evidence_judge import JudgeConfig
from dra.browser import BrowserConfig
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


def skill_bundle():
    """与网页“自动”模式相同：提供全部已启用技能的目录，不预选，由研究 Agent 决定是否加载。"""
    from dra import skills
    from dra.paths import DATA_DIR
    return skills.SkillRepository(DATA_DIR).prepare([])


def research_config(settings, seconds):
    # 运行代码与网页一致（执行后端、联网方式取自网页有效配置）；Bash 的联网访问由 bash_leak_audit 事后检查。
    return runtime_config.to_simple_config(
        settings, wall_timeout_s=seconds, execution=runtime_config.execution_config(settings),
        evidence_judge=JudgeConfig(enabled=False),
        browser=BrowserConfig(enabled=False), compaction_enabled=False, skill_bundle=skill_bundle(),
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

    def wrap_download(self, original):
        """原件下载与网页阅读同样过滤基准答案页：下载前查网址，下载后查最终地址、文件名与文字。"""
        def download(url, **kwargs):
            if self.leaked(url, url):
                self._note_original(url)
                raise ExecutionError('benchmark_page_excluded', 'Benchmark answer page excluded from evaluation')
            data, text, metadata = original(url, **kwargs)
            final = metadata.get('final_url') or url
            if self.leaked(final, final, metadata.get('filename'), text):
                self._note_original(url)
                raise ExecutionError('benchmark_page_excluded', 'Benchmark answer page excluded from evaluation')
            return data, text, metadata
        return download

    def _note_original(self, url):
        with self.lock:
            self.blocked_originals.append(url)


PLANNING = "plan_without_clarification"
"""与网页一致先生成计划；跳过澄清问题判断，计划的“需要确认”标记不拦截，按未经用户确认的目标交给研究。"""
NO_PLANNING = "none; research agent receives original question"


PLAN_ATTEMPTS = 3
"""计划生成最多尝试次数：网页上规划失败时用户可以重试，评测无人操作，按同样方式重新生成。"""


def prepare_plan(problem, brief_config, *, plan_fn=None):
    """只调用网页规划的第二步（生成计划）。全部尝试失败才如实抛出，不退回无计划研究。

    返回 (交给研究入口的参数, 落盘记录)。计划模型只收到题目，没有搜索能力。
    """
    failures = []
    for _ in range(PLAN_ATTEMPTS):
        try:
            brief = (plan_fn or research_brief.build_research_brief)(
                problem, runtime_config.BriefConfig(**brief_config))
            break
        except Exception as exc:
            failures.append(f"{type(exc).__name__}: {exc}"[:2000])
    else:
        raise ValueError(f"计划生成 {PLAN_ATTEMPTS} 次均失败：" + " | ".join(failures))
    # 评测没有人确认计划：交给研究的说明必须写成“未经用户确认”。
    unconfirmed = brief.model_copy(update={"needs_confirmation": False})
    kwargs = {"requirements": brief.to_run_requirements(),
              "brief_context": research_brief.render_brief_context(unconfirmed),
              "initial_route": [item.model_dump(mode="json") for item in brief.route]}
    record = {"protocol": PLANNING, "planner": brief_config,
              "clarification": "skipped: gap assessment not called",
              "planner_needs_confirmation": brief.needs_confirmation, "planning_failures": failures,
              "brief": brief.model_dump(mode="json"), "brief_context": kwargs["brief_context"]}
    return kwargs, record


def bash_leak_audit(run_dir):
    """Bash 可直接联网，绕过搜索/阅读/下载的泄题过滤；研究结束后扫描命令与输出，命中只标记不改答案。"""
    hits = []
    run_dir = Path(run_dir)
    journal = run_dir / "tool_calls.jsonl"
    if journal.exists():
        # 只按 \n 分行：splitlines 还会在网页内容里的 \x1c、\u2028 等字符处断开，切出的碎片可能解析成数字。
        for line in journal.read_text(encoding="utf-8", errors="replace").split("\n"):
            try:
                call = json.loads(line)
            except ValueError:
                continue
            if isinstance(call, dict) and call.get("tool") == "bash":
                text = " ".join(str(call.get(k) or "") for k in ("arguments_raw", "arguments", "result"))
                if LEAKAGE_MARKER.search(text):
                    hits.append({"where": "bash_call", "tool_call_id": call.get("tool_call_id"), "phase": call.get("phase")})
    logs = run_dir / "execution" / "logs"
    for path in sorted(logs.glob("*")) if logs.is_dir() else []:
        if LEAKAGE_MARKER.search(path.read_text(encoding="utf-8", errors="replace")):
            hits.append({"where": "bash_output", "file": path.name})
    return hits


def _usage_recorder(path):
    """逐次记录模型调用用量（含缓存命中、首字延迟、重试）。每个 worker 进程同时只跑一题，故不按 run_id 过滤。

    完整请求消息与工具定义不写入：内容已在 run.json / tool_calls.jsonl，逐次重复会使每题多出数 MB。
    """
    path.unlink(missing_ok=True)
    lock = threading.Lock()

    def sink(evt):
        if evt.get("type") != "llm_usage":
            return
        row = {k: v for k, v in evt.items() if k not in ("request_messages", "request_tools")}
        with lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    return events.subscribe(sink)


def delivered_answer(result):
    return "" if result.draft_rejected else result.draft.strip()


def run_one(question, out, config, *, run_fn=None, brief_config=None, plan_fn=None):
    """question 只含 id/problem/category，标准答案与答案类型不进入研究进程。

    给出 ``brief_config`` 时先按网页方式生成计划（不问澄清问题）再研究；不给时沿用无计划协议。
    """
    run_fn = run_fn or research.run_minimal_research
    # 看图跟随传入配置（新批次沿用网页设置）；浏览器在评测中始终关闭，泄题过滤不覆盖浏览器访问。
    config = config.model_copy(update={'browser': BrowserConfig(enabled=False)})
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    run_id = f"dsqa-{question['id']}-{datetime.now():%Y%m%d-%H%M%S-%f}"
    guard = WebSearch(config)
    old_fetch = simple_research.fetch_url_content
    old_download = file_sources.fetch_original
    simple_research.fetch_url_content = guard.wrap_fetch(old_fetch)
    file_sources.fetch_original = guard.wrap_download(old_download)
    trace_token = llm.set_trace_full_io(True)
    usage_sink = _usage_recorder(out / "llm_usage.jsonl")
    started = time.monotonic()
    record = {**question, "run_id": run_id, "response": "", "status": "error",
              "prompt_profile": config.prompt_profile}
    record["preflight_planning"] = PLANNING if brief_config is not None else NO_PLANNING
    atomic_json(out / "progress.json", {**record, "status": "running"})
    try:
        plan_kwargs = {}
        if brief_config is not None:
            try:
                plan_kwargs, plan_record = prepare_plan(question["problem"], brief_config, plan_fn=plan_fn)
            except Exception as exc:
                record["planning_error"] = f"{type(exc).__name__}: {exc}"
                raise
            atomic_json(out / "plan.json", plan_record)
        result = run_fn(question["problem"], config=config, run_id=run_id,
                        checkpoint_dir=RUNS_DIR / run_id, search_fn=guard.search, **plan_kwargs)
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
        events.unsubscribe(usage_sink)
    record["bash_leak_hits"] = bash_leak_audit(RUNS_DIR / run_id)
    record.update(wall_seconds=round(time.monotonic() - started, 2), searches=guard.searches,
                  blocked_benchmark_pages=guard.blocked, blocked_original_downloads=guard.blocked_originals)
    atomic_json(out / "result.json", record)
    atomic_json(out / "progress.json", record)
    return record


def _worker(question, out, config_data, brief_config=None):
    return run_one(question, out, simple_research.SimpleConfig.model_validate(config_data), brief_config=brief_config)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261002)
    parser.add_argument("--ids", type=int, nargs="+", help="显式指定零起始题号，用新批次做定向复跑")
    parser.add_argument("--batch", default="pilot-20261002")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seconds", type=float, default=1200, help="每题研究总时限，默认与第 68 题复测相同的 1200 秒")
    parser.add_argument("--model", help="仅覆盖本批研究模型，不修改网页设置")
    parser.add_argument("--provider", help="仅覆盖本批连接，不修改网页设置")
    parser.add_argument("--planner-model", help="仅覆盖本批计划模型，不修改网页设置")
    parser.add_argument("--planner-provider", help="仅覆盖本批计划连接，不修改网页设置")
    parser.add_argument("--no-plan", action="store_true", help="不生成计划，研究只收到原题（旧口径对照）")
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
    # 计划模型跟随网页设置；只记录模型身份，不含凭据。
    planner_overrides = {k: v for k, v in {"planner_model": args.planner_model,
                                           "planner_provider": args.planner_provider}.items() if v}
    brief_config = None if args.no_plan else runtime_config.to_brief_config(
        settings, **planner_overrides).model_dump(mode="json")
    protocol = {"seed": args.seed, "excluded_ids": sorted(EXPOSED_IDS),
                "questions": questions, "config": config_data,
                "provenance": json.loads((HERE / "data/provenance.json").read_text()),
                "preflight_planning": NO_PLANNING if args.no_plan else PLANNING}
    if brief_config is not None:
        protocol["planner"] = brief_config
    if args.ids is not None:
        protocol["selection"] = {"method": "explicit_ids", "ids": args.ids}
    print(json.dumps({"ids": [r["id"] for r in questions], "model": settings.subagent.model,
                      "provider": settings.subagent.provider, "reasoning": settings.subagent.reasoning,
                      "effort": settings.subagent.effort, "seconds": args.seconds,
                      "planning": protocol["preflight_planning"], "planner": brief_config,
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
        futures = {pool.submit(_worker, q, batch / str(q["id"]), config_data, brief_config): q for q in todo}
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
