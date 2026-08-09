"""LLM 客户端：薄封装 OpenAI 兼容接口（多 provider 注册表，按 provider 缓存 client + 流式 + 内容过滤 fallback）。

模型选择分入口：
- CLI 在 ``__main__.py`` 显式构造实验档；
- Web 读取本机 ``runtime_config.json``；
- Python API 使用调用方配置或 OrchestratorConfig/SubAgentConfig 代码默认。

固定辅助档只有内容过滤 fallback 与摘要模型；不要把某一入口的模型表写成
全局默认，当前核对见 STATUS.md。

用法外挂 token 计数：
  import dra.llm as llm_module
  llm_module.reset_token_usage()
  # ... 调用若干次 chat ...
  print(llm_module.get_token_usage())  # → {"input": N, "output": M}
"""

import contextvars
import json
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

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
# 默认最多等 90s，重型 writer 节点可显式给更长预算。
DEFAULT_REQUEST_TIMEOUT_S = 90.0
DEFAULT_MAX_RETRIES = 1


class LLMRequestTimeout(TimeoutError):
    """一次 LLM 请求在项目允许的等待时间内没有返回。"""


class LLMDeadlineExceeded(LLMRequestTimeout):
    """研究运行的整体 deadline 已耗尽，不能再发起或重试 LLM 请求。"""


class LLMProtocolError(RuntimeError):
    """Provider 返回的不是 OpenAI ChatCompletion 协议响应。"""


_request_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "dra_llm_request_deadline", default=None
)
_trace_full_io: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "dra_llm_trace_full_io", default=False
)


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


def _effective_request_timeout(request_timeout_s: float) -> float:
    if request_timeout_s <= 0:
        raise ValueError("request_timeout_s 必须 > 0")
    deadline = _request_deadline.get()
    if deadline is None:
        return float(request_timeout_s)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise LLMDeadlineExceeded("LLM 请求未发出：研究总 deadline 已耗尽")
    return min(float(request_timeout_s), remaining)

# 内容过滤标记：中文模型（DeepSeek/GLM/Kimi/通义等）会对良性话题随机触发内容审核 400
# （实测 DeepSeek 对"北理工珠海校区"这种话题都拦）。命中这些标记 + 给了 fallback 时，
# 换非中文模型重写（fallback 默认走 openai provider，即 zetatechs 中转），
# 保住整个 run 不因一次过滤血本无归。
_CONTENT_FILTER_MARKERS = (
    "content exists risk", "considered high risk", "content_filter",
    "content filter", "data_inspection_failed", "内容审核", "敏感",
)


def _is_content_filter(e: Exception) -> bool:
    """从异常文本判断是否内容审核拦截（区别于 key/参数错，后者换模型也没用）。"""
    s = str(e).lower()
    return any(m in s for m in _CONTENT_FILTER_MARKERS)

# 与 tools.py 一致：显式定位项目根 .env，不依赖运行方式
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

# 兜底默认模型：仅当调用方未显式传 model 时生效。
# orchestrator 总是显式传模型，此常量只在直调 nodes.py 函数时作为最后回退。
# 当前模型入口以 STATUS.md §3 为准。
DEFAULT_MODEL = "deepseek-v3.2-251201"

# 多 provider 注册表：provider 名 → (key 环境变量名, base_url 环境变量名)。
# 同一套 OpenAI 兼容 SDK，靠不同 key + base_url 直连不同厂商。
# 加新 provider 只需在这里加一行 + .env 配好对应变量。
_PROVIDERS: dict[str, tuple[str, str]] = {
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),        # 默认：zetatechs 中转
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),  # 直连 deepseek 备份（opencode 失败时的 fallback / 调试用）
    "opencode": ("OPENCODE_API_KEY", "OPENCODE_BASE_URL"),  # opencode-go 套餐：OpenAI 兼容端点
    "codex_local": ("CODEX_LOCAL_API_KEY", "CODEX_LOCAL_BASE_URL"),  # 本机自建代理（用户自搭，暴露 gpt-5.5 等；仅本机测评用）
    "codex521": ("CODEX521_API_KEY", "CODEX521_BASE_URL"),  # 用户自建 codex 中转（暴露 gpt-5.5 等；元组约定 (KEY, BASE_URL)）
    "kimi": ("KIMI_API_KEY", "KIMI_BASE_URL"),
}

