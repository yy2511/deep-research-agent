"""主子 Agent 共用的模型/检索配置与双源检索适配。"""

import time
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dra.models import (
    RetrievedDoc,
    normalize_url,
)
from dra.tools import ddg_search, exa_search, web_search
from dra.search_observation import SearchCall


class SubAgentConfig(BaseModel):
    """子代理预算配置。"""

    search_top_k: int = 6
    model_config = ConfigDict(extra="forbid")
    # Python API 的代码默认档。Web 与自动化脚本读 runtime_config.json；
    # 因此这里不是所有入口共享的生产默认。模型实验应由调用方注入配置。
    model: str = "minimax-m3"
    provider: str = "opencode"
    # 默认关闭思考；调用方可按所选模型启用。
    reasoning: bool = False
    # 非 None 时透传 reasoning_effort，并要求 reasoning=True；provider 可能严格校验取值。
    effort: str | None = None
    # 研究循环的采样温度；None＝按 llm 的默认（开思考 0.6、不开 0.3）。评测对齐服务商默认时显式给值。
    temperature: float | None = Field(default=None, ge=0, le=2)
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
    # 现役研究不再按连续无效回合结束；开启调用次数限制时仍用于估算子 Agent 的模型额度。
    max_invalid_calls: int = 3
    # 降落保留区：额度只剩这么多次时
    # search/fetch_page 拒收（不耗预算、计 strike），只放行 save_evidence/finish——
    # 防止只读不存。0=关闭；harness 级门保持 provider 无关。
    save_reserve_calls: int = 2

    @model_validator(mode="after")
    def _co_knob_invariants(self) -> "SubAgentConfig":
        """相互关联的配置项一起校验，构造时就报错，不让矛盾的组合悄悄生效。
        依据见各行注释；确要突破限值，就改这里的断言并写明新依据。"""
        # effort=开思考并定档，与 reasoning=False（关思考）语义冲突——fail-loud 防
        # 「设了 effort 却被 thinking:disabled 盖掉/组合行为不明」的静默错档。
        if self.effort is not None and not self.reasoning:
            raise ValueError(
                f"effort={self.effort!r} 需要 reasoning=True（effort=开思考并定强度，"
                f"与关思考互斥；请同时设 reasoning=True，co-knob）")
        # 降落保留区必须小于总预算，否则模型从第 1 手就只能存/收工（退化为无检索）
        if not (0 <= self.save_reserve_calls < self.max_tool_calls):
            raise ValueError(
                f"save_reserve_calls={self.save_reserve_calls} 必须在 [0, "
                f"max_tool_calls={self.max_tool_calls}) 内（co-knob）")
        return self


def _retrieve(
    query: str, config: SubAgentConfig, *, verbose: bool = False,
    exclude_domains: list[str] | None = None, on_provider_event=None, deadline: float | None = None,
) -> list[RetrievedDoc]:
    """Tavily + Exa 双源 Web 检索，合并后按 URL 与片段精确去重。

    **双源并发**："web" 默认 = Tavily 和 Exa **ThreadPoolExecutor 并发**，
    各源独立 try/except 互不阻塞——单源故障仍能用另一源继续。两家返回的 URL 重叠率仅约 4%，
    合并后覆盖面接近翻倍（详见 docs/RETRIEVAL_SOURCES_DECISION.md）。

    错误语义:
    - 部分源成功部分源失败 → 用成功的，逐源诊断交给 on_provider_event；verbose 可另打印
    - 所有源都失败 → 抛 RuntimeError(对齐 web_search 单源契约,避免子代理空跑烧 LLM 钱)
    - 所有源返空列表(没失败也没结果) → 返 [],不抛(Tavily/Exa 偶发 0 results 是正常)

    融合去重只用精确层(source_url + snippet 前 80 字符)，保留同主题不同来源。

    ``exclude_domains`` 交给 Tavily / Exa 在服务端排除；DDG 不支持，由调用方再过滤。
    """
    per_k = config.search_top_k_per_source
    tasks = [("tavily", web_search, per_k), ("exa", exa_search, per_k)]

    def _run_one(name: str, fn, top_k: int):
        """单源执行:成功返 (name, docs),失败返 (name, Exception)。"""
        t0 = time.monotonic()
        with SearchCall(on_provider_event, name, query, top_k).scope() as trace:
            try:
                extra = {"exclude_domains": exclude_domains} if exclude_domains and name != "ddg" else {}
                if deadline is not None:
                    extra["deadline"] = deadline
                result = fn(query, top_k=top_k, **extra)
                dt = time.monotonic() - t0
                trace.finish(result_count=len(result))
                return name, result, dt, None
            except Exception as e:  # 各源独立隔离,不阻塞他源
                dt = time.monotonic() - t0
                trace.finish(error=e)
                return name, None, dt, e

    docs: list[RetrievedDoc] = []
    errors: dict[str, Exception] = {}

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
        from dra.llm import cancellation_requested

        if config.enable_search_fallback and not cancellation_requested() and (deadline is None or time.monotonic() < deadline):
            _, fallback_docs, _, e = _run_one("ddg", ddg_search, config.search_top_k)
            if e is not None:
                errors["ddg"] = e
            else:
                docs = fallback_docs
                if verbose:
                    print(f"  [检索] ⚠️ 双源全挂 → DDG 兜底 → {len(docs)} 篇（snippet-only 降档）")
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
