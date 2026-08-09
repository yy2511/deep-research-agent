"""轻量步骤计时：找瓶颈 + 过程可观测（实时打印每步耗时 + 末尾汇总）。

为什么自己写而不上 cProfile/py-spy：我们要的是「业务步骤级」耗时（search / condense /
tool_loop / cross_worker_audit / write_report …）而非函数级火焰图——profiler 会被 LLM/网络 IO 等待淹没、
还看不出是哪个研究步骤。一个 context manager 按步骤名累计，直接回答「哪一步最贵」。

并发口径（重要）：子代理跑在 asyncio.to_thread 线程里并发执行，多个线程并发 append 用锁兜住。
计时是「聚合」口径——并发步骤在墙钟上重叠，各步累计之和 > 墙钟时间。找瓶颈看「哪类步骤累计最久、
调用多少次、单次多慢」，占比是占聚合总时长（非占墙钟）。末尾同时给墙钟，聚合/墙钟≈并发度。
"""

import contextvars
import threading
import time
from contextlib import contextmanager

_lock = threading.Lock()
_records: list[tuple[str, float]] = []  # (step_name, seconds)
_verbose = True

# 可观测上下文（contextvars：异步 + 线程安全，跨 asyncio.to_thread 经 copy_context 自动传播，
# 并发子代理各持一份副本不互串）。让 llm.chat() 不改签名就能知道「这次 call 属于哪个子代理 /
# Worker 第几次迭代 / 哪个节点」——一处读上下文，替代给每个节点函数加 trace 参数的逐处补丁。
_obs_ctx: contextvars.ContextVar[dict] = contextvars.ContextVar("dra_obs_ctx", default={})


def set_ctx(**kw) -> contextvars.Token:
    """合并更新可观测上下文（sid/worker_iteration/step）；返回 token。"""
    cur = dict(_obs_ctx.get())
    cur.update(kw)
    return _obs_ctx.set(cur)


def get_ctx() -> dict:
    """读当前可观测上下文（副本）。"""
    return dict(_obs_ctx.get())


def reset_ctx(token: contextvars.Token) -> None:
    """恢复到 set_ctx 之前（跨 context 的 token 静默忽略，不弄崩主流程）。"""
    try:
        _obs_ctx.reset(token)
    except (ValueError, LookupError):
        pass


def clear_ctx() -> None:
    """清空可观测上下文（run 入口 / 测试隔离用）。"""
    _obs_ctx.set({})


def reset(verbose: bool = True) -> None:
    """每个 run 开始时清空计数（run_orchestrator 入口调一次）。"""
    global _verbose
    with _lock:
        _records.clear()
    _verbose = verbose
    clear_ctx()  # run 入口清干净可观测上下文，保证基线


@contextmanager
def step(name: str, *, note: str = ""):
    """给一段步骤计时；结束即累计 + 实时打印（flush，后台 tail 也能看见）。

    顺带把 name 写进可观测上下文（供 llm.chat 标注 call 归属），并在结束时 emit `step_done`
    事件（步骤级耗时，驱动前端节点计时）——展示层旁路，no-op-safe，绝不影响计时本身。
    """
    t0 = time.monotonic()
    token = set_ctx(step=name)
    try:
        yield
    finally:
        dt = time.monotonic() - t0
        reset_ctx(token)
        with _lock:
            _records.append((name, dt))
        if _verbose:
            tail = f" | {note}" if note else ""
            print(f"    ⏱ {name}: {dt:.1f}s{tail}", flush=True)
        try:  # 步骤级耗时事件（放最后、吞异常，绝不弄崩主流程 / 计时）
            from dra import events
            c = get_ctx()
            events.emit(
                events.EventType.STEP_DONE,
                step=name,
                ms=round(dt * 1000, 1),
                sid=c.get("sid"),
                worker_iteration=c.get("worker_iteration"),
            )
        except Exception:  # noqa: BLE001
            pass


def summary(wall_clock_s: float | None = None) -> str:
    """按步骤名聚合，返回「按累计耗时降序」的瓶颈表（markdown 友好的等宽文本）。"""
    with _lock:
        recs = list(_records)
    if not recs:
        return ""
    agg: dict[str, list[float]] = {}
    for name, dt in recs:
        agg.setdefault(name, []).append(dt)
    total = sum(dt for _, dt in recs) or 1e-9
    rows = sorted(((n, sum(v), len(v)) for n, v in agg.items()), key=lambda x: -x[1])
    out = ["", "===== 耗时汇总（聚合口径，并发步骤会重叠）====="]
    out.append(f"{'步骤':<14}{'累计':>9}{'次数':>6}{'单次均':>9}{'占聚合':>8}")
    for n, tot, cnt in rows:
        out.append(f"{n:<14}{tot:>8.1f}s{cnt:>6}{tot / cnt:>8.1f}s{tot / total * 100:>7.0f}%")
    out.append(f"{'聚合合计':<14}{total:>8.1f}s")
    if wall_clock_s:
        out.append(f"{'墙钟':<14}{wall_clock_s:>8.1f}s   （聚合/墙钟={total / wall_clock_s:.1f}× ≈ 平均并发度）")
    return "\n".join(out)
