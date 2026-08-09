"""CLI 入口：`uv run python -m dra "你的研究问题"`。

当前跑 typed node orchestrator：scoping → 计划确认 → 分 Research Round research worker /
Decision Resolver + 确定性契约校验 → 单次并行 Final Research Pass → Report Plan /
Cross Worker Audit →
带引用报告。CLI 在 ``main`` 中显式选择实验模型档，不读取 Web 的
``runtime_config.json``。
"""

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from dra import timing, trace
from dra.models import ResearchPlan
from dra.nodes import (
    build_research_plan,
    render_report_markdown,
    revise_research_plan,
    scope_query,
)
from dra.orchestrator import OrchestratorConfig, run_orchestrator
from dra.subagent import SubAgentConfig

# 报告落盘目录（项目根 /reports）。__main__.py 在 src/dra/ 下，parents[2] = 项目根。
_REPORTS_DIR = Path(__file__).resolve().parents[2] / "reports"


def _slugify(text: str, maxlen: int = 30) -> str:
    """取 query 前若干字做文件名片段：保留中英文数字，其余转 -。"""
    s = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", text.strip())[:maxlen].strip("-")
    return s or "report"


def _save_report(query: str, md: str) -> Path:
    """把渲染好的 markdown 报告落盘到 reports/<时间戳>-<slug>.md，返回路径。"""
    _REPORTS_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _REPORTS_DIR / f"{ts}-{_slugify(query)}.md"
    path.write_text(md, encoding="utf-8")
    return path


def build_clarified_query(query: str, clarification: str) -> str:
    """把用户补充与原问题合成下游统一使用的明确 query。"""
    return (
        f"{query.strip()}\n"
        f"用户补充说明：{clarification.strip()}"
    )


def parse_cli_args(args: list[str]) -> tuple[list[str], bool, bool]:
    """解析 CLI 参数 → (query 词列表, stream, yes)。

    Worker 统一走 tool-calling loop，不提供执行模式选择。
    --yes：跳过研究前计划确认门（脚本化/无人值守跑）；orchestrator 内部自拆 plan，
    即 HITL 门加入前的旧行为。只跳计划门，不影响 scoping 澄清追问。
    """
    stream = "--stream" in args
    yes = "--yes" in args
    args = [a for a in args if a not in ("--stream", "--yes")]
    return args, stream, yes


def render_plan_text(research_plan: ResearchPlan) -> str:
    """研究计划 → 终端友好文本。"""
    lines = ["━" * 34, f"📋 研究计划（{len(research_plan.initial_tasks)} 个立即任务）"]
    for i, task in enumerate(research_plan.initial_tasks, 1):
        lines.append(f" [{i}] {task.objective}")
        lines.append(f"     🔍 {task.search_query}")
    lines.append("━" * 34)
    return "\n".join(lines)


def confirm_plan_loop(
    research_plan: ResearchPlan,
    *,
    revise_fn: Callable[[ResearchPlan, str], ResearchPlan],
    input_fn: Callable[[str], str] | None = None,
    print_fn: Callable[[str], None] | None = None,
) -> ResearchPlan | None:
    """计划确认环（HITL 门，决策记录 2026-07-06）：展示计划 → 等用户拍板。

    返回确认后的 research_plan（唯一开跑出口），取消返回 None。修订产物必回到展示
    重新确认——修订本身绝不触发执行。循环不设轮数熔断：人在环即限流，
    每轮只是一次人工触发的 planner 调用（对照：agent 自主循环必须三重熔断）。
    依赖全部注入（input/print/revise）——纯函数好测，载体换成 web 时逻辑同构。
    input/print 缺省晚绑定 builtins（不能写参数默认值：def 时绑死原对象，
    测试 monkeypatch builtins.input 就摸不到了）。
    """
    input_fn = input_fn or input
    print_fn = print_fn or print
    print_fn(render_plan_text(research_plan))
    while True:
        cmd = input_fn("[回车/y] 开始研究  [q] 取消  [其他文字] 作为修改意见修订计划\n> ").strip()
        if cmd == "" or cmd.lower() == "y":
            return research_plan
        if cmd.lower() == "q":
            return None
        print_fn(f"[修订] 按意见修订计划中：{cmd}")
        revised_plan = revise_fn(research_plan, cmd)
        if revised_plan.model_dump() == research_plan.model_dump():
            # revise_research_plan 失败兜底 = 原样返回 → 如实告知，不静默装作改过
            print_fn("[修订] 计划未改动（意见可能未生效），可换个说法再试。")
        research_plan = revised_plan
        print_fn(render_plan_text(research_plan))


