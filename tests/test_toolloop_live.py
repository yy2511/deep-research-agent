"""tool-loop live 冒烟：真模型 + 真检索跑一个研究任务。默认跳过，--run-live 才跑。

看什么（记 DEVLOG）：工具调用序列是否合理（search→fetch→save→finish）、
save_evidence 拒收率、是否在预算内自主 finish、墙钟、token。

中转故障日逃生舱：DRA_LIVE_MODEL / DRA_LIVE_PROVIDER 环境变量可临时换档
（如 opencode 503 时切 zetatechs），默认仍是主力档 flash——canonical 语义不变。
"""
import os

import pytest

import dra.llm as llm
from dra.models import ResearchTask
from dra.subagent import SubAgentConfig
from dra.toolloop import run_tool_loop

pytestmark = pytest.mark.live


def _live_config() -> SubAgentConfig:
    overrides = {}
    if os.getenv("DRA_LIVE_MODEL"):
        overrides["model"] = os.environ["DRA_LIVE_MODEL"]
        overrides["provider"] = os.getenv("DRA_LIVE_PROVIDER", "opencode")
    return SubAgentConfig(**overrides)


def test_tool_loop_live_single_research_task():
    task = ResearchTask(
        node_id="ev-sales",
        objective="2025 年全球电动汽车销量的主要数字与同比趋势",
        search_query="2025 global EV sales figures YoY growth")
    llm.reset_token_usage()
    report = run_tool_loop(task, _live_config(), verbose=True)
    print(f"\nstatus={report.status} tool_calls={report.tool_calls} "
          f"evidence={len(report.evidence)} tokens={llm.get_token_usage()}")
    print(f"summary={report.summary}")
    for c in report.evidence[:5]:
        print(f"- {c.claim[:80]} | {c.source_url}")
    assert report.status in ("ok", "timeout")
    assert len(report.evidence) >= 1          # 至少存下一张过门的证据
