"""结构化进度事件总线：管线「发生了什么」一处发、多处订阅（终端 / 网页 SSE / 日志）。

为什么不直接 print（见 DEVLOG / EXPERIMENT_PLAN 观测线）
----------------------------------------------------------
- print 写死 stdout、是自由文本、把「数据」和「显示」焊死。事件总线把它们解耦：管线 emit
  带字段的事件对象，谁关心谁订阅——同一批事件可同时驱动网页实时进度树、NDJSON 日志、CLI。
- **无订阅 = no-op**：不订阅时 emit 什么也不做，所以 CLI/批量/测试路径零变化、零开销。
- **线程安全**：子代理跑在 asyncio.to_thread 线程里并发 emit，用全局 list + 锁兜住。
- **绝不弄崩主流程**：sink 回调抛错被吞（观测是展示层，不能影响研究本身）。

并发边界（诚实）：现已支持 run_id 隔离——调用方 `set_run_id(rid)` 绑定当前执行上下文
（`asyncio.to_thread` 派生线程经 copy_context 自动传播，无需逐函数透传参数），
之后本上下文 emit 的每个事件自动带 run_id 字段；`subscribe(fn, run_id=...)` 按
run_id 服务端过滤，两个并发 `/research` 请求的 SSE 流不再串流。CLI 单跑不设
run_id（或传 None）即旧行为，事件里不出现 run_id 字段，字节级不变。
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Callable
from enum import Enum


class EventType(str, Enum):
    """SSE 事件契约的单一真相源：管线真实 emit / web 演示回放 / 前端 handle 三处共用同一套类型名。

    这里把真实管线与前端消费的事件名收成一处枚举；Python 侧改名/拼错会在导入期暴露，
    前端生成物由 tests/test_events_gen.py 守卫。

    字段契约（type → 关键字段；* 号为 web 层补发，非核心管线 emit）
    ------------------------------------------------------------
    scope                query
    research_plan        count, initial_tasks[], plan_nodes[]
    report_plan          sections[], n_limitations, unresolved_node_ids
    task_batch_dispatched round_index, count, objectives[], phase
    ready_set_computed   round_index, pending, reason, n_tasks
    research_round_completed round_index, added, total, remaining
    nodes_assessed       assessments[{node_id, status, summary, gaps}], completed_ids[], unresolved_node_ids[]
    subagent_start       sid, objective, node_id, round_index
    subagent_done        sid, objective, node_id, round_index, tool_calls, evidence_count,
                         status(ok|empty|timeout|failed|hard_error),
                         stop_reason?(sufficient|no_progress|timeout|tool_budget),
                         summary（截断 600 字符）
    step_done            step, ms, sid, worker_iteration
    llm_call             step, sid, worker_iteration, model, ms, input, output, io_complete?, in_tok, out_tok
    collect              before, after, deduped, failures, n_sub
    cross_worker_audit   findings, reason, conflicts[{dimension, description}]
    writing              n_evidence, n_writer_evidence, n_groups
    shape_gate           phase(initial|final), missing[]
    report_md            markdown, saved_path?, trace_path?   *
    done                 status, n_evidence, completion_blockers, warnings, unresolved_node_ids
    error                message             *
    end                  （无字段）           *  前端据此 es.close()，防 EventSource 自动重连
    json_retry           reason, attempt, raw_head, exhausted?
    subagent_tool_call   sid, objective, call_no, tool, ok, args_summary, result_summary, evidence_total,
                         accepted?, rejected?, reject_reasons?（仅 save_evidence 附带）,
                         saved_cards?[{card_no, claim, support_quote, quote_truncated,
                         source_title, source_url, published_at}]（仅成功收录证据时附带；
                         card_no 为 Worker 内追加序，最终以报告 References 为准）,
                         links?[{title, url}]（仅 search 成功附带,来源可点验）,
                         url?, n_excerpts?（仅 fetch_page 成功附带）
    cancelled            （无字段）           *  web 层在 task.cancel() 善终后补发
    （所有事件）run_id?   —— set_run_id 后自动附带，未设置则无此字段
    """

    SCOPE = "scope"
    RESEARCH_PLAN = "research_plan"
    REPORT_PLAN = "report_plan"
    TASK_BATCH_DISPATCHED = "task_batch_dispatched"
    READY_SET_COMPUTED = "ready_set_computed"
    RESEARCH_ROUND_COMPLETED = "research_round_completed"
    NODES_ASSESSED = "nodes_assessed"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_DONE = "subagent_done"
    STEP_DONE = "step_done"
    LLM_CALL = "llm_call"
    COLLECT = "collect"
    CROSS_WORKER_AUDIT = "cross_worker_audit"
    WRITING = "writing"
    SHAPE_GATE = "shape_gate"
    REPORT_MD = "report_md"
    DONE = "done"
    ERROR = "error"
    END = "end"
    JSON_RETRY = "json_retry"
    SUBAGENT_TOOL_CALL = "subagent_tool_call"
    CANCELLED = "cancelled"


# 新 run / plan attempt 的事件协议版本。缺失或不同版本的本地记录不可回放；
# 这是一次原子 breaking switch，不提供旧事件 normalizer。
EVENT_SCHEMA_VERSION = 2


_sinks: list[Callable[[dict], None]] = []
_lock = threading.Lock()

_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dra_run_id", default=None)


def set_run_id(rid: str | None) -> None:
    """给当前执行上下文绑定 run_id；此后本上下文（含 asyncio.to_thread 派生线程，
    copy_context 自动传播）emit 的每个事件都带 run_id 字段。多请求隔离的根。"""
    _run_id_var.set(rid)


def subscribe(
    fn: Callable[[dict], None], *, run_id: str | None = None
) -> Callable[[dict], None]:
    """注册一个事件订阅者（收到每个事件 dict）。

    run_id 给定时只收该 run 的事件（服务端过滤，JS 零改动）；不给（默认）则收所有事件，
    行为与旧版一致。**API 契约**：返回值是实际注册的可调用对象，unsubscribe 必须用
    返回值而非原始 fn——过滤模式下 fn 被包了一层，原始 fn 从未进入 _sinks。
    """
    handle = fn
    if run_id is not None:
        def handle(evt: dict, _fn=fn, _rid=run_id) -> None:
            if evt.get("run_id") == _rid:
                _fn(evt)
    with _lock:
        _sinks.append(handle)
    return handle


def unsubscribe(fn: Callable[[dict], None]) -> None:
    """注销订阅者（找不到则忽略）。"""
    with _lock:
        if fn in _sinks:
            _sinks.remove(fn)


def emit(event_type: EventType | str, **data) -> None:
    """发一个事件：{type, ts, **data} 推给所有订阅者。无订阅则直接返回（no-op）。

    event_type 可收 EventType 或裸 str；落盘前统一取 .value，
    保证 SSE 线上 type 恒为纯字符串（不依赖 json 对 str-Enum 的处理跨版本差异）。
    """
    with _lock:
        if not _sinks:
            return
        sinks = list(_sinks)
    et = event_type.value if isinstance(event_type, EventType) else event_type
    evt = {"type": et, "ts": time.time(), **data}
    rid = _run_id_var.get()
    if rid is not None:
        evt.setdefault("run_id", rid)
    for fn in sinks:
        try:
            fn(evt)
        except Exception:  # noqa: BLE001 — 展示层订阅者绝不能弄崩主流程
            pass