# 按 provider 缓存 client（惰性）。单例升级为字典，支持多厂商共存。
_clients: dict[str, OpenAI] = {}

# token 计数器：累计所有 chat() 调用的 token 消耗
_token_usage: dict[str, int] = {"input": 0, "output": 0}


def reset_token_usage() -> None:
    """清空累计的 token 计数（每轮 run 开始时调一次）。"""
    _token_usage["input"] = 0
    _token_usage["output"] = 0


def get_token_usage() -> dict[str, int]:
    """返回累计 token 消耗（input / output）。"""
    return dict(_token_usage)


def _get_client(provider: str = "openai") -> OpenAI:
    """惰性初始化并缓存指定 provider 的 OpenAI 兼容客户端。

    base_url 为空走官方 OpenAI；未知 provider 直接报错（防拼写错默默走错厂商）。
    """
    if provider not in _clients:
        if provider not in _PROVIDERS:
            raise RuntimeError(
                f"未知 provider: {provider!r}，可选 {list(_PROVIDERS)}"
            )
        # override=True：本项目以 .env 为准，避免被 shell（如 ~/.zshrc 的
        # 历史 export）里同名变量污染。dotenv 默认 override=False，是常见坑。
        load_dotenv(_ENV_PATH, override=True)
        key_name, url_name = _PROVIDERS[provider]
        # .strip().strip('"') 兜底：.env 里有的值写成 KEY = "v"，带空格 / 引号。
        key = (os.environ.get(key_name) or "").strip().strip('"').strip()
        base_url = (os.environ.get(url_name) or "").strip().strip('"').strip() or None
        if not key:
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


def check_provider(provider: str) -> ProviderStatus:
    """key 未配置直接短路，不发请求；key 配了才真调 models.list() 验证连通性，
    异常在这里兜住转 reachable=False，不上抛。

    显式 load_dotenv：本函数可能在 _get_client 之前被调用（比如 GET /api/providers
    是进程里第一次涉及 provider 的请求），.env 这时还没读进 os.environ，会误判所有
    provider 都没配 key（已用真实场景实测踩过：新进程直接调 check_provider 拿到
    key_configured=False，load_dotenv 后同一调用变 True）。_get_client 内部也会
    load_dotenv，但那是本函数走到"有 key"分支之后的事，救不了这里的提前读取。"""
    load_dotenv(_ENV_PATH, override=True)
    key_name, _ = _PROVIDERS[provider]
    key = (os.environ.get(key_name) or "").strip().strip('"').strip()
    if not key:
        return ProviderStatus(name=provider, key_configured=False, reachable=False)
    try:
        client = _get_client(provider)
        resp = client.models.list()
        return ProviderStatus(
            name=provider, key_configured=True, reachable=True,
            models=[m.id for m in resp.data],
        )
    except Exception as e:
        return ProviderStatus(
            name=provider, key_configured=True, reachable=False, error=str(e)[:200],
        )


_provider_cache: list[ProviderStatus] | None = None


def list_provider_status(*, force_refresh: bool = False) -> list[ProviderStatus]:
    """遍历全部 provider；结果缓存，force_refresh 才重新探测。单个 provider 的异常
    已在 check_provider 内部兜住，这里不需要再 try/except。

    force_refresh 额外清 _clients（客户端缓存）：改了已存在的 key/base_url 后，旧 client
    已被 _get_client 缓存着旧值，不清就一直复用旧 key、refresh 也白搭——清掉让下次
    _get_client 按 .env 现值重建（配合设置面板「刷新」按钮，改 key 免重启即可生效）。"""
    global _provider_cache
    if force_refresh or _provider_cache is None:
        if force_refresh:
            _clients.clear()
        _provider_cache = [check_provider(p) for p in _PROVIDERS]
    return _provider_cache


