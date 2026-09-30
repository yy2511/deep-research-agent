"""自主研究的单问题/JSONL 入口；默认 dry-run，--run-live 才联网付费。

模型与预算沿用 Web 运行配置；显式参数覆盖调用次数与时限。
"""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from dra import runtime_config, simple_research
from dra.paths import RUNS_DIR
from dra import research as research_mod


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("question", nargs="?")
    group.add_argument("--questions", type=Path, help="每行一个字符串或 {query: ...}")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument(
        "--max-lead-turns",
        type=int,
        default=None,
        help="缺省沿用 SimpleConfig 默认（与 Web 一致）；显式值原样使用",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="缺省沿用 SimpleConfig 默认；显式值原样使用",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--run-live", action="store_true")
    p.add_argument(
        "--mode",
        choices=("simple",),
        default="simple",
        help="自主研究：主 Agent 按需委派子任务，直接交付带引用答案",
    )
    return p


def questions(args):
    if args.question:
        yield args.question
        return
    with args.questions.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if index >= args.limit:
                break
            item = json.loads(line)
            query = item.get("query") if isinstance(item, dict) else item
            if not isinstance(query, str) or not query.strip():
                raise ValueError(f"第 {index + 1} 行必须是问题字符串或含 query 的对象")
            yield query


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if not 1 <= args.limit <= 100:
        p.error("limit 1–100")
    if args.max_lead_turns is not None and not 1 <= args.max_lead_turns <= 40:
        p.error("max-lead-turns 1–40")
    if args.timeout is not None and not 60 <= args.timeout <= 2400:
        p.error("timeout 60–2400 秒")
    for row in _run_simple(args, args.dry_run or not args.run_live):
        print(json.dumps(row, ensure_ascii=False), flush=True)


def simple_config(args) -> simple_research.SimpleConfig:
    """把脚本上限映射到最小闭环预算；模型/检索沿用同一份运行配置。

    未显式传上限时沿用运行时开关，与 Web 一致；显式值原样使用。
    """
    settings = runtime_config.current()
    kwargs: dict = {}
    if args.max_lead_turns is not None:
        # 显式值原样使用：SimpleConfig 支持极小预算的合法/诚实兜底，不放大用户显式限制。
        kwargs["max_llm_calls"] = args.max_lead_turns
        kwargs["max_tool_calls"] = args.max_lead_turns
    if args.timeout is not None:
        kwargs["wall_timeout_s"] = args.timeout
    return runtime_config.to_simple_config(settings, **kwargs)


def _run_simple(args, offline):
    """--mode simple：单 Lead 最小闭环。返回逐题的结果行。"""
    config = simple_config(args)
    for query in questions(args):
        started = time.monotonic()
        run_id = f"simple-{uuid4().hex}"
        directory = None if offline else RUNS_DIR / run_id
        row = {
            "query": query,
            "mode": "simple",
            "run_id": run_id,
            "artifact_dir": str(directory.resolve()) if directory else None,
        }
        if offline:
            row |= {
                "status": "dry_run",
                "stop_reason": "live_not_enabled",
                "usage": {},
                "wall_seconds": round(time.monotonic() - started, 3),
                "delivery_status": None,
                "research_coverage": {},
                "draft_chars": 0,
                "sources": 0,
                "cards": 0,
                "lead_turns": 0,
            }
            yield row
            continue
        try:
            result = research_mod.run_minimal_research(
                query,
                simple_research.requirements_from_query(query),
                config=config,
                run_id=run_id,
                checkpoint_dir=directory,
            )
            row |= {
                "status": result.delivery_status,
                "stop_reason": result.stop_reason,
                "usage": result.usage,
                "wall_seconds": result.wall_seconds,
                "delivery_status": result.delivery_status,
                "research_coverage": result.research_coverage,
                "draft_chars": len(result.draft),
                "sources": len(result.sources),
                "cards": len(result.cards),
                "lead_turns": sum(1 for c in result.calls if c.role == "lead"),
                "report_path": str((directory / "report.md").resolve()),
            }
        except Exception as exc:
            row |= {
                "status": "failed",
                "stop_reason": type(exc).__name__,
                "usage": {},
                "wall_seconds": round(time.monotonic() - started, 3),
                "delivery_status": None,
                "research_coverage": {},
                "draft_chars": 0,
                "sources": 0,
                "cards": 0,
                "lead_turns": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        yield row


if __name__ == "__main__":
    main()