def main() -> None:
    args, stream, yes = parse_cli_args(sys.argv[1:])
    if not args:
        print("Usage: uv run python -m dra [--stream] [--yes] '你的研究问题'")
        sys.exit(1)
    query = " ".join(args)

    # 1) Scoping：纯逻辑前置判定，模糊 query 先追问一次
    scope = scope_query(query)
    if scope.needs_clarification:
        print(f"[澄清] {scope.reason}")
        answer = input(f"{scope.clarification_question}\n> ").strip()
        if not answer:
            print("[取消] 未收到补充说明，本次不启动研究。")
            return
        query = build_clarified_query(query, answer)
        print(f"[澄清完成] 将按以下范围研究：{query}")

    config = OrchestratorConfig(
        planner_model="gpt-5.5", planner_provider="codex521", planner_effort="high",
        writer_model="gpt-5.5", writer_provider="codex521",
        writer_reasoning=True, writer_effort="high",
        subagent=SubAgentConfig(
            model="gpt-5.5", provider="codex521",
            reasoning=True, effort="high",
        ),
    )

    # 2) 计划确认门（HITL，决策记录 2026-07-06）：计划在 CLI 层生成并经用户
    #    确认/修订后注入 orchestrator；--yes 跳过（research_plan=None = 内部自拆）。
    research_plan: ResearchPlan | None = None
    if not yes:
        print("[计划] 拆解研究问题 → 研究计划 ...")
        research_plan = confirm_plan_loop(
            build_research_plan(
                query, max_initial_tasks=config.max_initial_tasks,
                model=config.planner_model, provider=config.planner_provider,
                effort=config.planner_effort,
            ),
            revise_fn=lambda plan, feedback: revise_research_plan(
                plan, feedback, max_initial_tasks=config.max_initial_tasks,
                model=config.planner_model, provider=config.planner_provider,
                effort=config.planner_effort,
            ),
        )
        if research_plan is None:
            print("[取消] 已取消本次研究。")
            return

    # 3) 跑 orchestrator-worker 闭环（async 桥接到同步 main）
    state = asyncio.run(run_orchestrator(query, config, research_plan=research_plan))

    # 4) 输出报告 + 落盘
    print("\n" + "=" * 60)
    if state.report is not None:
        md = render_report_markdown(state.report, state.evidence)
        print(md)
        print("=" * 60)
        path = _save_report(query, md)
        print(f"\n📄 报告已落盘：{path}")
    else:
        print("(无报告)")
        print("=" * 60)

    # 4.5) 落盘自包含轨迹查看器 HTML（可观测 / demo；非侵入，失败不影响报告）
    tr_path = trace.save_trace(
        state, _REPORTS_DIR, slug=_slugify(query), timing_summary=timing.summary()
    )
    if tr_path:
        print(f"🔎 轨迹查看器（浏览器打开）：{tr_path}")

    # 5) 统计：所有 worker 都来自确认的 plan-node DAG / Final Research Pass。
    initial_done = len(state.sub_reports)
    planned_task_total = (
        len(state.research_plan.initial_tasks) if state.research_plan is not None else 0
    )
    sub_failed = max(0, planned_task_total - initial_done)
    print(
        f"\n[统计] 子代理 {initial_done}/{planned_task_total} 完成"
        + (f"（{sub_failed} 个失败）" if sub_failed else "")
        + f" | 工具动作 {sum(r.tool_calls for r in state.sub_reports)}"
        f" | 证据 {len(state.evidence)} 张 | 状态 {state.status}"
    )
    if state.sub_reports:
        print("[子代理明细]")
        for r in state.sub_reports:
            print(
                f"  - {r.objective}: {r.tool_calls} 次工具动作 / {len(r.evidence)} 张证据"
                + (f" / {len(r.conflicts)} 个矛盾" if r.conflicts else "")
            )


if __name__ == "__main__":
    main()
