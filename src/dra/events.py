"""结构化进度事件总线：管线「发生了什么」一处发、多处订阅（终端 / 网页 SSE / 日志）。

为什么不直接 print
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
    step_done            step, ms, sid, worker_iteration
    llm_call             step, sid, worker_iteration, model, ms, input, output, io_complete?, in_tok, out_tok,
                         delegation_id?, parent_call_id?, call_id?, turn?; 完整 trace 另含 tools 工具定义
    llm_usage            每次 HTTP 尝试的 provider/model/attempt/outcome/raw_usage/cache_status、
                         cache_read_tokens/cache_miss_tokens/first_token_ms、费用口径与调用身份；安全参数白名单；完整 trace 时另含 request_messages/request_tools
    report_md            markdown, saved_path?, trace_path?   *
    error                message             *
    end                  （无字段）           *  前端据此 es.close()，防 EventSource 自动重连
    json_retry           reason, attempt, raw_head, exhausted?
    subagent_tool_call   sid, objective, call_no, tool, ok, args_summary, result_summary, evidence_total,
                         phase（"running" 实际执行已开始 / "done" 终态）、call_id（本次工具调用 id，
                         与 turn 一起唯一定位一次调用）、turn（Lead 回合号）、
                         delegation_id（sub1/sub2…，主线为空）、parent_call_id（发起派发的那次
                         工具调用 id，主线为空）——并行子 Agent 靠这两个字段归属事件、
                         model_call_id（模型调用账本 id，与工具 call_id 区分），完整 trace 的 done
                         事件另含 arguments_raw / result_full / io_complete；摘要字段仍有界，
                         status?（仅 phase="done"：ok|failed|rejected|cancelled|timeout）、
                         reason?（仅 phase="done"：失败/拒收原因，成功为空）、
                         display?（有界**结构化展示字段**，从完整回执截取，供工具卡显示真实
                         搜索词/标题/URL/片段/范围/收录数：search_sources→
                         {results:[{source_ref,title,url,snippet,published_at}],total,shown}、
                         read_source→{source_ref,title,url,shown_range,total_chars,has_more,
                         excerpts:[{excerpt_no,location,quote}],excerpt_total}、
                         save_evidence→{accepted,rejected,card_ids,submissions…}。
                         只是展示投影，模型看到的完整回执与工具协议不变）、
                         accepted?, rejected?, reject_reasons?（仅 save_evidence 附带）,
    agent_reasoning      与 lead_message 相同分片合同，独立 msg_id、call_id、kind=reasoning；interrupted 标明中断。仅接口可读纯文本。
    research_status      模式与进度摘要（仅 simple 路径使用）：mode="simple"、phase、
                         tool_calls、model_calls、sources、cards、allowed（服务端上限）、
                         stop_reason?、delivery?、coverage?、coverage_reasons?、requirements?、gaps?、warnings?（终态才带）；
                         plan_sync_status?（reported=已提交最终路线，missing=未同步，null/缺失=无路线或旧记录）。
                         零 Worker：这里是单 Lead 的进度，不是 Worker 派发。
    lead_message         Lead 在**执行当时**发出的普通公开说明（不是隐藏推理、不是事后播报）：
                         msg_id（稳定身份 = sid:turn）、turn（Lead 回合号）、
                         part_seq（**本条消息内**的分片序，从 0 起；与全局 seq 无关——
                         全局 seq 由 web 层分配，只管 SSE 去重与顺序）、
                         mode（"append" 增量 / "replace" 全量覆盖 / "complete" 仅标记结束）、
                         text（append/replace 带正文，complete 恒为空字符串）、
                         complete（是否已完整）、role、kind（note|answer）、label?。
                         同一 msg_id 的多条 lead_message 按 part_seq 拼出唯一正文；
                         **完成态不重复追加全文**——流式成功时终态只发 complete 标记，
                         只有非流式（无增量）才发一条 replace。
                         与调试 I/O 开关无关：普通用户必须始终能看到这些说明。
    cancelled            （无字段）           *  web 层在取消善终后补发
    （所有事件）run_id?   —— set_run_id 后自动附带，未设置则无此字段
    """

    SKILL_LOADED = "skill_loaded"
    DELEGATION_UPDATED = "delegation_updated"
    STEP_DONE = "step_done"
    LLM_CALL = "llm_call"
    LLM_USAGE = "llm_usage"
    REPORT_MD = "report_md"
    ERROR = "error"
    END = "end"
    JSON_RETRY = "json_retry"
    SUBAGENT_TOOL_CALL = "subagent_tool_call"
    CANCELLED = "cancelled"
    CONTEXT_COMPACTION = "context_compaction"
    EVIDENCE_JUDGED = "evidence_judged"  # results: {card_id: judgment}, role, delegation_id；提交卡片后发出。
    RESEARCH_STATUS = "research_status"
    LEAD_MESSAGE = "lead_message"
    AGENT_REASONING = "agent_reasoning"
    RESEARCH_PLAN_UPDATED = "research_plan_updated"
    RESEARCH_INPUT = "research_input"


# 新 run / plan attempt 的事件协议版本。缺失或不同版本的本地记录不可回放；
# 这是一次原子 breaking switch，不提供旧事件 normalizer。
EVENT_SCHEMA_VERSION = 3


_sinks: list[Callable[[dict], None]] = []
_lock = threading.Lock()

_run_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dra_run_id", default=None)


def set_run_id(rid: str | None) -> None:
    """给当前执行上下文绑定 run_id；此后本上下文（含 asyncio.to_thread 派生线程，
    copy_context 自动传播）emit 的每个事件都带 run_id 字段。多请求隔离的根。"""
    _run_id_var.set(rid)


def get_run_id() -> str | None:
    """读取当前运行身份，供 provider 会话路由复用；不包含用户问题或密钥。"""
    return _run_id_var.get()


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
