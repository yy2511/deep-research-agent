"""LLM 客户端：薄封装 OpenAI 兼容接口（多 provider 注册表，按 provider 缓存 client + 流式）。

模型选择分入口：
- 自动化脚本与 Web 共享 ``runtime_config.json``；
- Python API 使用调用方配置或 SimpleConfig/SubAgentConfig 代码默认。

当前默认值与各入口配置边界见 STATUS.md。

用量由 execution_scope 绑定的 UsageMeter 按运行记录。
"""

import contextvars
import hashlib
import json
import os
import re
import time
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
import httpx
from openai import APIStatusError, APITimeoutError, OpenAI
from pydantic import BaseModel, Field

# 4xx 客户端错误立即上抛，重试无意义：
#   400 BadRequest / 401 Auth / 403 Permission / 404 NotFound / 422 Validation
# 例外 429 RateLimit 和 5xx 走重试路径。
_NO_RETRY_STATUS = {400, 401, 403, 404, 422}

# SDK 的默认 read timeout 是 600s，且默认自动重试 2 次；再叠项目自己的重试会把
# 单个调用拉到不可解释的长等待。项目统一管理重试，SDK 一律关闭内建重试；每次请求
# 默认最多等 90s，调用方可显式给更长预算。
DEFAULT_REQUEST_TIMEOUT_S = 90.0
DEFAULT_MAX_RETRIES = 1


class LLMRequestTimeout(TimeoutError):
    """一次 LLM 请求在项目允许的等待时间内没有返回。"""


class LLMDeadlineExceeded(LLMRequestTimeout):
    """研究运行的整体 deadline 已耗尽，不能再发起或重试 LLM 请求。"""


class LLMProtocolError(RuntimeError):
    """Provider 返回的不是 OpenAI ChatCompletion 协议响应。"""


class LLMPersistenceError(RuntimeError):
    """用量账本失败，不能作为网络故障重试。"""


class LLMStreamIncomplete(RuntimeError):
    """流式回包在终止标记之前就结束了（空流 / 缺 ``finish_reason``）。

    这是**传输层**现象，不是模型给出的协议结论：请求已被接受、连接正常关闭，
    但没有终止标记，因此无法确认回复已收完。按可重试处理（换连接重发）；
    重试用尽后把未完成的 ``turn`` 交回调用方，保持原有语义——
    **一个工具都不执行，上层如实记为协议错误并走收尾**。

    真实运行里见过：某一轮拿到空流（content 与 usage 都为空、无 finish_reason）。
    如果当成不可恢复的错误，整轮研究会就此终止，所以按可重试处理。
    """

    def __init__(self, turn: "ToolTurn") -> None:
        super().__init__(turn.note or "流式回包未给出终止标记")
        self.turn = turn


class LLMChatStreamIncomplete(RuntimeError):
    """普通对话的流式回包在终止标记之前就结束了：内容不完整，按传输故障重试。"""


_request_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "dra_llm_request_deadline", default=None
)
_trace_full_io: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "dra_llm_trace_full_io", default=False
)
_cancel_event: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "dra_llm_cancel_event", default=None
)
_usage_meter: contextvars.ContextVar["UsageMeter | None"] = contextvars.ContextVar(
    "dra_llm_usage_meter", default=None
)
_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "dra_llm_session_id", default=None
)
_standalone_session_id = uuid4().hex


class UsageMeter:
    """按 run/角色计费旁路；to_thread 共享此对象，不使用旧的进程级 reset。

    只统计 provider 成功返回的 usage；缺失 usage 的响应显式计数，失败请求的
    服务端消耗不可知。snapshot 是取样时的值，取消后迟到的响应不会改写已返回结果。
    """

    def __init__(self, *, initial=None, on_record=None):
        self._lock = threading.Lock()
        self._roles: dict[str, dict[str, int]] = {
            role: dict(counts) for role, counts in (initial or {}).items()
        }
        self._on_record = on_record

    def record(self, usage) -> None:
        from dra import timing

        role = timing.get_ctx().get("usage_role", "other")
        with self._lock:
            bucket = self._roles.setdefault(role, {"input": 0, "output": 0, "responses": 0,
                                                  "missing_usage": 0})
            bucket["responses"] += 1
            incoming = getattr(usage, "prompt_tokens", None)
            outgoing = getattr(usage, "completion_tokens", None)
            bucket["input"] += incoming or 0
            bucket["output"] += outgoing or 0
            bucket["missing_usage"] += int(incoming is None or outgoing is None)
        if self._on_record is not None:
            self._on_record()

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {role: dict(counts) for role, counts in self._roles.items()}


@contextmanager
def execution_scope(*, deadline=None, cancel_event=None, meter=None, session_id=None):
    """为一个同步调用/异步 run 绑定可传播的 deadline、取消信号与用量收集器。"""
    dt = _request_deadline.set(deadline)
    ct = _cancel_event.set(cancel_event)
    mt = _usage_meter.set(meter if meter is not None else _usage_meter.get())
    st = _session_id.set(session_id or _session_id.get() or uuid4().hex)
    try:
        yield
    finally:
        _session_id.reset(st)
        _usage_meter.reset(mt)
        _cancel_event.reset(ct)
        _request_deadline.reset(dt)


def current_cancel_event():
    """当前执行上下文绑定的取消信号；未绑定时为 None。

    供嵌套入口继承外层取消语义：新开 execution_scope 不再把已有信号吞掉。
    """
    return _cancel_event.get()


def cancellation_requested() -> bool:
    event = _cancel_event.get()
    return event is not None and event.is_set()


def _record_usage(usage) -> None:
    meter = _usage_meter.get()
    if meter is not None:
        meter.record(usage)


def set_request_deadline(deadline: float | None) -> contextvars.Token:
    """绑定当前 run 的单调时钟 deadline。

    ``asyncio.to_thread`` 会复制 contextvars，因此 worker 线程也会继承它；worker
    自己可再覆盖为更早的子代理 deadline。每次 chat/call_tools 都以剩余时间截断
    HTTP read timeout，避免全局 40 分钟只在步骤边界才生效。
    """
    return _request_deadline.set(deadline)


def reset_request_deadline(token: contextvars.Token) -> None:
    """恢复调用前 deadline；跨上下文 token 不应影响主流程。"""
    try:
        _request_deadline.reset(token)
    except (ValueError, LookupError):
        pass


def set_trace_full_io(enabled: bool) -> contextvars.Token:
    """为当前规划或研究运行启用完整 LLM 输入输出 trace。

    ContextVar 会随 ``asyncio.to_thread`` 传播，又不会串到其它并发运行。完整模式
    只应用于显式从 Web 调试档发起的新调用；普通运行继续使用截断 trace。
    """
    return _trace_full_io.set(bool(enabled))


def reset_trace_full_io(token: contextvars.Token) -> None:
    """恢复调用前的 trace 完整度设置。"""
    try:
        _trace_full_io.reset(token)
    except (ValueError, LookupError):
        pass


def trace_full_io_enabled() -> bool:
    """模型与工具诊断共用同一显式开关，随执行上下文传播。"""
    return _trace_full_io.get()


def _effective_request_timeout(request_timeout_s: float) -> float:
    if cancellation_requested():
        raise LLMDeadlineExceeded("LLM 请求未发出：当前运行已取消")
    if request_timeout_s <= 0:
        raise ValueError("request_timeout_s 必须 > 0")
    deadline = _request_deadline.get()
    if deadline is None:
        return float(request_timeout_s)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LLMDeadlineExceeded("LLM 请求未发出：研究总 deadline 已耗尽")
    return min(float(request_timeout_s), remaining)