# ---------------------------------------------------------------------------
# trace 插桩：实时可视化要看的 LLM 节点（input/output/耗时）。chat 是所有 call 的唯一咽喉，
# 在此一处插桩；按 obs 上下文（timing.get_ctx 的 step 名）前缀过滤——只对这些节点 emit
# llm_call，其余节点（如 condense 走 ThreadPool 不传 contextvar / format_gate 等纯函数）只计时不抓 io。
# 加节点只需扩这个元组。input 截头尾（常含整篇检索文档上万字）、output 全量留（看模型判断全貌）。
# ---------------------------------------------------------------------------
# 编排级（build_research_plan/build_report_plan/cross_worker_audit/write_report）也接：
# 都是主线程 / 串行调用，
# contextvars 正常传播。condense 经 copy_context 传播（见 nodes.condense_docs），一并追踪。
_TRACE_STEP_PREFIXES = ("cross_worker_audit", "write_report",
                        "build_research_plan", "revise_research_plan",
                        "assess_research_nodes",
                        "resolve_decisions",
                        "compile_ready_tasks", "build_report_plan",
                        "condense", "summarize", "tool_loop")
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

    _, base_url_env = _PROVIDERS[provider]
    if isinstance(resp, str):
        stripped = resp.lstrip().lower()
        response_kind = "HTML 页面" if stripped.startswith(("<!doctype html", "<html")) else "文本"
    else:
        response_kind = type(resp).__name__
    raise LLMProtocolError(
        f"provider={provider!r} model={model!r} 返回了 {response_kind}，"
        "不是 OpenAI ChatCompletion JSON；"
        f"请检查 {base_url_env} 是否指向正确的 OpenAI-compatible API 根路径"
        "（通常以 /v1 结尾）"
    )


def _trace_llm_call(messages: list[dict], output: str, t0: float, model: str, usage) -> None:
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
            sid=ctx.get("sid"),
            worker_iteration=ctx.get("worker_iteration"),
            model=model,
            ms=round((time.monotonic() - t0) * 1000, 1),
            input=input_text,
            output=out,
            io_complete=full_io,
            **tok,
        )
    except Exception:  # noqa: BLE001 — 展示层旁路绝不弄崩主流程
        pass


