"""用 DeepResearch Bench II 的题目跑研究，只产出报告，不打分。

    uv run python eval/drb2/run_pilot.py --idx 15 43            # 只打印将要做什么
    uv run python eval/drb2/run_pilot.py --idx 15 43 --run-live # 真跑：调用模型和付费搜索

给研究 Agent 的问题是题目的 ``content.task``（用户可见的问题本身）。数据集的 ``prompt``
字段另外在末尾用英文要求“不许看某篇文章”，这里不靠提示词，改由代码强制：

- 题目列出的原文网址进网站黑名单（产品功能，检索和按网址读取都会拦）；
- 评测额外按原文标题拦截转载页：检索结果的标题或正文开头出现原文标题就剔除，
  按网址读回的页面同样检查。这一层只在评测里有，产品里没有。

每题输出到 ``eval/drb2/runs/<批次>/idx-<n>/``：``report.md``（正文 + 引用来源，供评分用）、
``full.md``（产品导出的完整报告）、``meta.json``（耗时、用量、拦截次数等）。
完整运行轨迹在数据目录的 runs/ 下，run_id 记在 meta.json 里。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from dra import research, runtime_config, simple_research, subagent, file_sources
from dra.execution import ExecutionConfig, ExecutionError
from dra.evidence_judge import JudgeConfig
from dra.vision import VisionConfig
from dra.paths import RUNS_DIR
from dra.site_filter import normalize_blocked_sites, provider_exclude_domains
from dra.tools import UrlFetchError

HERE = Path(__file__).resolve().parent
TASKS = HERE / "data" / "tasks.jsonl"


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

    def block_original(self, url, **kwargs):
        self.hits.append({'where': 'original_download', 'url': url, 'reason': 'not_enabled_in_evaluation'})
        raise ExecutionError('benchmark_download_disabled', '本评测仅使用受原文标题过滤保护的文字阅读；原件下载未启用。')


def report_for_judge(result: simple_research.SimpleResult) -> str:
    """交给评分的报告：正文加引用来源列表，不含运行状态等内部信息。"""
    lines = [result.draft.strip() or "（没有可交付正文）", "", "## 参考来源", ""]
    for card in result.cards:
        quote = card.evidence_quotes()[0]
        lines.append(f"- [{card.card_id}] {quote.source_title or quote.source_url} {quote.source_url or ''}".rstrip())
    return "\n".join(lines) + "\n"


def run_one(task: dict, out: Path) -> dict:
    sites, skipped = blocked_sites_for(task)
    config = runtime_config.to_simple_config(runtime_config.current(), blocked_sites=sites,
        execution=ExecutionConfig(enabled=False), vision=VisionConfig(enabled=False), evidence_judge=JudgeConfig(enabled=False))
    guard = TitleGuard(task["content"]["blocked"].get("title", ""))
    run_id = f"drb2-idx{task['idx']}-{datetime.now():%Y%m%d-%H%M%S}"
    original_fetch = simple_research.fetch_url_content
    original_download = file_sources.fetch_original
    simple_research.fetch_url_content = guard.wrap_fetch(original_fetch)
    file_sources.fetch_original = guard.block_original
    started = time.monotonic()
    try:
        result = research.run_minimal_research(
            task["content"]["task"], config=config, run_id=run_id,
            checkpoint_dir=RUNS_DIR / run_id, search_fn=guard.search_fn(config))
    finally:
        simple_research.fetch_url_content = original_fetch
        file_sources.fetch_original = original_download
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report_for_judge(result), encoding="utf-8")
    (out / "full.md").write_text(simple_research.render_markdown(result), encoding="utf-8")
    meta = {
        "idx": task["idx"], "id": task["id"], "theme": task["theme"], "run_id": run_id,
        "model": f"{config.adapter.model}@{config.adapter.provider}",
        "wall_timeout_s": config.wall_timeout_s,
        "wall_seconds": round(time.monotonic() - started, 1),
        "stop_reason": result.stop_reason, "delivery_status": result.delivery_status,
        "draft_chars": len(result.draft), "cards": len(result.cards), "sources": len(result.sources),
        "usage": result.usage, "warnings": result.warnings,
        "blocked_sites": sites, "blocked_urls_not_ruleable": skipped, "title_guard_hits": guard.hits,
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--idx", type=int, nargs="+", required=True, help="题目 idx，可多个")
    p.add_argument("--batch", default=datetime.now().strftime("%Y%m%d-%H%M"), help="输出批次目录名")
    p.add_argument("--run-live", action="store_true", help="真跑（调用模型和付费搜索）")
    args = p.parse_args(argv)
    tasks = load_tasks(args.idx)
    settings = runtime_config.current()
    for task in tasks:
        sites, skipped = blocked_sites_for(task)
        print(f"idx {task['idx']} {task['id']} [{task['theme']}] 黑名单 {len(sites)} 条"
              + (f"，无法转成规则 {len(skipped)} 条" if skipped else ""), file=sys.stderr)
    if not args.run_live:
        model = runtime_config.to_simple_config(settings).adapter
        print(f"未加 --run-live，不运行。将使用模型 {model.model}@{model.provider}", file=sys.stderr)
        return
    for task in tasks:
        meta = run_one(task, HERE / "runs" / args.batch / f"idx-{task['idx']}")
        print(json.dumps({k: meta[k] for k in ("idx", "run_id", "wall_seconds", "stop_reason",
                                               "delivery_status", "draft_chars", "cards", "title_guard_hits")},
                         ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