def _deadline_expired() -> bool:
    """当前 run 的墙钟 deadline 是否已到（没有绑定 deadline 时为 False）。

    流式消费要在**每个 chunk 之间**查它：HTTP read timeout 只在没有新数据时计时，
    持续有 chunk 的长流可以跑满任意长的时间，光靠单次请求 timeout 兜不住总预算。
    """
    deadline = _request_deadline.get()
    return deadline is not None and time.monotonic() >= deadline


def _stream_usage_supported(provider: str, model: str) -> bool:
    """该 provider/模型是否可安全要求流式用量回执（``stream_options.include_usage``）。

    只有**已核实**的通道才开：OpenCode Go 的流式接口确认会返回 include_usage 用量。其它 OpenAI-compatible 中转可能
    不认这个字段直接 400，宁可不传、按 usage_unknown 如实记账。
    """
    return provider == "opencode"

# 与 tools.py 一致：显式定位项目根 .env，不依赖运行方式
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# 兜底默认模型：仅当调用方未显式传 model 时生效。
# 当前模型入口以 STATUS.md §3 为准。
DEFAULT_MODEL = "deepseek-v3.2-251201"

# 多 provider 注册表：provider 名 → (key 环境变量名, base_url 环境变量名)。
# 同一套 OpenAI 兼容 SDK，靠不同 key + base_url 直连不同厂商。
# 加新 provider 只需在这里加一行 + .env 配好对应变量。
_PROVIDERS: dict[str, tuple[str, str]] = {
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),        # 默认：zetatechs 中转
    # 用 DRA_ 前缀是为了避开 DEEPSEEK_BASE_URL 这个名字：本机另一个工具把它当保留变量，
    # 项目 .env 里出现该名会让那个工具从本目录启动时报错。
    "deepseek": ("DEEPSEEK_API_KEY", "DRA_DEEPSEEK_BASE_URL"),  # 直连 deepseek
    "opencode": ("OPENCODE_API_KEY", "OPENCODE_BASE_URL"),  # opencode-go 套餐：OpenAI 兼容端点
    "codex_local": ("CODEX_LOCAL_API_KEY", "CODEX_LOCAL_BASE_URL"),  # 本机自建代理（用户自搭，暴露 gpt-5.5 等；仅本机测评用）
    "codex521": ("CODEX521_API_KEY", "CODEX521_BASE_URL"),  # 用户自建 codex 中转（暴露 gpt-5.5 等；元组约定 (KEY, BASE_URL)）
    "kimi": ("KIMI_API_KEY", "KIMI_BASE_URL"),
}

# 已改名变量的兼容回退：provider 名 → 旧 base_url 变量名（按顺序尝试）。
# deepseek 的 base_url 变量改名为 DRA_DEEPSEEK_BASE_URL（原因见上），但已部署的容器、
# CI 和其他机器仍可能只配旧名。没有回退时 base_url 会解析成 None，SDK 于是静默回落到
# 官方 OpenAI 端点——配置看起来"配了"，请求却发到了别的厂商。
# 回退只读旧名，不要求 .env 保留它；新名存在时永远优先。
_BASE_URL_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "deepseek": ("DEEPSEEK_BASE_URL",),
}

# 按 provider 缓存 client（惰性）。单例升级为字典，支持多厂商共存。
_clients: dict[str, OpenAI] = {}

# 连接级故障：这类异常重试无意义**除非换一条新连接**，故重试前必须丢弃缓存 client。
# 与 tools.py 的 _CONN_ERROR_MARKERS / _reset_client 同因同治——那边是 Tavily 的 requests
# session，这边是 OpenAI SDK 的 httpx 连接池，表现都是复用断掉的 keep-alive 连接时
# 拿到 SSLEOFError（UNEXPECTED_EOF_WHILE_READING），且每次重试都原地再挂。
# 用类名匹配而非 isinstance，避免在 llm 层硬依赖 httpx/SSL 的异常类型。
_CONN_ERROR_MARKERS = (
    "SSLError", "SSLEOFError", "UNEXPECTED_EOF", "ConnectionError",
    "APIConnectionError", "ConnectError", "ConnectionResetError",
    "RemoteDisconnected", "Max retries exceeded", "Connection aborted",
    "EOF occurred",
)


def _is_conn_error(e: Exception) -> bool:
    """粗判是否连接级故障（据异常链文本），决定要不要换连接重试。"""
    text = f"{type(e).__name__}: {e}"
    cause = e
    seen = 0
    while cause is not None and seen < 5:  # 顺异常链找根因（SDK 会层层包裹）
        text += f" | {type(cause).__name__}: {cause}"
        cause = cause.__cause__ or cause.__context__
        seen += 1
    return any(m in text for m in _CONN_ERROR_MARKERS)


def _discard_client(provider: str) -> None:
    """丢弃该 provider 缓存的 client（连同它的连接池），强制下次重新握手。

    模型接口的失败多是连接阶段的 SSL EOF（约 5 秒、无回执）。只重试而不换连接，
    重试会复用同一根已断的连接，结果一样，所以要先丢弃旧连接。
    """
    _clients.pop(provider, None)

def _env_value(name: str) -> str | None:
    """读取环境变量并归一化；.env 里有的值写成 KEY = "v"，带空格 / 引号。"""
    return (os.environ.get(name) or "").strip().strip('"').strip() or None


def _base_url_env_names(provider: str) -> tuple[str, ...]:
    """当前生效的 base_url 变量名，含兼容回退名；解析与错误提示共用。"""
    return (_PROVIDERS[provider][1], *_BASE_URL_ENV_ALIASES.get(provider, ()))


def _resolve_base_url(provider: str) -> str | None:
    """按新名→旧名顺序解析 base_url；都为空才返回 None（走官方 OpenAI）。

    只读 `os.environ`，**不**加载 .env：调用方必须先 `load_dotenv(_ENV_PATH)`，
    否则会把"已配置"误判成"未配置"；调用方必须先加载环境配置。
    """
    for name in _base_url_env_names(provider):
        value = _env_value(name)
        if value is not None:
            return value
    return None


def _get_client(provider: str = "openai") -> OpenAI:
    """惰性初始化并缓存指定 provider 的 OpenAI 兼容客户端。

    base_url 为空走官方 OpenAI；未知 provider 直接报错（防拼写错默默走错厂商）。
    """
    if provider not in _clients:
        from dra import provider_registry
        if not provider_registry.exists(provider):
            raise RuntimeError(
                f"未知 provider: {provider!r}，可选 {list(_PROVIDERS)}"
            )
        # override=True：本项目以 .env 为准，避免被 shell（如 ~/.zshrc 的
        # 历史 export）里同名变量污染。dotenv 默认 override=False，是常见坑。
        key, base_url = provider_registry.credentials(provider)
        if not key:
            key_name = _PROVIDERS[provider][0] if provider in _PROVIDERS else "API Key"
            raise RuntimeError(f"缺少 {key_name}，请在项目根 .env 中配置")
        # SDK 默认还会自行重试两次；它和本模块的重试叠起来会把一次网络故障
        # 放大成多轮 600 秒等待。重试策略只保留在 chat/call_tools 这一层，便于
        # 统一受 request_timeout_s 与 run deadline 约束。
        _clients[provider] = OpenAI(api_key=key, base_url=base_url, max_retries=0)
    return _clients[provider]


class ProviderStatus(BaseModel):
    name: str
    key_configured: bool
    reachable: bool
    models: list[str] = []
    error: str | None = None
    label: str = ""
    custom: bool = False
    base_url: str = ""
    checked_at: float | None = None
    model_details: list[dict] = Field(default_factory=list)
    catalog_provider: str = ""
    reasoning_format: str = "auto"