def chat(
    messages: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    provider: str = "openai",
    temperature: float = 0.3,
    max_tokens: int = 1024,
    max_retries: int = DEFAULT_MAX_RETRIES,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    response_format: dict | None = None,
    reasoning: bool = True,
    effort: str | None = None,
    fallback: tuple[str, str] | None = None,
    on_chunk: Callable[[str], None] | None = None,
) -> str:
    """发一轮对话，返回文本内容。失败重试 ≤ max_retries（指数退避）。

    每一轮 HTTP 请求都有 ``request_timeout_s`` 上限（默认 90 秒），并会自动
    截断到当前 run 的剩余 deadline。SDK 内建重试已在 client 构造时关闭，因此
    ``max_retries`` 是唯一的网络重试所有者；重型 writer 会显式设为 0，并让
    JSON 格式层只做一次语义重试，避免多层重试乘法放大。

    effort（思维强度，2026-07-06）：非 None 时 extra_body 发 {"reasoning_effort": effort}，
    provider 无关透传——zeta 用模型名后缀（gpt-5.5-high）不需要它；换别家 OpenAI 兼容
    中转时用它设档。值不做白名单（各家档位表不同：low/medium/high/xhigh/none…），
    端点自己校验。与 reasoning=False 语义互斥（effort=开思考并定档）：本层 effort
    优先、绝不同时发关闭参数（thinking:disabled 或 reasoning_effort:none）；档位 config
    的校验器另有 fail-loud。
    实测（2026-07-06）：端点**严格校验取值**——deepseek@opencode 认合法档（high 不报错，
    不代表内部真分档）、乱值（如 "banana"）直接 400。故风险是档位名拼错/端点档位表
    与预期不符 → 400 崩当次调用，不是「模型不支持就静默忽略」；换中转前先确认合法档表。

    on_chunk（流式，非侵入）：给定时走 stream=True，每收到一个增量 token 就调 on_chunk(片段)，
    同时累计并最终返回完整字符串——**返回值与非流式完全一致，所有现有调用方不传它即零变化**。
    回调里抛错会被吞（流式只是展示层，绝不能弄崩主流程）。流式分支不强制 stream_options，
    部分中转不回传 usage 时该次调用的 token 不计入计数（诚实降级，换便宜健壮）。

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
    kwargs: dict = dict(
        model=model, messages=messages, temperature=temperature, max_tokens=max_tokens
    )
    # qwen/gemini 过 zetatechs 中转时不兼容 response_format，传了返回空字符。
    # 检查 model 前缀跳过；deepseek 系不受影响（直接透传）。
    if response_format is not None and not model.startswith(("qwen", "gemini")):
        kwargs["response_format"] = response_format
        # OpenAI/Codex 后端硬约束：用 json_object 时 **input(user) message** 里必须字面出现
        # "json"，否则 400（错误原文强调 "input messages"——system 里有 json 不算数）。
        # zetatechs/opencode 中转不强制此条，故现有 prompt 一直没踩到；真 Codex 后端更严。
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
    if on_chunk is not None:
        kwargs["stream"] = True
    _t0 = time.monotonic()  # trace：本次 chat 起点（成功时算 call 墙钟耗时）
    for attempt in range(max_retries + 1):
        try:
            # 每次重试重新计算：退避时间也计入总 deadline，不能让第二次请求越界。
            request_kwargs = dict(kwargs)
            request_kwargs["timeout"] = _effective_request_timeout(request_timeout_s)
            if on_chunk is not None:
                # 流式分支：逐 chunk 回调 + 累计，返回完整内容（与非流式一致）
                parts: list[str] = []
                usage = None
                for chunk in client.chat.completions.create(**request_kwargs):
                    u = getattr(chunk, "usage", None)
                    if u:
                        usage = u
                    choices = getattr(chunk, "choices", None)
                    if not choices:
                        continue
                    piece = getattr(choices[0].delta, "content", None) or ""
                    if piece:
                        parts.append(piece)
                        try:
                            on_chunk(piece)
                        except Exception:  # noqa: BLE001 — 展示层回调绝不能弄崩主流程
                            pass
                if usage:
                    _token_usage["input"] += usage.prompt_tokens or 0
                    _token_usage["output"] += usage.completion_tokens or 0
                _text = "".join(parts)
                _trace_llm_call(messages, _text, _t0, model, usage)
                return _text
            resp = _require_chat_completion(
                client.chat.completions.create(**request_kwargs),
                provider=provider,
                model=model,
            )
            usage = getattr(resp, "usage", None)
            if usage:
                _token_usage["input"] += usage.prompt_tokens or 0
                _token_usage["output"] += usage.completion_tokens or 0
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
            # 4xx 客户端错误（key/参数/权限错）立刻上抛——再试也错
            if e.status_code in _NO_RETRY_STATUS:
                # 内容审核拦截 + 有 fallback → 换非中文模型重写一次（关掉 fallback 防递归）
                # effort 不随行：逃生通道固定走 zeta 后缀模型（gpt-5-mini-minimal 自带档位），
                # 参数保守优先——fallback 的使命是别崩 run，不是复刻原调用的调优。
                if fallback is not None and _is_content_filter(e):
                    fb_provider, fb_model = fallback
                    return chat(
                        messages, model=fb_model, provider=fb_provider,
                        temperature=temperature, max_tokens=max_tokens,
                        max_retries=max_retries, response_format=response_format,
                        reasoning=True, fallback=None, on_chunk=on_chunk,
                        request_timeout_s=request_timeout_s,
                    )
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
        except Exception as e:  # 网络抖动/超时等 → 重试
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {e}") from e


# ---------------------------------------------------------------------------
# 结构化输出加固层（Task 2）：三层递进解析 + 失败三分类重试。
# 治根因：摘要伪 0 / gpt-5.5 空 content / 审计静默空 dict 是同一类故障
# （LLM JSON 抖动），此前各调用点各自兜底（_parse_json 返 {} 无告警）——收成一层。
# 思路借 lunon extract_json（剥 think 块+平衡括号扫描）与 aiq EmptyResponseRetry
# （区分"空"与"格式坏"分别重试），实现独立（见 DEVLOG）。
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


def call_tools(
    messages: list[dict],
    *,
    tools: list[dict],
    model: str = DEFAULT_MODEL,
    provider: str = "openai",
    temperature: float = 0.3,
    max_tokens: int = 2048,
    max_retries: int = DEFAULT_MAX_RETRIES,
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    reasoning: bool = True,
    effort: str | None = None,
) -> ToolTurn:
    """发一轮带 tools 的对话，返回 ToolTurn。

    重试/4xx/timeout 语义与 chat 一致（_NO_RETRY_STATUS 立即上抛、429/5xx
    指数退避；每次请求最多等 request_timeout_s，并受 run deadline 截断）。
    不做内容过滤 fallback：loop 的 history 含 tool_calls 结构，中途换模型重写
    语义不成立——由上层 run_tool_loop 按「异常降级带部分证据返回」兜底。
    reasoning / effort 语义与 chat 一致（见 _reasoning_extra_body）：
    effort 优先；opencode 关推理用 thinking:disabled；codex521/codex_local 关推理用
    reasoning_effort:none；其它 provider 不乱传。
    """
    client = _get_client(provider)
    kwargs: dict = dict(model=model, messages=messages, temperature=temperature,
                        max_tokens=max_tokens, tools=tools)
    extra = _reasoning_extra_body(provider=provider, reasoning=reasoning, effort=effort)
    if extra is not None:
        kwargs["extra_body"] = extra
    _t0 = time.monotonic()
    for attempt in range(max_retries + 1):
        try:
            request_kwargs = dict(kwargs)
            request_kwargs["timeout"] = _effective_request_timeout(request_timeout_s)
            resp = _require_chat_completion(
                client.chat.completions.create(**request_kwargs),
                provider=provider,
                model=model,
            )
            usage = getattr(resp, "usage", None)
            if usage:
                _token_usage["input"] += usage.prompt_tokens or 0
                _token_usage["output"] += usage.completion_tokens or 0
            msg = resp.choices[0].message
            raw_tcs = msg.tool_calls or []
            calls: list[ToolCall] = []
            for tc in raw_tcs:
                raw_args = tc.function.arguments or ""
                try:
                    parsed = json.loads(raw_args)
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                calls.append(ToolCall(
                    id=tc.id, name=tc.function.name, arguments_raw=raw_args,
                    arguments=parsed if isinstance(parsed, dict) else None))
            assistant_message: dict = {"role": "assistant", "content": msg.content or ""}
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
                _t0, model, usage)
            return ToolTurn(content=content, tool_calls=calls,
                            assistant_message=assistant_message)
        except LLMDeadlineExceeded:
            raise
        except LLMProtocolError:
            raise
        except (APITimeoutError, httpx.TimeoutException) as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise LLMRequestTimeout(
                f"LLM 请求超时（单次上限 {request_timeout_s:g}s，已重试 {max_retries} 次）"
            ) from e
        except APIStatusError as e:
            if e.status_code in _NO_RETRY_STATUS:
                raise RuntimeError(
                    f"LLM 调用失败（HTTP {e.status_code}，4xx 不重试）: {e}") from e
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(
                f"LLM 调用失败（HTTP {e.status_code}，已重试 {max_retries} 次）: {e}") from e
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）: {e}") from e
