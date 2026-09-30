"""Web 与脚本共用的自主研究入口。"""

from __future__ import annotations

from pathlib import Path
from dra import simple_research


def run_minimal_research(
    query: str,
    requirements: list[simple_research.Requirement] | None = None,
    *,
    config: simple_research.SimpleConfig | None = None,
    brief_context: str | None = None,
    initial_route: list[dict] | None = None,
    run_id: str = "simple-research",
    checkpoint_dir: str | Path | None = None,
    resume: bool = False,
    model_call=None,
    condense_fn=None,
    search_fn=None,
    ref_resolver=None,
    user_input=None,
) -> simple_research.SimpleResult:
    """确认后的问题 → 自主取证与委派 → 带引用的交付。"""
    if not requirements:
        requirements = simple_research.requirements_from_query(query)
    return simple_research.run_simple_research(
        query,
        requirements,
        config=config,
        brief_context=brief_context,
        initial_route=initial_route,
        run_id=run_id,
        run_dir=Path(checkpoint_dir) if checkpoint_dir is not None else None,
        resume=resume,
        model_call=model_call,
        condense_fn=condense_fn,
        search_fn=search_fn,
        ref_resolver=ref_resolver,
        session_id=run_id,
        user_input=user_input,
    )