# ---------------------------------------------------------------------------
# trace 插桩：实时可视化要看的 LLM 节点（input/output/耗时）。chat 是所有 call 的唯一咽喉，
# 在此一处插桩；按 obs 上下文（timing.get_ctx 的 step 名）前缀过滤——只对这些节点 emit
# llm_call，其余节点（如 condense 走 ThreadPool 不传 contextvar / format_gate 等纯函数）只计时不抓 io。
# 加节点只需扩这个元组。input 截头尾（常含整篇检索文档上万字）、output 全量留（看模型判断全貌）。
# ---------------------------------------------------------------------------
_TRACE_STEP_PREFIXES = (
    "condense",
    "vision_observation",
    "lead_research",
    "lead_wrapup",
    "subagent_research",
    "subagent_wrapup",
    "research_brief",
)
_INPUT_HEAD = 1600
_INPUT_TAIL = 1200
_OUTPUT_CAP = 60000


def _render_input(messages: list[dict]) -> str:
    """把实际发送的 messages（包括 tool_calls 等附加字段）完整渲染。"""
    rendered: list[str] = []
    for message in messages:
        role = message.get("role", "?")
        block = f"【{role}】\n{message.get('content', '')}"
        extras = {k: v for k, v in message.items() if k not in {"role", "content"}}
        if extras:
            block += "\n" + json.dumps(extras, ensure_ascii=False, default=str)
        rendered.append(block)
    return "\n\n".join(rendered)


def _truncate_input(messages: list[dict]) -> str:
    """把 messages 拼成可读文本，超长截头尾（普通 trace 的轻量策略）。"""
    text = _render_input(messages)
    if len(text) <= _INPUT_HEAD + _INPUT_TAIL + 200:
        return text
    omitted = len(text) - _INPUT_HEAD - _INPUT_TAIL
    return f"{text[:_INPUT_HEAD]}\n\n…（省略中间 {omitted} 字）…\n\n{text[-_INPUT_TAIL:]}"


# 自建 OpenAI-compatible Codex 中转：暴露 GPT-5.x，关推理走 reasoning_effort=none
# （与显式 effort 同通道透传；不能发 opencode 的 thinking:disabled）。
_CODEX_REASONING_PROVIDERS = frozenset({"codex521", "codex_local"})


# 请求关闭思考时被服务商拒绝过的 (provider, model)：之后不再发送关闭参数。
# 模型目录分辨不出“能否关闭思考”（能关与只能开的模型资料一样），只能按服务商的拒绝来认。
_THINKING_ONLY: set[tuple[str, str]] = set()


def _default_temperature(temperature: float | None, *, reasoning: bool, effort: str | None) -> float:
    """调用方没指定温度时：开思考用 0.6，不开用 0.3。

    DeepSeek-R1 官方建议推理模型温度取 0.5–0.7（推荐 0.6），过低容易无限重复；
    实际运行里见过开思考后主 Agent 反复输出同一句话。部分服务会忽略该参数，属正常。
    """
    if temperature is not None:
        return temperature
    return 0.6 if reasoning or effort is not None else 0.3


def _rejects_disabling_thinking(exc: Exception) -> bool:
    """服务商是否以“该模型不支持关闭思考”拒绝了请求（如 GLM-5.3 的 thinking-only 报错）。"""
    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    return status == 400 and ("thinking" in text or "reasoning" in text) and any(
        word in text for word in ("not supported", "unsupported", "thinking-only", "cannot be disabled", "can't be disabled"))


def _reasoning_extra_body(
    *, provider: str, reasoning: bool, effort: str | None,
) -> dict | None:
    """构造 reasoning 控制用的 extra_body；chat / call_tools 共用，语义必须一致。

    优先级：
    1. effort 非 None → {"reasoning_effort": effort}（provider-agnostic，开思考并定档）
    2. reasoning=False + opencode → thinking disabled
    3. reasoning=False + codex521/codex_local → reasoning_effort "none"
    4. 其它 → None（不注入，避免给不支持的模型乱传）

    effort 与关推理参数互斥：effort 优先，绝不同时发两个。
    """
    if provider.startswith('custom_'):
        from dra.provider_registry import reasoning_format
        profile=reasoning_format(provider)
        if profile=='none':return None
        if profile=='openrouter':
            return {'reasoning':{'effort':effort}} if effort is not None else {'reasoning':{'enabled':reasoning}}
        if profile=='thinking':
            body={'thinking':{'type':'enabled' if reasoning or effort is not None else 'disabled'}}
            if effort is not None:body['reasoning_effort']=effort
            return body
        # 通用兼容端点只有显式档位才发送参数；默认不假设支持关闭思考。
    if effort is not None:
        return {"reasoning_effort": effort}
    if not reasoning:
        if provider == "opencode":
            return {"thinking": {"type": "disabled"}}
        if provider in _CODEX_REASONING_PROVIDERS:
            return {"reasoning_effort": "none"}
    return None


def _require_chat_completion(resp, *, provider: str, model: str):
    """在访问 ``usage/choices`` 前校验 OpenAI-compatible 响应形态。

    OpenAI SDK 默认对 HTTP 200 的非 JSON 响应不做严格校验，而是直接返回 ``str``。
    中转 Base URL 误指向站点首页时，过去会在 ``resp.usage`` 处报一个误导性的
    AttributeError；这里把它收敛成可操作、无需重试的配置错误。
    """
    choices = getattr(resp, "choices", None)
    if choices:
        return resp

    if isinstance(resp, str):
        stripped = resp.lstrip().lower()
        response_kind = "HTML 页面" if stripped.startswith(("<!doctype html", "<html")) else "文本"
    else:
        response_kind = type(resp).__name__
    setting = (' 或 '.join(_base_url_env_names(provider)) if provider in _PROVIDERS
               else '自定义连接的 Base URL')
    raise LLMProtocolError(
        f"provider={provider!r} model={model!r} 返回了 {response_kind}，"
        "不是 OpenAI ChatCompletion JSON；"
        f"请检查 {setting} 是否指向正确的"
        " OpenAI-compatible API 根路径（通常以 /v1 结尾）"
    )


def _trace_llm_call(messages: list[dict], output: str, t0: float, model: str, usage,
                    *, tools: list[dict] | None = None) -> None:
    """成功一次 chat 后，按当前 obs 上下文 emit llm_call（仅 TRACE 节点）；吞异常、不碰主流程。"""
    try:
        from dra import events, timing
        ctx = timing.get_ctx()
        step = ctx.get("step") or ""
        if not step.startswith(_TRACE_STEP_PREFIXES):
            return
        full_io = _trace_full_io.get()
        input_text = _render_input(messages) if full_io else _truncate_input(messages)
        out = (
            output
            if full_io or len(output) <= _OUTPUT_CAP
            else f"{output[:_OUTPUT_CAP]}\n…（截断，共 {len(output)} 字）"
        )
        tok = {}
        if usage is not None:
            tok = {"in_tok": getattr(usage, "prompt_tokens", None),
                   "out_tok": getattr(usage, "completion_tokens", None)}
        events.emit(
            events.EventType.LLM_CALL,
            step=step,
            usage_role=ctx.get("usage_role"),
            sid=ctx.get("sid"),
            worker_iteration=ctx.get("worker_iteration"),
            delegation_id=ctx.get("delegation_id"),
            parent_call_id=ctx.get("parent_call_id"),
            call_id=ctx.get("call_id"),
            turn=ctx.get("turn"),
            model=model,
            ms=round((time.monotonic() - t0) * 1000, 1),
            input=input_text,
            output=out,
            io_complete=full_io,
            **({"tools": tools} if full_io and tools is not None else {}),
            **tok,
        )
    except Exception:  # noqa: BLE001 — 展示层旁路绝不弄崩主流程
        pass


