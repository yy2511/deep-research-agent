"""用 DeepResearch Bench II 的题目跑研究，只产出报告，不打分。

    uv run python eval/drb2/run_pilot.py --idx 15 43            # 只打印将要做什么
    uv run python eval/drb2/run_pilot.py --idx 15 43 --run-live # 真跑：调用模型和付费搜索

给研究 Agent 的问题是题目的 ``content.task``（用户可见的问题本身）。数据集的 ``prompt``
字段另外在末尾用英文要求“不许看某篇文章”，这里不靠提示词，改由代码强制：

- 题目列出的原文网址进网站黑名单（产品功能，检索和按网址读取都会拦）；
- 评测额外按原文标题拦截转载页：检索结果的标题或正文开头出现原文标题就剔除，
  按网址读回的页面同样检查。这一层只在评测里有，产品里没有。

研究配置与 DeepSearchQA 网页对齐批次相同（``eval/deepsearchqa/run_pilot.py``）：先按网页方式生成计划
（跳过澄清，计划标为未经用户确认）、提供全部已启用技能、看图跟随网页设置、运行代码与原件下载开启，
浏览器、证据核验（Jev）和长对话整理关闭；``--seconds`` 设总时限，``--model/--provider`` 与
``--planner-model/--planner-provider`` 只覆盖本批，不改网页设置。

每题输出到 ``eval/drb2/runs/<批次>/idx-<n>/``：``report.md``（正文 + 引用来源，供评分用）、
``full.md``（产品导出的完整报告）、``meta.json``（耗时、用量、拦截次数等）、``plan.json``（计划）、
``llm_usage.jsonl``（逐次模型调用用量与缓存命中）。完整运行轨迹在数据目录的 runs/ 下，run_id 记在 meta.json 里。
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import importlib.util
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from dra import llm, research, runtime_config, simple_research, skills, subagent, file_sources
from dra.execution import ExecutionError
from dra.evidence_judge import JudgeConfig
from dra.browser import BrowserConfig
from dra.paths import DATA_DIR, RUNS_DIR
from dra.site_filter import normalize_blocked_sites, provider_exclude_domains
from dra.tools import UrlFetchError

HERE = Path(__file__).resolve().parent
TASKS = HERE / "data" / "tasks.jsonl"


def _dsqa_runner():
    """复用 DeepSearchQA 网页对齐批次的计划生成与用量记录，两套评测的研究入口保持同一口径。"""
    path = HERE.parent / "deepsearchqa" / "run_pilot.py"
    spec = importlib.util.spec_from_file_location("dsqa_run_pilot", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DSQA = _dsqa_runner()


def load_tasks(indexes: list[int]) -> list[dict]:
    rows = {row["idx"]: row for row in map(json.loads, TASKS.read_text(encoding="utf-8").splitlines())}
    missing = [i for i in indexes if i not in rows]
    if missing:
        raise SystemExit(f"题目不存在：idx {missing}")
    return [rows[i] for i in indexes]


def blocked_sites_for(task: dict) -> tuple[list[str], list[str]]:
    """把题目给的原文网址转成黑名单规则；写法不合规的单独列出，不静默丢。"""
    rules, skipped = [], []
    for url in task["content"]["blocked"].get("urls", []):
        try:
            rules.extend(normalize_blocked_sites([url]))
        except ValueError:
            skipped.append(url)
    return normalize_blocked_sites(rules), skipped


def _squash(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


class TitleGuard:
    """评测专用：按原文标题拦截转载页，并记下拦了哪些。"""

    def __init__(self, title: str):
        self.key = _squash(title)
        self.hits: list[dict] = []

    def matches(self, *texts: str | None) -> bool:
        return bool(self.key) and any(self.key in _squash((t or "")[:3000]) for t in texts)

    def search_fn(self, config: simple_research.SimpleConfig):
        exclude = provider_exclude_domains(config.blocked_sites)

        def search(query: str):
            docs = subagent._retrieve(query, config.adapter, exclude_domains=exclude or None)
            kept = []
            for doc in docs:
                if self.matches(doc.title, doc.raw_content):
                    self.hits.append({"where": "search", "query": query, "url": doc.source_url, "title": doc.title})
                else:
                    kept.append(doc)
            return kept
        return search

    def wrap_fetch(self, fetch):
        def guarded(url, **kw):
            fetched = fetch(url, **kw)
            if self.matches(fetched.get("title"), fetched.get("content")):
                self.hits.append({"where": "fetch", "url": url, "title": fetched.get("title")})
                raise UrlFetchError("该页面是本题禁止使用的原始文章，正文未采用")
            return fetched
        return guarded

    def wrap_download(self, original):
        """原件下载同样按原文标题拦截：命中时不登记来源。"""
        def download(url, **kwargs):
            data, text, metadata = original(url, **kwargs)
            if self.matches(metadata.get('filename'), text):
                self.hits.append({'where': 'original_download', 'url': url, 'title': metadata.get('filename')})
                raise ExecutionError('benchmark_page_excluded', '该文件是本题禁止使用的原始文章，未采用')
            return data, text, metadata
        return download


def report_for_judge(result: simple_research.SimpleResult) -> str:
    """交给评分的报告：正文加引用来源列表，不含运行状态等内部信息。"""
    lines = [result.draft.strip() or "（没有可交付正文）", "", "## 参考来源", ""]
    for card in result.cards:
        quote = card.evidence_quotes()[0]
        lines.append(f"- [{card.card_id}] {quote.source_title or quote.source_url} {quote.source_url or ''}".rstrip())
    return "\n".join(lines) + "\n"


def research_config(settings, sites, seconds=None):
    """与 DeepSearchQA ``research_config`` 相同，只把黑名单换成本题原文网址。"""
    config = DSQA.research_config(settings, seconds or settings.wall_timeout_s)
    return config.model_copy(update={"blocked_sites": sites})


def bash_leak_audit(run_dir, guard, sites):
    """Bash 可直接联网，绕过检索/阅读/下载的原文拦截；研究结束后扫描命令与输出，命中只标记不改报告。"""
    hits = []
    markers = [s.lower() for s in sites]
    run_dir = Path(run_dir)

    def leaked(text):
        low = text.lower()
        return guard.matches(text) or any(m in low for m in markers)
    journal = run_dir / "tool_calls.jsonl"
    if journal.exists():
        for line in journal.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                call = json.loads(line)
            except ValueError:
                continue
            if call.get("tool") == "bash" and leaked(" ".join(str(call.get(k) or "") for k in ("arguments_raw", "arguments", "result"))):
                hits.append({"where": "bash_call", "tool_call_id": call.get("tool_call_id")})
    logs = run_dir / "execution" / "logs"
    for path in sorted(logs.glob("*")) if logs.is_dir() else []:
        if leaked(path.read_text(encoding="utf-8", errors="replace")):
            hits.append({"where": "bash_output", "file": path.name})
    return hits


def run_one(task: dict, out: Path, *, settings=None, seconds=None, brief_config=None) -> dict:
    """不给 ``brief_config`` 时不生成计划（旧口径，测试用）；命令行默认按网页方式先生成计划。"""
    settings = settings or runtime_config.current()
    sites, skipped = blocked_sites_for(task)
    config = research_config(settings, sites, seconds)
    config = config.model_copy(update={"browser": BrowserConfig(enabled=False)})
    guard = TitleGuard(task["content"]["blocked"].get("title", ""))
    run_id = f"drb2-idx{task['idx']}-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    original_fetch = simple_research.fetch_url_content
    original_download = file_sources.fetch_original
    simple_research.fetch_url_content = guard.wrap_fetch(original_fetch)
    file_sources.fetch_original = guard.wrap_download(original_download)
    trace_token = llm.set_trace_full_io(True)
    usage_sink = DSQA._usage_recorder(out / "llm_usage.jsonl")
    started = time.monotonic()
    plan_kwargs = {}
    try:
        if brief_config is not None:
            plan_kwargs, plan_record = DSQA.prepare_plan(task["content"]["task"], brief_config)
            (out / "plan.json").write_text(json.dumps(plan_record, ensure_ascii=False, indent=1), encoding="utf-8")
        result = research.run_minimal_research(
            task["content"]["task"], config=config, run_id=run_id,
            checkpoint_dir=RUNS_DIR / run_id, search_fn=guard.search_fn(config), **plan_kwargs)
    finally:
        simple_research.fetch_url_content = original_fetch
        file_sources.fetch_original = original_download
        llm.reset_trace_full_io(trace_token)
        DSQA.events.unsubscribe(usage_sink)
    (out / "report.md").write_text(report_for_judge(result), encoding="utf-8")
    (out / "full.md").write_text(simple_research.render_markdown(result), encoding="utf-8")
    meta = {
        "idx": task["idx"], "id": task.get("id"), "theme": task.get("theme"), "run_id": run_id,
        "model": f"{config.adapter.model}@{config.adapter.provider}", "effort": config.adapter.effort,
        "planner": brief_config, "preflight_planning": DSQA.PLANNING if brief_config is not None else DSQA.NO_PLANNING,
        "prompt_profile": config.prompt_profile,
        "wall_timeout_s": config.wall_timeout_s,
        "wall_seconds": round(time.monotonic() - started, 1),
        "stop_reason": result.stop_reason, "delivery_status": result.delivery_status,
        "draft_chars": len(result.draft), "cards": len(result.cards), "sources": len(result.sources),
        "usage": result.usage, "warnings": result.warnings,
        "blocked_sites": sites, "blocked_urls_not_ruleable": skipped, "title_guard_hits": guard.hits,
        "bash_leak_hits": bash_leak_audit(RUNS_DIR / run_id, guard, sites),
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def _worker(task, out, seconds, brief_config, overrides):
    settings = runtime_config.current()
    if overrides:
        settings = settings.model_copy(update={"subagent": settings.subagent.model_copy(update=overrides)})
    return run_one(task, out, settings=settings, seconds=seconds, brief_config=brief_config)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--idx", type=int, nargs="+", required=True, help="题目 idx，可多个")
    p.add_argument("--batch", default=datetime.now().strftime("%Y%m%d-%H%M"), help="输出批次目录名")
    p.add_argument("--seconds", type=float, help="每题研究总时限（秒），默认跟随网页设置")
    p.add_argument("--workers", type=int, default=1, help="并发题数")
    p.add_argument("--model", help="仅覆盖本批研究模型，不修改网页设置")
    p.add_argument("--provider", help="仅覆盖本批连接，不修改网页设置")
    p.add_argument("--planner-model", help="仅覆盖本批计划模型，不修改网页设置")
    p.add_argument("--planner-provider", help="仅覆盖本批计划连接，不修改网页设置")
    p.add_argument("--no-plan", action="store_true", help="不生成计划，研究只收到原题（旧口径对照）")
    p.add_argument("--run-live", action="store_true", help="真跑（调用模型和付费搜索）")
    args = p.parse_args(argv)
    tasks = load_tasks(args.idx)
    settings = runtime_config.current()
    overrides = {k: v for k, v in {"model": args.model, "provider": args.provider}.items() if v}
    if overrides:
        settings = settings.model_copy(update={"subagent": settings.subagent.model_copy(update=overrides)})
    planner = {k: v for k, v in {"planner_model": args.planner_model, "planner_provider": args.planner_provider}.items() if v}
    brief_config = None if args.no_plan else runtime_config.to_brief_config(settings, **planner).model_dump(mode="json")
    for task in tasks:
        sites, skipped = blocked_sites_for(task)
        print(f"idx {task['idx']} {task['id']} [{task['theme']}] 黑名单 {len(sites)} 条"
              + (f"，无法转成规则 {len(skipped)} 条" if skipped else ""), file=sys.stderr)
    config = research_config(settings, [], args.seconds)
    print(json.dumps({"model": config.adapter.model, "provider": config.adapter.provider, "effort": config.adapter.effort,
                      "prompt_profile": config.prompt_profile, "seconds": config.wall_timeout_s,
                      "planner": brief_config, "execution": config.execution.enabled, "vision": config.vision.enabled,
                      "skills": len(config.skill_bundle["skills"]), "workers": args.workers,
                      "run_live": args.run_live}, ensure_ascii=False), flush=True)
    if not args.run_live:
        return
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_worker, task, HERE / "runs" / args.batch / f"idx-{task['idx']}",
                               args.seconds, brief_config, overrides) for task in tasks]
        for future in as_completed(futures):
            meta = future.result()
            print(json.dumps({k: meta[k] for k in ("idx", "run_id", "wall_seconds", "stop_reason", "delivery_status",
                                                   "draft_chars", "cards", "title_guard_hits", "bash_leak_hits")},
                             ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
