"""单个 research worker 的执行与预算配置。

每个 worker 对应 ReadySet / Final Research Pass 编译出的一个 ResearchTask，统一走可调用
search/fetch/save 的 tool loop。worker 不写最终报告，只把证据、摘要与运行状态
打包成 SubAgentReport 回传给 Orchestrator。

设计要点：
- 上下文隔离：子代理之间不共享 evidence；全部回传到 orchestrator
  汇总时再统一处理。
- 不调 write_report：报告由 orchestrator 在 Report Plan 与全局质量审计之后综合写一次。
- async 包装：内部同步调用工具与 LLM，外层用 `asyncio.to_thread` 并发，
  不强行把所有底层改 async。
- 子代理预算：工具调用、证据总量和墙钟均由代码硬限制。
- V2-1 检索源路由：config.sources 决定走 web / local / 融合，默认 ["web"] 不破坏
  现有基线；local 走 local_rag_search（经典 RAG 本地语料）。
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, ConfigDict, model_validator

from dra.models import (
    RetrievedDoc,
    SubAgentReport,
    ResearchTask,
    normalize_url,
)
from dra.tools import ddg_search, exa_search, local_rag_search, web_search


class SubAgentConfig(BaseModel):
    """子代理预算配置。"""

    search_top_k: int = 6
    model_config = ConfigDict(extra="forbid")
    # Python API 的代码默认档。CLI 会显式覆盖，Web 读 runtime_config.json；
    # 因此这里不是所有入口共享的生产默认。模型实验应由调用方注入配置。
    model: str = "minimax-m3"
    provider: str = "opencode"
    # 工蜂关推理：工具决策与摘要通常是机械活，关掉思维链 = 更快更省 + 防推理吃光 max_tokens
    # 把可见 content 挤截断（opencode 模型默认开推理，实测 thinking:disabled 可关）。
    reasoning: bool = False
    # 非 None 时透传 reasoning_effort，并要求 reasoning=True；provider 可能严格校验取值。
    effort: str | None = None
    # 摘要独立推理开关（与 tool-loop 决策的 reasoning/effort 解耦）：
    # 摘要是机械 JSON 压缩，即使子代理档开推理也默认关，防思维链挤空可见 content。
    # summarize_effort 非 None 时必须 summarize_reasoning=True（同 effort/reasoning co-knob）。
    summarize_reasoning: bool = False
    summarize_effort: str | None = None
    # 双源 Web 检索边界见 docs/RETRIEVAL_SOURCES_DECISION.md：
    # "web" 默认 = Tavily + Exa 并发(各取 search_top_k_per_source 篇,合并去重)。
    # 含 "local" → 加 local_rag_search(本地 RAG 语料)。
    # 显式指定 ["web"] / ["local"] / ["web","local"] 都生效。
    sources: list[str] = ["web"]
    # 双源并行每源各取的 top_k。
    search_top_k_per_source: int = 4
    # 墙钟熔断（协作式 deadline）：单个子代理总时长预算，超时带着已抽证据正常返回
    # （status="timeout"），不丢半途成果。None=不限。
    wall_timeout_s: float | None = 900.0
    # 三级检索 fallback：Tavily+Exa 双源全挂时，先试 DDG（免 key）保底再抛异常。
    # 默认开——纯安全网，只在双源都已失败的分支里生效，不改变任何当前成功路径的行为。
    enable_search_fallback: bool = True
    # 工具调用总预算。
    max_tool_calls: int = 12
    # 连续无效调用（未知工具/坏参数/校验全拒/纯文本回复）熔断阈值，防死循环空烧。
    max_invalid_calls: int = 3
    # 最多保留的证据卡数，防止 Writer 上下文被低价值材料挤满。
    max_cards_total: int = 45
    # 降落保留区：额度只剩这么多次时
    # search/fetch_page 拒收（不耗预算、计 strike），只放行 save_evidence/finish——
    # 防止只读不存。0=关闭；harness 级门保持 provider 无关。
    save_reserve_calls: int = 2

    def effective_max_cards_total(self) -> int:
        return self.max_cards_total

    @model_validator(mode="after")
    def _co_knob_invariants(self) -> "SubAgentConfig":
        """耦合旋钮联动校验（lunon 式构造期 fail-loud）。实测锚点见各行注释；
        确要突破限值就改这里的断言并附新实测依据——这正是本校验的意义。"""
        # effort=开思考并定档，与 reasoning=False（关思考）语义冲突——fail-loud 防
        # 「设了 effort 却被 thinking:disabled 盖掉/组合行为不明」的静默错档。
        if self.effort is not None and not self.reasoning:
            raise ValueError(
                f"effort={self.effort!r} 需要 reasoning=True（effort=开思考并定强度，"
                f"与关思考互斥；请同时设 reasoning=True，co-knob）")
        # summarize 档同构 co-knob：summarize_effort 与 summarize_reasoning 互斥组合 fail-loud。
        if self.summarize_effort is not None and not self.summarize_reasoning:
            raise ValueError(
                f"summarize_effort={self.summarize_effort!r} 需要 summarize_reasoning=True"
                f"（effort=开思考并定强度，与关思考互斥；请同时设 summarize_reasoning=True，co-knob）")
        # 降落保留区必须小于总预算，否则模型从第 1 手就只能存/收工（退化为无检索）
        if not (0 <= self.save_reserve_calls < self.max_tool_calls):
            raise ValueError(
                f"save_reserve_calls={self.save_reserve_calls} 必须在 [0, "
                f"max_tool_calls={self.max_tool_calls}) 内（co-knob）")
        if self.max_cards_total < 1:
            raise ValueError("max_cards_total 必须 >= 1")
        return self


def _retrieve(
    query: str, config: SubAgentConfig, *, verbose: bool = False
) -> list[RetrievedDoc]:
    """按 config.sources 做检索分发:web(Tavily+Exa 双源并发) / local / 融合。

    **C 方案双源并发**(2026-06-30):"web" 默认 = Tavily 和 Exa **ThreadPoolExecutor 并发**,
    各源独立 try/except 互不阻塞——单源故障仍能用另一源继续。两家实测 URL 重叠率仅 4%,
    合并 ≈ 覆盖度翻倍(详见 docs/RETRIEVAL_SOURCES_DECISION.md 与
    scratchpad/compare_tavily_exa_result.json)。

    错误语义:
    - 部分源成功部分源失败 → 用成功的,记 verbose print
    - 所有源都失败 → 抛 RuntimeError(对齐 web_search 单源契约,避免子代理空跑烧 LLM 钱)
    - 所有源返空列表(没失败也没结果) → 返 [],不抛(Tavily/Exa 偶发 0 results 是正常)

    融合去重只用精确层(source_url + snippet 前 80 字符):web 与 local 天然不同来源,
    语义去重会误杀同主题不同来源(P2-2 已知缺陷,不传染到融合)。
    """
    sources = config.sources or ["web"]
    per_k = config.search_top_k_per_source

    # 派发表:(source_name, 检索函数, top_k)
    tasks: list[tuple[str, callable, int]] = []
    if "web" in sources:
        tasks.append(("tavily", web_search, per_k))
        tasks.append(("exa", exa_search, per_k))
    if "local" in sources:
        tasks.append(("local", local_rag_search, per_k))

    if not tasks:
        return []

    def _run_one(name: str, fn, top_k: int):
        """单源执行:成功返 (name, docs),失败返 (name, Exception)。"""
        t0 = time.monotonic()
        try:
            result = fn(query, top_k=top_k)
            dt = time.monotonic() - t0
            return name, result, dt, None
        except Exception as e:  # 各源独立隔离,不阻塞他源
            dt = time.monotonic() - t0
            return name, None, dt, e

    docs: list[RetrievedDoc] = []
    errors: dict[str, Exception] = {}

    # 单源直接调,多源 ThreadPoolExecutor 并发(墙钟取 max,不是 sum)
    if len(tasks) == 1:
        name, fn, top_k = tasks[0]
        n, r, dt, e = _run_one(name, fn, top_k)
        if e is not None:
            errors[n] = e
        else:
            docs.extend(r)
        if verbose:
            status = f"❌ {type(e).__name__}" if e else f"→ {len(r)} 篇"
            print(f"  [检索] {n} {status} ({dt:.1f}s)")
    else:
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = [pool.submit(_run_one, n, fn, k) for n, fn, k in tasks]
            for f in futures:
                n, r, dt, e = f.result()
                if e is not None:
                    errors[n] = e
                else:
                    docs.extend(r)
                if verbose:
                    status = f"❌ {type(e).__name__}: {str(e)[:60]}" if e else f"→ {len(r)} 篇"
                    print(f"  [检索] {n} {status} ({dt:.1f}s)")

    # 全挂兜底:双源都抛异常 → 先试 DDG 保底（免 key 第三级）,仍无 → 抛 RuntimeError
    if not docs and errors and len(errors) == len(tasks):
        if config.enable_search_fallback and "web" in sources:
            try:
                docs = ddg_search(query, top_k=config.search_top_k)
                if verbose:
                    print(f"  [检索] ⚠️ 双源全挂 → DDG 兜底 → {len(docs)} 篇（snippet-only 降档）")
            except Exception as e:
                errors["ddg"] = e
        if not docs:
            raise RuntimeError(
                "all sources failed: " +
                ", ".join(f"{k}={type(v).__name__}: {str(v)[:120]}" for k, v in errors.items())
            )

    # 融合精确去重(零改原有逻辑):同一 (source_url 归一化, snippet 前 80 字符) 视为同一文档。
    # URL 侧走 normalize_url（协议/host大小写/www./尾斜杠/追踪参数/fragment 归一化），
    # 拦住同页不同写法（如 tavily 带 utm、exa 不带）被误判成两篇不同文档；
    # 展示/存储的 d.source_url 本身不变，只影响这里的比较 key。
    seen: set[tuple[str, str]] = set()
    merged: list[RetrievedDoc] = []
    for d in docs:
        key = (normalize_url(d.source_url), (d.snippet or "")[:80])
        if key in seen:
            continue
        seen.add(key)
        merged.append(d)
    return merged


def _expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() > deadline


async def run_subagent(
    task: ResearchTask,
    config: SubAgentConfig | None = None,
    *,
    verbose: bool = False,
    deadline: float | None = None,
    mission_context: str | None = None,
) -> SubAgentReport:
    """async 包装：底层同步实现用 asyncio.to_thread 跑到线程池，
    多个子代理可由 asyncio.gather 并发。

    deadline：time.monotonic() 绝对值，orchestrator 级全局墙钟预算透传（见
    orchestrator.run_orchestrator 的 _deadline）。与 config.wall_timeout_s
    取更早者，双层熔断。

    所有 worker 统一走 tool loop，避免同一任务在两套检索、抽证和事件语义之间漂移。
    """
    config = config or SubAgentConfig()
    from dra.toolloop import run_tool_loop  # 函数级 import：防 toolloop↔subagent 循环导入
    return await asyncio.to_thread(
        run_tool_loop, task, config, verbose=verbose, deadline=deadline,
        mission_context=mission_context)