def _provider_headers(provider: str) -> dict:
    """OpenCode 要求真实客户端身份和稳定会话 ID，用于路由与缓存。

    run_id 跨恢复稳定；Worker 按 sid 隔离。无 run 的直接调用使用 execution_scope
    的身份，未包 scope 的独立调用才退到进程会话。绝不把问题正文、路径或密钥放进头。
    """
    if provider != "opencode":
        return {}
    from dra import events, timing

    ctx = timing.get_ctx()
    identity = [_session_id.get() or events.get_run_id() or _standalone_session_id,
                ctx.get("usage_role", ""), ctx.get("sid", "")]
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return {"extra_headers": {"User-Agent": "deep-research-agent/0.0.1",
                              "x-opencode-session": f"dra-{digest}"}}


def _observed_create(client, kwargs, *, provider, model, attempt):
    from dra.llm_observation import observed_create

    def abort_reason():
        if cancellation_requested():
            return "cancelled"
        return "deadline" if _deadline_expired() else None

    return observed_create(client, kwargs, provider=provider, model=model,
                           attempt=attempt, abort_reason=abort_reason, full_io=trace_full_io_enabled())


def chat(
    messages: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    provider: str = "openai",
    temperature: float | None = None,
    max_tokens: int | None = 1024,
    max_retries: int = DEFAULT_MAX_RETRIES,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    response_format: dict | None = None,
    reasoning: bool = True,
    effort: str | None = None,
    on_chunk: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str], None] | None = None,
) -> str:
    """发一轮对话，返回文本内容。失败重试 ≤ max_retries（指数退避）。

    每一轮 HTTP 请求都有 ``request_timeout_s`` 上限（默认 90 秒），并会自动
    截断到当前 run 的剩余 deadline。SDK 内建重试已在 client 构造时关闭，因此
    ``max_retries`` 是唯一的网络重试所有者。

    ``max_tokens=None`` 时**不向上游发送该字段**，由服务端/模型自身决定输出长度
    （当前「先跑通链路、限额后续再调」的策略）；传整数则照常作为硬上限。

    effort（思考强度）：非 None 时 extra_body 发 {"reasoning_effort": effort}，
    provider 无关透传——zeta 用模型名后缀（gpt-5.5-high）不需要它；换别家 OpenAI 兼容
    中转时用它设档。值不做白名单（各家档位表不同：low/medium/high/xhigh/none…），
    端点自己校验。与 reasoning=False 语义互斥（effort=开思考并定档）：本层 effort
    优先、绝不同时发关闭参数（thinking:disabled 或 reasoning_effort:none）；档位 config
    的校验器另有 fail-loud。
    注意：端点**严格校验取值**——deepseek@opencode 认合法档（high 不报错，
    不代表内部真分档）、乱值（如 "banana"）直接 400。故风险是档位名拼错/端点档位表
    与预期不符 → 400 崩当次调用，不是「模型不支持就静默忽略」；换中转前先确认合法档表。

    on_chunk（流式，非侵入）：给定时走 stream=True，每收到一个增量 token 就调 on_chunk(片段)，
    同时累计并最终返回完整字符串——**返回值与非流式完全一致，所有现有调用方不传它即零变化**。
    回调里抛错会被吞（流式只是展示层，绝不能弄崩主流程）。流式分支不强制 stream_options，
    部分中转不回传 usage 时该次调用的 token 不计入计数（诚实降级，换便宜健壮）。

    on_reasoning（流式，非侵入）：给定时同样走 stream=True，每收到一段思考内容
    （``reasoning_content`` / ``reasoning``）就回调一次，供界面实时展示；思考内容不计入返回值。

    provider 选择走哪个厂商（见 _PROVIDERS）；默认 openai。
    response_format 透传给底层 API，如 {"type": "json_object"} 强制合法 JSON。

    reasoning=False：按 provider 确定性关掉思维链（见 _reasoning_extra_body）：
    - opencode → thinking:{type:disabled}（该语法 opencode 特有，传给其它厂商会 400）
    - codex521 / codex_local（自建 OpenAI-compatible Codex 中转，暴露 GPT-5.x）→
      reasoning_effort:"none"（与显式 effort 同通道，effort 优先、不同时发）
    - 其它 provider → 不注入 extra_body（避免给不支持的模型乱传）
    机械活（抽取/摘要/子代理反思）关推理 = 更快更省更稳；拆解/写作/核查等保持开推理。
    """
    client = _get_client(provider)
    requested_temperature = temperature
    if (provider, model) in _THINKING_ONLY:
        reasoning = True
    temperature = _default_temperature(temperature, reasoning=reasoning, effort=effort)
    kwargs: dict = dict(model=model, messages=messages, temperature=temperature)
    if max_tokens is not None:
        # None = 不设输出上限：连字段都不发，避免上游把 null 当成非法参数。
        kwargs["max_tokens"] = max_tokens
    kwargs.update(_provider_headers(provider))
    # qwen/gemini 过 zetatechs 中转时不兼容 response_format，传了返回空字符。
    # 检查 model 前缀跳过；deepseek 系不受影响（直接透传）。
    if response_format is not None and not model.startswith(("qwen", "gemini")):
        kwargs["response_format"] = response_format
        # OpenAI/Codex 后端硬约束：用 json_object 时 **input(user) message** 里必须字面出现
        # "json"，否则 400（错误原文强调 "input messages"——system 里有 json 不算数）。
        # 有的中转不强制这一条，但官方后端会严格检查。
        # 补到最后一条 message 末尾（通常是 user），对不检查此条的 provider 无害（多个词不改语义）。
        if response_format.get("type") == "json_object" and messages:
            last = messages[-1]
            if "json" not in str(last.get("content", "")).lower():
                patched = [dict(m) for m in messages]
                patched[-1]["content"] = f"{patched[-1].get('content', '')}\n\n（请以 JSON 格式输出）"
                kwargs["messages"] = patched
    extra = _reasoning_extra_body(provider=provider, reasoning=reasoning, effort=effort)
    if extra is not None:
        kwargs["extra_body"] = extra
    streaming = on_chunk is not None or on_reasoning is not None
    if streaming:
        kwargs["stream"] = True
        if _stream_usage_supported(provider, model):
            kwargs["stream_options"] = {"include_usage": True}
    _t0 = time.monotonic()  # trace：本次 chat 起点（成功时算 call 墙钟耗时）
    for attempt in range(max_retries + 1):
        try:
            # 每次重试重新计算：退避时间也计入总 deadline，不能让第二次请求越界。
            # client 也在这里重取：连接级故障会 _discard_client()，下一轮拿到全新连接池
            # （只加重试不换连接，重试会复用断掉的 keep-alive 连接，结果一样）。
            client = _get_client(provider)
            request_kwargs = dict(kwargs)
            request_kwargs["timeout"] = _effective_request_timeout(request_timeout_s)
            if streaming:
                # 流式分支：逐 chunk 回调 + 累计，返回完整内容（与非流式一致）
                parts: list[str] = []
                usage = None
                finish = None
                stream = _observed_create(client, request_kwargs, provider=provider, model=model, attempt=attempt + 1)
                try:
                    for chunk in stream:
                        u = getattr(chunk, "usage", None)
                        if u:
                            usage = u
                        if cancellation_requested():
                            # 与 call_tools 一致：取消后不再读流，立即结束这次调用。
                            raise LLMDeadlineExceeded("LLM 流式输出已中止：当前运行已取消")
                        choices = getattr(chunk, "choices", None)
                        if not choices:
                            continue
                        finish = getattr(choices[0], "finish_reason", None) or finish
                        delta = choices[0].delta
                        thought = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                        if thought and on_reasoning is not None and isinstance(thought, str):
                            try:
                                on_reasoning(thought)
                            except Exception:  # noqa: BLE001 — 展示层回调绝不能弄崩主流程
                                pass
                        piece = getattr(delta, "content", None) or ""
                        if piece:
                            parts.append(piece)
                            if on_chunk is not None:
                                try:
                                    on_chunk(piece)
                                except Exception:  # noqa: BLE001 — 展示层回调绝不能弄崩主流程
                                    pass
                finally:
                    close = getattr(stream, "close", None)
                    if close is not None:
                        close()
                _record_usage(usage)
                if finish is None:
                    # 流在结束标记之前就断了：内容不完整，按传输故障重试，不当作“模型返回了空内容”。
                    raise LLMChatStreamIncomplete("流式回包未给出终止标记，回复可能被上游中断")
                _text = "".join(parts)
                _trace_llm_call(messages, _text, _t0, model, usage)
                return _text
            resp = _require_chat_completion(
                _observed_create(client, request_kwargs, provider=provider, model=model, attempt=attempt + 1),
                provider=provider,
                model=model,
            )
            usage = getattr(resp, "usage", None)
            _record_usage(usage)
            _text = resp.choices[0].message.content or ""
            _trace_llm_call(messages, _text, _t0, model, usage)
            return _text
        except LLMDeadlineExceeded:
            # deadline 已耗尽时重试没有意义，也不能被下面的通用网络异常吞掉。
            raise
        except LLMProtocolError:
            # Base URL / 中转协议错不会靠重试恢复，保留可操作的原始报错。
            raise
        except (APITimeoutError, httpx.TimeoutException) as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise LLMRequestTimeout(
                f"LLM 请求超时（单次上限 {request_timeout_s:g}s，已重试 {max_retries} 次）"
            ) from e
        except APIStatusError as e:
            if not reasoning and _rejects_disabling_thinking(e):
                # 只能思考的模型：记住它，不带关闭参数重发一次（不计入重试次数）。
                _THINKING_ONLY.add((provider, model))
                return chat(messages, model=model, provider=provider, temperature=requested_temperature,
                            max_tokens=max_tokens, max_retries=max_retries,
                            request_timeout_s=request_timeout_s, response_format=response_format,
                            reasoning=True, effort=effort, on_chunk=on_chunk, on_reasoning=on_reasoning)
            # 4xx 客户端错误（key/参数/权限错）立刻上抛——再试也错
            if e.status_code in _NO_RETRY_STATUS:
                raise RuntimeError(
                    f"LLM 调用失败（HTTP {e.status_code}，4xx 不重试）: {e}"
                ) from e
            # 429 / 5xx 走退避重试
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(
                f"LLM 调用失败（HTTP {e.status_code}，已重试 {max_retries} 次）: {e}"
            ) from e
        except LLMPersistenceError:
            raise
        except Exception as e:  # 网络抖动/超时等 → 重试
            if _is_conn_error(e):
                _discard_client(provider)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {e}") from e


# ---------------------------------------------------------------------------
# 结构化输出加固层：三层递进解析 + 失败三分类重试。
# 模型返回的 JSON 时好时坏（空内容、夹着思考块、括号不配对），与其每个调用点各自兜底，
# 不如收成一层：先剥掉思考块、按括号配对扫描提取 JSON，再区分“空”和“格式坏”分别重试。
# ---------------------------------------------------------------------------

_THINK_BLOCK_RE = re.compile(r"<(think|thinking|reasoning)>.*?</\1>",
                             re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(raw: str) -> dict | list | None:
    """三层递进：① 剥 <think> 块后整体 loads；② 取代码围栏内内容（从后往前试）；
    ③ 平衡括号扫描——从最右往左找 '{'/'['，配平后尝试解析（答案通常在推理文本之后）。
    全部失败返回 None（不是 {}——让调用方能区分"解析失败"与"合法空对象"）。"""
    import json as _json
    if not raw or not raw.strip():
        return None
    text = _THINK_BLOCK_RE.sub("", raw).strip()
    try:
        return _json.loads(text)
    except _json.JSONDecodeError:
        pass
    for m in reversed(_FENCE_RE.findall(text)):
        try:
            return _json.loads(m.strip())
        except _json.JSONDecodeError:
            continue
    for opener, closer in (("{", "}"), ("[", "]")):
        starts = [i for i, ch in enumerate(text) if ch == opener][-8:]  # 至多试 8 个起点
        for start in reversed(starts):
            depth = 0
            for j in range(start, len(text)):
                if text[j] == opener:
                    depth += 1
                elif text[j] == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return _json.loads(text[start:j + 1])
                        except _json.JSONDecodeError:
                            break
    return None


def call_json(
    messages: list[dict],
    *,
    expect_keys: tuple[str, ...] = (),
    validate: Callable[[dict], str | None] | None = None,
    json_retries: int = 1,
    **chat_kwargs,
) -> dict:
    """chat + 解析 + 校验 + 按失败原因重试。穷尽后返回 {}（已 emit json_retry 告警事件）。

    失败三分类与重试策略：
    - empty（content 空，如 reasoning 吃光输出）→ 原样重试（新采样即修复瞬时态）；
    - parse（有内容但抠不出 JSON）→ 追加"只输出 JSON"nudge 重试；
    - schema（JSON 合法但缺 expect_keys 字段，或调用方内部结构校验失败）→ 追加具体
      错误 nudge 重试（③d 加固点）。

    ``validate`` 返回 None/空字符串表示通过，返回非空错误说明表示业务结构无效。
    它让调用方校验嵌套字段，而不必等 ``call_json`` 返回后才发现错误、错过本层重试。
    """
    from dra import events
    msgs = list(messages)
    reason, raw = "", ""
    for attempt in range(json_retries + 1):
        raw = chat(msgs, **chat_kwargs)
        missing: list[str] = []
        validation_error = ""
        if not raw.strip():
            reason = "empty"
        else:
            data = extract_json(raw)
            if not isinstance(data, dict):
                reason = "parse"
            else:
                missing = [k for k in expect_keys if k not in data]
                if missing:
                    reason = "schema"
                elif validate is not None and (validation_error := (validate(data) or "")):
                    reason = "schema"
                else:
                    return data
        if attempt < json_retries:
            events.emit(events.EventType.JSON_RETRY, reason=reason,
                        attempt=attempt + 1, raw_head=raw[:200])
            if reason in ("parse", "schema"):
                if reason == "parse":
                    nudge = "上一次输出无法解析为合法 JSON。"
                elif missing:
                    nudge = f"上一次输出缺少必需字段：{missing}。"
                else:
                    nudge = f"上一次输出的内部结构不符合要求：{validation_error}。"
                msgs = msgs + [
                    {"role": "assistant", "content": (raw[-1500:] or "(空)")},
                    {"role": "user", "content": nudge + "请只输出符合要求的 JSON，不要任何其他文字。"},
                ]
    events.emit(events.EventType.JSON_RETRY, reason=reason, attempt=json_retries + 1,
                raw_head=raw[:200], exhausted=True)
    return {}


# ---------------------------------------------------------------------------
# 原生 tool-loop 共用的 function-calling 入口。与 chat 的分工：chat 走纯文本/JSON
# 输出，call_tools 走 tools 参数返回结构化 tool_calls。
# ---------------------------------------------------------------------------


class ToolCall(BaseModel):
    """一次模型发起的工具调用请求（provider 无关的最小规约）。"""

    id: str
    name: str
    arguments_raw: str = ""
    arguments: dict | None = None
    """arguments_raw 的 JSON 解析结果；解析失败或不是对象时为 None——不抛异常，
    由 run_tool_loop 把错误信息回给模型自纠（工具契约哲学：拒收而非崩溃）。"""


class ToolTurn(BaseModel):
    """call_tools 的单轮返回：可见文本 + 工具调用列表 + 可原样回填 history 的 assistant 消息。

    assistant_message 必须原样 append 进 messages 再补 tool 结果消息——OpenAI 契约
    要求 tool 消息的 tool_call_id 能对上前一条 assistant 的 tool_calls。
    """

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    assistant_message: dict = Field(default_factory=dict)
    input_tokens: int | None = None
    output_tokens: int | None = None
    incomplete: bool = False
    """流式装配不完整（终止原因异常 / 缺 index·id·name / 回调出错 / 未知扩展字段）。
    为 True 时 **本次返回的 tool_calls 一律不得执行**——半截或伪造的参数会以错误
    参数触达外部边界。"""
    note: str = ""
    """incomplete 时的可读原因（也用于如实记录，不给模型看）。"""
    streamed: bool = False
    """本轮走了流式且收到过 content 增量。调用方据此决定是否需要补发全量文本。"""
    stopped: bool = False
    """流在收到终止标记前因**取消 / deadline** 主动断开（不是协议错误）。"""
    stop_reason: str = ""
    attempts: int = 1
    """本次预算调用**实际发出的 HTTP 尝试次数**（1 = 一次成功）。

    ``>1`` 表示发生过传输层重试（空流 / 缺终止标记 / 连接错误）。账本按一次调用记账，
    但把尝试次数如实记下来——否则"重试到底有没有触发"只能靠翻日志猜。
    """
    """stopped 为真时的原因：cancelled | deadline。"""


# 流式终止原因白名单：只有这两个表示"回复已按预期收完"。
# function_call/length/content_filter 都是**异常终止**（内容被截断或被拦下），
# 空流（一个 choice 都没有）同样没有终止标记——一律按不完整处理，不执行工具。
_STREAM_OK_FINISH = frozenset({"stop", "tool_calls"})


def _accumulate_stream_tool_calls(
    fragments: dict[int, dict],
) -> tuple[list[dict], list[str]]:
    """把流式 tool_calls 分片按 index 拼成完整调用；返回 (原始调用 dict 列表, 问题列表)。

    OpenAI 流式契约（已用真实 OpenCode Go / deepseek-v4-pro 探测核对）：
    ``choices[].delta.tool_calls[]`` 逐片到达，``index`` 标识第几个调用；首片带
    ``id``/``function.name``，``function.arguments`` 是**跨片拼接的 JSON 片段**——
    必须全部拼完再解析，任何中途截断都不能当完整参数执行。

    缺 ``index`` / ``id`` / ``name`` 一律记为协议问题：**不猜 index、不补造 id**。
    伪造调用身份会让工具回执的 tool_call_id 对不上 provider 的历史，把协议错误
    掩盖成一次"正常"调用。
    """
    calls: list[dict] = []
    problems: list[str] = []
    for index in sorted(fragments):
        frag = fragments[index]
        name = frag.get("name") or ""
        args_raw = "".join(frag.get("arguments") or [])
        if not frag.get("id"):
            problems.append(f"tool_calls[{index}] 缺少 id")
            continue
        if not name:
            problems.append(f"tool_calls[{index}] 缺少 function.name")
            continue
        calls.append({
            "id": frag["id"],
            "type": frag.get("type") or "function",
            "function": {"name": name, "arguments": args_raw},
        })
    return calls, problems


def call_tools(
    messages: list[dict],
    *,
    tools: list[dict],
    model: str = DEFAULT_MODEL,
    provider: str = "openai",
    temperature: float | None = None,
    max_tokens: int | None = 2048,
    max_retries: int = DEFAULT_MAX_RETRIES,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    reasoning: bool = True,
    effort: str | None = None,
    on_chunk: Callable[[str], None] | None = None,
    on_reasoning: Callable[[str, str], None] | None = None,
    stream_usage: bool | None = None,
) -> ToolTurn:
    """发一轮带 tools 的对话，返回 ToolTurn。

    重试/4xx/timeout 语义与 chat 一致（_NO_RETRY_STATUS 立即上抛、429/5xx
    指数退避；每次请求最多等 request_timeout_s，并受 run deadline 截断）。
    reasoning / effort 语义与 chat 一致（见 _reasoning_extra_body）：
    effort 优先；opencode 关推理用 thinking:disabled；codex521/codex_local 关推理用
    reasoning_effort:none；其它 provider 不乱传。

    on_chunk（流式，非侵入）：给定时 ``stream=True``，每收到一段**普通 assistant
    content** 增量就调 ``on_chunk(片段)``；返回值与非流式完全一致（content 为拼好的
    全文），不传它的一切现有调用方零变化。reasoning_content 只累计进
    assistant_message 供协议回填；可读字符串另经 on_reasoning(field, text) 记录，不混入公开说明。
    回调抛错被吞（展示层不能弄崩主流程）。

    工具调用在流式下按 OpenAI 契约分片装配：``delta.tool_calls[].index`` 标识调用，
    ``function.arguments`` 是跨片拼接的 JSON 片段——**全部拼完才解析**，保留 provider
    原生 id / name / type 回填 history。若流没有正常收尾（缺 finish_reason）或某个调用
    缺 name，则 ``ToolTurn.incomplete=True``，``tool_calls`` 一律不得执行——宁可如实
    报协议错误，也不拿半截参数去触达外部边界。

    其中「空流 / 缺终止标记」属于传输层中断，会按 ``max_retries`` 重试（换连接重发），
    重试用尽才把未完成的 ``ToolTurn`` 交回上层。显式异常终止（``length`` /
    ``content_filter``）与结构缺陷（缺 name/index）不重试，直接标不完整。

    stream_usage 控制是否要求 ``stream_options={"include_usage": True}``：
    ``None``（默认）按 provider 自动判定——只有已核实支持的通道（OpenCode，见
    ``_stream_usage_supported``）才开，避免给不认该字段的兼容端点乱传导致 400；
    显式 True/False 覆盖判定。usage 缺失时如实置 None，由调用方按 usage_unknown 记账。
    """
    client = _get_client(provider)
    requested_temperature = temperature
    if (provider, model) in _THINKING_ONLY:
        reasoning = True
    temperature = _default_temperature(temperature, reasoning=reasoning, effort=effort)
    kwargs: dict = dict(model=model, messages=messages, temperature=temperature, tools=tools)
    if max_tokens is not None:
        # 与 chat 同语义：None = 不设输出上限，字段整个不发。
        kwargs["max_tokens"] = max_tokens
    kwargs.update(_provider_headers(provider))
    extra = _reasoning_extra_body(provider=provider, reasoning=reasoning, effort=effort)
    if extra is not None:
        kwargs["extra_body"] = extra
    if on_chunk is not None:
        kwargs["stream"] = True
        want_usage = (
            _stream_usage_supported(provider, model) if stream_usage is None else stream_usage
        )
        if want_usage:
            kwargs["stream_options"] = {"include_usage": True}
    _t0 = time.monotonic()
    for attempt in range(max_retries + 1):
        try:
            # 与 chat 同因同治：连接级故障会 _discard_client()，这里重取拿新连接池，
            # 否则重试复用断掉的 keep-alive 连接、原地再挂。
            client = _get_client(provider)
            request_kwargs = dict(kwargs)
            request_kwargs["timeout"] = _effective_request_timeout(request_timeout_s)
            if on_chunk is not None:
                turn = _stream_tool_turn(
                    client, request_kwargs, messages=messages, model=model,
                    provider=provider, on_chunk=on_chunk, on_reasoning=on_reasoning, t0=_t0,
                    attempt=attempt + 1,
                )
                turn.attempts = attempt + 1
                return turn
            resp = _require_chat_completion(
                _observed_create(client, request_kwargs, provider=provider, model=model, attempt=attempt + 1),
                provider=provider,
                model=model,
            )
            usage = getattr(resp, "usage", None)
            _record_usage(usage)
            msg = resp.choices[0].message
            raw_tcs = msg.tool_calls or []
            calls: list[ToolCall] = []
            incomplete = False
            for tc in raw_tcs:
                raw_args = tc.function.arguments or ""
                try:
                    parsed = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                name = getattr(tc.function, "name", None) or ""
                if not name:
                    incomplete = True
                calls.append(ToolCall(
                    id=tc.id or "", name=name, arguments_raw=raw_args,
                    arguments=parsed if isinstance(parsed, dict) else None))
            assistant_message: dict = {"role": "assistant", "content": msg.content or ""}
            # thinking tool-call 协议要求保留这些 provider 扩展；不要重新生成或截断。
            for field in ("reasoning_content", "reasoning", "reasoning_details"):
                value = getattr(msg, field, None)
                if value is not None:
                    assistant_message[field] = value
            if raw_tcs:
                assistant_message["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments or ""}}
                    for tc in raw_tcs]
            content = msg.content or ""
            tool_trace = "".join(
                "\n[tool_call] "
                f"{c.name}({c.arguments_raw if _trace_full_io.get() else c.arguments_raw[:400]})"
                for c in calls
            )
            _trace_llm_call(
                messages,
                content + tool_trace,
                _t0, model, usage, tools=tools)
            turn = ToolTurn(content=content, tool_calls=calls,
                            assistant_message=assistant_message,
                            input_tokens=getattr(usage, "prompt_tokens", None),
                            output_tokens=getattr(usage, "completion_tokens", None),
                            incomplete=incomplete,
                            note="有工具调用缺少 function.name" if incomplete else "")
            turn.attempts = attempt + 1
            return turn
        except LLMDeadlineExceeded:
            raise
        except (LLMProtocolError, LLMPersistenceError):
            raise
        except LLMStreamIncomplete as e:
            # 空流 / 缺终止标记：换一条连接重发（复用可疑连接只会原地再挂），
            # 重试用尽才把未完成的 turn 交回上层——上层按协议错误收尾，不执行半截工具。
            if attempt < max_retries:
                _discard_client(provider)
                time.sleep(2 ** attempt)
                continue
            e.turn.attempts = attempt + 1
            return e.turn
        except (APITimeoutError, httpx.TimeoutException) as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise LLMRequestTimeout(
                f"LLM 请求超时（单次上限 {request_timeout_s:g}s，已重试 {max_retries} 次）"
            ) from e
        except APIStatusError as e:
            if not reasoning and _rejects_disabling_thinking(e):
                _THINKING_ONLY.add((provider, model))
                return call_tools(messages, tools=tools, model=model, provider=provider,
                                  temperature=requested_temperature, max_tokens=max_tokens, max_retries=max_retries,
                                  request_timeout_s=request_timeout_s, reasoning=True, effort=effort,
                                  on_chunk=on_chunk, on_reasoning=on_reasoning, stream_usage=stream_usage)
            if e.status_code in _NO_RETRY_STATUS:
                raise RuntimeError(
                    f"LLM 调用失败（HTTP {e.status_code}，4xx 不重试）: {e}") from e
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(
                f"LLM 调用失败（HTTP {e.status_code}，已重试 {max_retries} 次）: {e}") from e
        except Exception as e:
            if _is_conn_error(e):
                _discard_client(provider)
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {e}") from e


# reasoning_details 的字符串字段：同一 index 的跨 chunk 分片按字段累加，其余字段后到者覆盖。
_REASONING_TEXT_FIELDS = ("text", "data", "summary", "signature")


def extend_reasoning_details(target: list, blocks) -> None:
    """把一次 delta 里的 reasoning_details 块**按原协议平铺**进目标序列。

    协议依据（OpenRouter reasoning-tokens 文档）：
    ``delta.reasoning_details`` 是**对象数组**，每块是 reasoning.summary /
    reasoning.text / reasoning.encrypted 之一，带可选 ``index``（Sequential index of
    the reasoning detail）；"complete reasoning sequence is built by concatenating all
    chunks in order"。

    因此：每块是一个**独立元素**，跨 chunk 只能追加成**一层**数组——把
    ``[block0]``、``[block1]`` 拼成 ``[[block0],[block1]]`` 会多一层嵌套，回填时
    不再是协议要求的对象序列。同一 ``index`` 的分片按字符串字段累加合并；
    index/type 对不上的**绝不猜**，按原样各自成块（序列保持完整，语义不被重写）。

    非对象块无法安全回填（未知结构不能静默重写后继续调用）：不计入序列，
    返回问题说明给调用方标 incomplete。
    """
    problems: list[str] = []
    if isinstance(blocks, dict):
        blocks = [blocks]
    if not isinstance(blocks, (list, tuple)):
        return [f"reasoning_details 结构无法识别：{type(blocks).__name__}"]
    for block in blocks:
        if not isinstance(block, dict):
            problems.append(f"reasoning_details 块不是对象：{type(block).__name__}")
            continue
        index = block.get("index")
        if index is None or isinstance(index, bool) or not isinstance(index, int):
            target.append(_plain(block))
            continue
        merged = False
        for existing in target:
            if (
                isinstance(existing, dict)
                and existing.get("index") == index
                and existing.get("type") == block.get("type")
            ):
                for key, value in block.items():
                    if key in _REASONING_TEXT_FIELDS and isinstance(value, str) \
                            and isinstance(existing.get(key), str):
                        existing[key] = existing[key] + value
                    else:
                        existing[key] = _plain(value)
                merged = True
                break
        if not merged:
            target.append(_plain(block))
    return problems


def _reasoning_shape(blocks, *, max_types: int = 3) -> str:
    """reasoning_details 的**结构**摘要：字段名 / 块数 / 块类型，绝不含思考正文。

    这份摘要是给公开错误事件和诊断记录看的：隐藏推理正文不得因为它们进入普通 UI
    （调试档也只在 ``_trace_full_io`` 下另发 llm_call，见 events.py 契约）。
    """
    if not isinstance(blocks, list):
        return f"结构={type(blocks).__name__}"
    types: list[str] = []
    for block in blocks:
        name = block.get("type") if isinstance(block, dict) else type(block).__name__
        if isinstance(name, str) and name not in types:
            types.append(name)
    shown = ", ".join(types[:max_types]) or "无 type"
    return f"块数={len(blocks)}，类型=[{shown}]"


def _plain(value):
    """把 SDK 对象转成纯 JSON 类型（dict/list/str/数值），无法转换则退到字符串。

    用于保留 provider 扩展字段的原始结构；这里只做类型规整，不改内容语义。
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _plain(dump())
        except Exception:  # noqa: BLE001
            pass
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


def _stream_tool_turn(
    client,
    request_kwargs: dict,
    *,
    messages: list[dict],
    model: str,
    provider: str,
    on_chunk: Callable[[str], None],
    t0: float,
    attempt: int = 1,
    on_reasoning: Callable[[str, str], None] | None = None,
) -> ToolTurn:
    """流式执行一次带 tools 的调用并装配 ToolTurn（call_tools 的流式分支）。

    与 chat 的流式分支同一套降级原则：usage 拿不到就置 None（由调用方记
    usage_unknown），绝不编造；异常照常按 call_tools 的重试语义上抛。

    边界（阶段 1 加固）：
    - **终止原因**必须属于 ``_STREAM_OK_FINISH``；``length``/``content_filter``/
      ``function_call``/空流/缺终止标记都按不完整处理，工具一个都不执行。
    - **取消与 deadline 在消费过程中检查**：持续有 chunk 的长流不能靠单次 HTTP
      read timeout 兜住总墙钟；一旦取消/deadline 到达就主动断开流（``close()``
      释放连接）并如实标 ``stopped``，不当成协议错误。
    - **reasoning 扩展按字段分别保留**：``reasoning_content`` / ``reasoning`` 各自
      原样累计回填；非字符串结构（如 ``reasoning_details`` 的数组）原样保留，
      无法识别的字段记协议问题（不静默丢弃后继续调用）。
    - **增量回调抛错**不再静默吞掉：记进 note 并标不完整——展示层接不上
      不能变成"用户没看到但流程照跑"。
    """
    content_parts: list[str] = []
    reasoning_text: dict[str, list[str]] = {}
    reasoning_raw: dict[str, list] = {}
    reasoning_details: list = []  # 见 reasoning_raw 的 "reasoning_details"
    fragments: dict[int, dict] = {}
    problems: list[str] = []
    usage = None
    finish_reason: str | None = None
    saw_choice = False
    stopped: str = ""

    def _consume(chunk) -> None:
        nonlocal usage, finish_reason, saw_choice
        u = getattr(chunk, "usage", None)
        if u:
            usage = u
        choices = getattr(chunk, "choices", None)
        if not choices:
            return
        saw_choice = True
        choice = choices[0]
        reason = getattr(choice, "finish_reason", None)
        if reason:
            finish_reason = reason
        delta = getattr(choice, "delta", None)
        if delta is None:
            return
        piece = getattr(delta, "content", None)
        if piece:
            content_parts.append(piece)
            try:
                on_chunk(piece)
            except Exception as exc:  # noqa: BLE001 — 但必须如实记录，不能装作没事
                problems.append(f"增量回调失败：{type(exc).__name__}: {exc}")
        # 只显示供应商返回的纯文本；不透明结构仅原样回填协议。
        for field in ("reasoning_content", "reasoning"):
            value = getattr(delta, field, None)
            if value is None or value == "":
                continue
            if isinstance(value, str):
                reasoning_text.setdefault(field, []).append(value)
                if on_reasoning is not None:
                    try:
                        on_reasoning(field, value)
                    except Exception as exc:
                        problems.append(f"思考增量回调失败：{type(exc).__name__}: {exc}")
            else:
                # 非字符串结构：原样保留（转成纯 JSON 类型），不猜它的语义
                reasoning_raw.setdefault(field, []).append(_plain(value))
        details = getattr(delta, "reasoning_details", None)
        if details:
            reasoning_details = reasoning_raw.setdefault("reasoning_details", [])
            problems.extend(extend_reasoning_details(reasoning_details, details))
        for tc in getattr(delta, "tool_calls", None) or []:
            index = getattr(tc, "index", None)
            if not isinstance(index, int) or index < 0:
                # 不猜 index：缺索引的分片归入 -1，装配阶段会记为协议问题
                problems.append("tool_calls 分片缺少有效 index")
                index = -1
            frag = fragments.setdefault(index, {"arguments": []})
            if getattr(tc, "id", None):
                frag["id"] = tc.id
            if getattr(tc, "type", None):
                frag["type"] = tc.type
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    frag["name"] = fn.name
                if getattr(fn, "arguments", None):
                    frag["arguments"].append(fn.arguments)

    stream = _observed_create(client, request_kwargs, provider=provider, model=model, attempt=attempt)
    try:
        for chunk in stream:
            if cancellation_requested():
                stopped = "cancelled"
                break
            if _deadline_expired():
                stopped = "deadline"
                break
            _consume(chunk)
    finally:
        # 提前断开必须关掉底层连接；关闭时的收尾异常不能顶替真实停止原因。
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass

    raw_calls, call_problems = _accumulate_stream_tool_calls(fragments)
    problems.extend(call_problems)
    if stopped:
        # 取消/deadline 是上层意图，不是协议错误；半截内容一律不执行。
        problems = list(dict.fromkeys(problems))
        _record_usage(usage)
        return ToolTurn(
            content="".join(content_parts),
            tool_calls=[],
            assistant_message={},
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            incomplete=True,
            note="；".join(problems) if problems else "",
            stopped=True,
            stop_reason=stopped,
        )
    if not saw_choice:
        problems.append("流没有返回任何 choice（空流），无法确认回复内容")
    elif finish_reason is None:
        problems.append("流未给出终止标记（finish_reason），无法确认回复已收完")
    elif finish_reason not in _STREAM_OK_FINISH:
        problems.append(f"流以异常原因终止：finish_reason={finish_reason}")
    # 传输层中断（空流 / 缺终止标记）与「模型明确给了异常终止原因」要分开：
    # 前者重发一次可能就好，后者重发是浪费。
    transport_incomplete = not saw_choice or finish_reason is None

    incomplete = bool(problems)
    if incomplete:
        # 不完整就不再对外暴露可执行调用：调用方拿到空列表，不可能误执行半截参数。
        # 已拼到的片段不静默丢弃，写进 note 供如实记录（诊断用，不给模型看）。
        for raw in raw_calls:
            frag = raw["function"]["arguments"]
            shown = frag if _trace_full_io.get() else frag[:200]
            problems.append(f"未执行片段 tool_calls[{raw['id']}] {raw['function']['name']}({shown})")
        raw_calls = []
        if reasoning_details:
            # 公开错误只报结构，不带思考正文；正文仍在 assistant_message 里供协议回填。
            problems.append(
                f"reasoning_details 已按协议保留（{_reasoning_shape(reasoning_details)}）")
    _record_usage(usage)
    content = "".join(content_parts)
    tool_calls: list[ToolCall] = []
    for raw in raw_calls:
        raw_args = raw["function"]["arguments"]
        try:
            parsed = json.loads(raw_args)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        tool_calls.append(ToolCall(
            id=raw["id"], name=raw["function"]["name"], arguments_raw=raw_args,
            arguments=parsed if isinstance(parsed, dict) else None,
        ))
    assistant_message: dict = {"role": "assistant", "content": content}
    # thinking tool-call 协议要求**按字段**原样回填：不把 reasoning 与
    # reasoning_content 拼成一个字段，也不丢掉 reasoning_details。
    for field, parts in reasoning_text.items():
        assistant_message[field] = "".join(parts)
    for field, value in reasoning_raw.items():
        if field == "reasoning_details":
            # 已经是协议要求的**平坦对象序列**（见 extend_reasoning_details）
            assistant_message[field] = value
        else:
            # 其它非字符串扩展字段：单块不做多余包裹，多块保序成列表。
            # 同样只回填，不把内容写进公开诊断。
            assistant_message[field] = value[0] if len(value) == 1 else value
    if raw_calls:
        assistant_message["tool_calls"] = raw_calls
    tool_trace = "".join(
        "\n[tool_call] "
        f"{c.name}({c.arguments_raw if _trace_full_io.get() else c.arguments_raw[:400]})"
        for c in tool_calls
    )
    _trace_llm_call(messages, content + tool_trace, t0, model, usage, tools=request_kwargs.get("tools"))
    turn = ToolTurn(
        content=content,
        tool_calls=tool_calls,
        assistant_message=assistant_message,
        input_tokens=getattr(usage, "prompt_tokens", None),
        output_tokens=getattr(usage, "completion_tokens", None),
        incomplete=incomplete,
        note="；".join(dict.fromkeys(problems)),
        streamed=bool(content_parts),
    )
    if transport_incomplete and not stopped:
        raise LLMStreamIncomplete(turn)
    return turn
