"""Web 检索工具层：Tavily、Exa 与 DuckDuckGo fallback。

工具将外部检索结果映射为 RetrievedDoc，供研究工具读取和存证。
上传资料库的本地检索由 library.py 实现。
"""

import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from tavily import TavilyClient
from tavily.errors import BadRequestError, ForbiddenError, InvalidAPIKeyError, MissingAPIKeyError

from dra.models import RetrievedDoc

# 显式定位项目根的 .env（src/dra/tools.py → parents[2] 即项目根），
# 不依赖运行方式（脚本 / pytest / stdin 都能稳定加载）。
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

_client: TavilyClient | None = None


def _get_client() -> TavilyClient:
    """惰性初始化 Tavily client。

    双模式：
    - 直连（默认）：TAVILY_API_KEY → https://api.tavily.com（SDK 默认）
    - 代理：配 TAVILY_BASE_URL（如 xuncv 自建多 key 池 http://localhost:8088）
      认证用 TAVILY_MASTER_KEY；未配 MASTER_KEY 回退 TAVILY_API_KEY 便于调试。

    未配 BASE_URL 行为字节级与老路一致（不传 api_base_url，走 SDK 默认值）。
    """
    global _client
    if _client is None:
        # override=True：以 .env 为准，避免 shell 同名变量污染（详见 llm.py）
        load_dotenv(_ENV_PATH, override=True)
        base_url = os.environ.get("TAVILY_BASE_URL")  # 配了即走代理
        key = (
            os.environ.get("TAVILY_MASTER_KEY") if base_url else None
        ) or os.environ.get("TAVILY_API_KEY")
        if not key:
            raise RuntimeError(
                "缺少 TAVILY_API_KEY（代理模式下也可用 TAVILY_MASTER_KEY），请在项目根 .env 中配置"
            )
        kwargs: dict = {"api_key": key}
        if base_url:
            kwargs["api_base_url"] = base_url
        _client = TavilyClient(**kwargs)
    return _client


def _reset_client() -> None:
    """丢弃当前 Tavily client（连同其底层 requests session / 连接池）。

    根治 SSL EOF：`TavilyClient` 是单例、底层 requests session 的连接池会被重试复用；
    一旦某条 keep-alive 连接被服务端/中途断开（表现为 SSLEOFError UNEXPECTED_EOF），
    复用它的每次重试都会原地再挂——这正是「连试 N 次全是同一个 SSL EOF」的根因。
    在连接级故障后重置，强制下次 `_get_client()` 重新握手、建全新连接，而非死磕断连。
    """
    global _client
    _client = None


# 连接级故障：这类异常重试无意义除非换一条新连接，故重试前必须 _reset_client()。
# 用类名匹配而非 isinstance，避免在工具层硬依赖 requests/urllib3 的异常类型。
_CONN_ERROR_MARKERS = (
    "SSLError", "SSLEOFError", "UNEXPECTED_EOF", "ConnectionError",
    "ConnectionResetError", "RemoteDisconnected", "Max retries exceeded",
    "Connection aborted", "EOF occurred",
)


def _is_conn_error(e: Exception) -> bool:
    """粗判是否连接级故障（据异常链文本），决定要不要重置连接重试。"""
    text = f"{type(e).__name__}: {e}"
    cause = e
    seen = 0
    while cause is not None and seen < 5:  # 顺异常链找根因（requests 会层层包裹）
        text += f" | {type(cause).__name__}: {cause}"
        cause = cause.__cause__ or cause.__context__
        seen += 1
    return any(m in text for m in _CONN_ERROR_MARKERS)


def web_search(
    query: str, top_k: int = 5, *, max_retries: int = 3, exclude_domains: list[str] | None = None
) -> list[RetrievedDoc]:
    """联网检索，返回粗排后的文档列表（已映射为 RetrievedDoc）。

    暂时失败重试 ≤ max_retries 次（指数退避 1s/2s/4s）；参数、认证与缺配置直接失败。
    失败抛 RuntimeError
    交由上层处理。

    注：Tavily 偶发 SSL EOF（疑与 include_raw_content 大 payload + 并发有关），
    频发时会让一整题拿不到证据，所以默认重试 3 次；另有 DuckDuckGo 兜底源（见 search_fallback）。
    """
    resp = None
    for attempt in range(max_retries + 1):
        # 缺配置无需重试；连接级故障后下一轮仍会取得全新 client。
        client = _get_client()
        try:
            resp = client.search(
                query=query,
                max_results=top_k,
                search_depth="basic",
                # A-1 读取层（叉 a）：让 Tavily 回填整页正文 raw_content，不止 snippet。
                # search_depth 仍用 basic（最省 1 credit）；advanced（2 credit，更狠的
                # 正文抽取）是另一档成本旋钮，效果不够再上，不进叉 a。
                include_raw_content=True,
                **({"exclude_domains": exclude_domains} if exclude_domains else {}),
            )
            break
        except Exception as e:  # 工具边界统一兜底，再决定重试/上抛
            # SSL EOF / 连接重置：重置 client，强制下一轮用全新连接重试，
            # 否则会一直复用同一条断连、连试都原地挂在同一个 SSL EOF。
            if _is_conn_error(e):
                _reset_client()
            status = e.response.status_code if isinstance(e, requests.HTTPError) and e.response is not None else None
            permanent = (
                isinstance(e, (ValueError, BadRequestError, ForbiddenError, InvalidAPIKeyError, MissingAPIKeyError))
                and not isinstance(e, requests.exceptions.JSONDecodeError)
            ) or (
                status is not None and 400 <= status < 500 and status not in (408, 429)
            )
            if not permanent and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"web_search 失败（已重试 {attempt} 次）: {e}") from e

    raw_docs = []
    for r in resp.get("results", []):
        published_at = _parse_published_at(r.get("published_date"))
        raw_docs.append(
            RetrievedDoc(
                source_url=r.get("url"),
                title=r.get("title", ""),
                snippet=r.get("content", ""),
                raw_content=r.get("raw_content"),
                score=r.get("score", 0.0),
                published_at=published_at,
            )
        )

    # P2-1：仅当 query 显式有时效诉求时才启用时间衰减重排。
    # 非时效 query（如 "什么是 RAG"）不重排，保持 Tavily 原始相关性排序。
    tau = _detect_recency_tau(query)
    if tau is not None:
        weighted = [(_combined_score(d, tau), d) for d in raw_docs]
        weighted.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in weighted]
    return raw_docs


# ---------------------------------------------------------------------------
# Exa neural search（与 Tavily 并行，由 _retrieve 合并去重）
# ---------------------------------------------------------------------------

_EXA_ENDPOINT = "https://api.exa.ai/search"
_EXA_HIGHLIGHT_MAX_CHARS = 600


def exa_search(
    query: str, top_k: int = 5, *, max_retries: int = 3, exclude_domains: list[str] | None = None
) -> list[RetrievedDoc]:
    """Exa neural search → list[RetrievedDoc]，镜像 web_search 签名。

    Exa 一次返回 URL、query highlights、全文 text 和可选 publishedDate。

    设计要点:
    - **不复用 Tavily 单例 client**:Exa 是 raw POST,无 client 概念
    - 复用 `_try_parse_date` 处理 publishedDate(Exa 实测可能返 ISO long/short/human 三种格式)
    - 暂时失败重试 ≤ max_retries 次(指数退避 1s/2s/4s)，参数 / 认证错误直接交上层
    - 不调用 Tavily 专用的 `_detect_recency_tau`；两类源的日期覆盖和排序分数不可直接混用
    """
    load_dotenv(_ENV_PATH, override=True)
    key = os.environ.get("EXA_API_KEY")
    if not key:
        raise RuntimeError("缺少 EXA_API_KEY，请在项目根 .env 中配置")

    resp = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.post(
                _EXA_ENDPOINT,
                headers={"x-api-key": key, "Content-Type": "application/json"},
                json={
                    "query": query,
                    "numResults": top_k,
                    # text 留作 fetch_page/逐字门的全文；highlights 是 Exa 按当前
                    # query 抽取的原文片段，专供 search 列表判断是否值得精读。
                    # 不能再用全文开头冒充 snippet：正文开头常是背景或模板内容。
                    "contents": {
                        "text": True,
                        "highlights": {
                            "query": query,
                            "maxCharacters": _EXA_HIGHLIGHT_MAX_CHARS,
                        },
                    },
                    **({"excludeDomains": exclude_domains} if exclude_domains else {}),
                },
                timeout=30,
            )
            if r.status_code != 200:
                raise requests.HTTPError(f"exa_search HTTP {r.status_code}: {r.text[:200]}", response=r)
            resp = r.json()
            break
        except Exception as e:
            status = e.response.status_code if isinstance(e, requests.HTTPError) and e.response is not None else None
            permanent = status is not None and 400 <= status < 500 and status not in (408, 429)
            if not permanent and attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"exa_search 失败（已重试 {attempt} 次）: {e}") from e

    raw_docs = []
    for item in (resp or {}).get("results", []):
        raw_text = item.get("text")
        text = raw_text if isinstance(raw_text, str) else ""
        raw_highlights = item.get("highlights")
        highlights = raw_highlights if isinstance(raw_highlights, list) else []
        highlight_text = "\n".join(
            value.strip()
            for value in highlights
            if isinstance(value, str) and value.strip()
        )
        # publishedDate 可能是 ISO long / ISO short / "Mar 15, 2024" 等多种格式
        # 复用 _try_parse_date 统一容错;解析不出 → published_at=None(_time_weight 中性处理)
        published_at = _try_parse_date(item.get("publishedDate") or "")
        raw_docs.append(
            RetrievedDoc(
                source_url=item.get("url"),
                title=item.get("title") or "",
                # highlights 是 query-related extractive content；旧实现取 text[:300]
                # 会把正文开头误当相关摘要。老响应没有 highlights 时有界回退全文头部。
                snippet=(highlight_text or text)[:_EXA_HIGHLIGHT_MAX_CHARS],
                raw_content=text,  # 全文,grounding 门核验依赖
                score=float(item.get("score") or 0.0),
                published_at=published_at,
            )
        )
    return raw_docs


# ---------------------------------------------------------------------------
# 按 URL 取正文（用户直接给链接时的读取入口，不经过搜索）
# ---------------------------------------------------------------------------

# 单次 extract 的 HTTP 上限：这是**单次请求**上限，run 剩余墙钟由调用方再截断。
# 检索请求一般 5-15 秒，30 秒留足余量；Tavily 文档下限是 1 秒，低于它不发请求。
EXTRACT_TIMEOUT_S = 30.0
EXTRACT_MIN_TIMEOUT_S = 1.0


class UrlFetchError(RuntimeError):
    """按 URL 取正文**失败**（可操作的工具级原因：超时、拒收、空正文、结构异常…）。

    刻意与"账本/落盘失败"分开：后者是 LLMPersistenceError，必须穿越工具边界向上抛，
    不能被降级成一条工具回执继续研究。
    """
_EXTRACT_DEPTH = "basic"
_EXTRACT_FORMAT = "markdown"
# 明显的内部/本机地址：用户给的公开链接不该指向这些，直接拒收而不是发请求。
_INTERNAL_HOST_SUFFIXES = (".local", ".internal", ".localhost", ".home.arpa")


def validate_public_url(url: str) -> str:
    """校验并归一化用户给的公开 HTTP(S) URL；不合法就抛 ValueError（联网前拒收）。

    只做到当前路径必要的程度：必须是 HTTP(S)、有有效主机、不带账号密码、不是明显的
    本机/内部地址。不做 DNS 解析、不做内网扫描，也不是通用 SSRF 防护平台。
    """
    from urllib.parse import urlsplit

    text = (url or "").strip()
    if not text:
        raise ValueError("url 不能为空")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError("url 含控制字符")
    if " " in text:
        raise ValueError("url 不能包含空格")
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("只支持 http/https 链接")
    if parts.username or parts.password:
        raise ValueError("url 不能带账号密码")
    host = (parts.hostname or "").strip().lower()
    if not host or "." not in host:
        raise ValueError("url 缺少有效主机名")
    if host == "localhost" or host.endswith(_INTERNAL_HOST_SUFFIXES):
        raise ValueError("url 指向本机/内部地址，拒绝读取")
    ip = None
    try:
        import ipaddress

        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    ):
        raise ValueError("url 指向本机/内部地址，拒绝读取")
    return text


def fetch_url_content(
    url: str,
    *,
    timeout_s: float = EXTRACT_TIMEOUT_S,
    deadline: float | None = None,
    include_usage: bool = True,
) -> dict:
    """按 URL 取正文（Tavily extract）。单 URL、单请求、不生成 claim、不新增 LLM 调用。

    只负责"取回这一次的正文与元信息"，落盘/编号/已读范围由调用方（直接研究内核）决定。
    失败一律转成 ``RuntimeError("取正文失败（...）")``：消息里不含 API key、代理地址或
    整份请求头（extract 本身不发请求头，异常文本只做截断）。

    ``timeout_s`` 是单次 HTTP 上限；给了 ``deadline``（run 墙钟，单调时钟）就再按剩余
    时间截断——总预算不会被一次提取拖穿。取消/到期由调用方在发请求前判定，本函数只
    负责"确定要发时"的超时收敛。

    返回 ``{"content", "url", "title", "credits", "request_id"}``；``content`` 为空字符串
    表示服务端没有给出可用正文，由调用方如实报"空正文"，不得当成成功。
    """
    target = validate_public_url(url)
    timeout = float(timeout_s)
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
    if timeout < EXTRACT_MIN_TIMEOUT_S:
        # 低于服务商文档下限的直接不发：发了也是无效请求，还白占一次工具额度。
        raise UrlFetchError("剩余时间不足：未发起正文提取")
    try:
        client = _get_client()
        payload = client.extract(
            [target],
            format=_EXTRACT_FORMAT,
            extract_depth=_EXTRACT_DEPTH,
            timeout=timeout,
            include_usage=include_usage,
        )
    except TimeoutError as exc:
        raise UrlFetchError(f"取正文超时（单次上限 {timeout:.0f}s）") from exc
    except Exception as exc:  # noqa: BLE001 — 只给稳定类别，不回传任意异常全文
        reason = _safe_error_reason(exc)
        raise UrlFetchError(f"取正文失败：{reason}") from exc

    if not isinstance(payload, dict):
        raise UrlFetchError(f"取正文失败：返回结构异常（{type(payload).__name__}）")
    usage = payload.get("usage")
    credits = usage.get("credits") if isinstance(usage, dict) else None
    request_id = payload.get("request_id") if isinstance(payload.get("request_id"), str) else None
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        failed = payload.get("failed_results")
        reason = ""
        if isinstance(failed, list) and failed and isinstance(failed[0], dict):
            reason = str(failed[0].get("error") or "")[:200]
        # 失败也要把服务商已经给出的用量线索带回调用方（由它如实记账）；
        # 只挂一次已知元信息，不新建账本。
        raise _extract_error(
            reason or "该链接没有返回可读正文", credits=credits, request_id=request_id
        )
    first = results[0] if isinstance(results[0], dict) else {}
    content = first.get("raw_content")
    if not isinstance(content, str):
        # 类型异常与 failed_results 同一处理：结构不对不代表这次请求没被计费。
        raise _extract_error(
            f"正文字段类型异常（{type(content).__name__}）",
            credits=credits, request_id=request_id,
        )
    return {
        "content": content,
        "url": str(first.get("url") or target),
        "title": str(first.get("title") or ""),
        "credits": credits,
        "request_id": request_id,
    }


def _extract_error(reason: str, *, credits=None, request_id=None, chars=None) -> UrlFetchError:
    """构造带**已知元信息**的提取失败异常（同一挂载方式，所有失败分支共用）。

    服务商可能已经计费（给出了 credits/request_id）：失败也要把线索交给调用方如实记账，
    不能因为"没拿到正文"就写成用量未知。不新建账本，只在异常上挂一次。
    """
    error = UrlFetchError(f"取正文失败：{reason}")
    error.extract_meta = {"credits": credits, "request_id": request_id, "chars": chars}
    return error


# 可安全展示的原因：都是固定文案，不含异常正文（异常正文可能带密钥、代理地址或
# 服务商内部信息）。命中哪条只看异常类名，不做文本透传。
_SAFE_ERROR_REASONS = (
    ("AuthenticationError", "服务商拒绝了凭据，请检查服务端配置"),
    ("PermissionDeniedError", "没有读取该链接的权限"),
    ("NotFoundError", "该链接不存在（404）"),
    ("RateLimitError", "服务商限流，请稍后重试"),
    ("BadRequestError", "服务商认为请求不合法"),
    ("APIConnectionError", "无法连接服务商"),
    ("ConnectionError", "网络连接失败"),
    ("SSLError", "HTTPS 连接失败"),
    ("JSONDecodeError", "服务商返回了非 JSON 响应"),
    ("ValueError", "服务商返回结构无法解析"),
)


def _safe_error_reason(exc: Exception) -> str:
    """把任意异常收敛成**稳定类别**，绝不回传异常全文。

    只看异常类名映射固定文案；同时顺异常链找最具体的类别。都不认识就报"未知错误
    （类型名）"——类型名本身是安全信息，异常文本不是。
    """
    seen: list[str] = []
    cause: BaseException | None = exc
    depth = 0
    while cause is not None and depth < 5:
        seen.append(type(cause).__name__)
        cause = cause.__cause__ or cause.__context__
        depth += 1
    for name in seen:
        for marker, text in _SAFE_ERROR_REASONS:
            if marker in name:
                return f"{text}（{name}）"
    return f"未知错误（{seen[0] if seen else type(exc).__name__}）"


# ---------------------------------------------------------------------------
# P2-1：发布日期解析 + 时间衰减权重
# ---------------------------------------------------------------------------


def _parse_published_at(tavily_date: str | None = None) -> str | None:
    """仅从 Tavily 返回的 published_date 字段解析，不使用 snippet。

    snippet 中的日期可能是事件日期、版本日期或历史日期，不是文章发布日期，
    不可靠。Tavily 元数据不可用时宁可留 None，不给错误时间标签。
    """
    if not tavily_date:
        return None
    return _try_parse_date(tavily_date)


def _try_parse_date(raw: str) -> str | None:
    """尝试多种格式解析日期字符串，成功返回 ISO 8601，失败返回 None。"""
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    # ISO 8601 / RFC 3339
    try:
        cleaned = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.isoformat()
    except (ValueError, TypeError):
        pass
    # 常见纯日期格式
    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%d %B %Y", "%B %d, %Y", "%b %d, %Y"]:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _detect_recency_tau(query: str) -> int | None:
    """根据 query 关键词自适应返回时间衰减常数 τ（天）。

    只有显式含时效诉求关键词的 query 才启用时间衰减：
    - 强时效（最新/最近/刚刚/今天）→ 7 天
    - 年时效（今年）→ 30 天

    返回 None 表示不需要时间衰减，保持原始排序。
    不硬编码年份数字（如 "2026"）："2026 年综述" 不等于只偏好 30 天。
    """
    if any(kw in query for kw in ["最新", "最近", "近期", "刚刚", "今天"]):
        return 7
    if "今年" in query:
        return 30
    return None


def _combined_score(doc: RetrievedDoc, tau: int) -> float:
    """score × time_weight，用于重排。"""
    return doc.score * _time_weight(doc.published_at, tau)


def _time_weight(published_at: str | None, tau: int) -> float:
    """时间衰减权重 exp(-Δdays / τ)。

    - 无发布日期 → 0.5（中性，不惩罚也不偏好）
    - 未来日期 → 1.0（视为最近）
    - 发布日期越近权重越接近 1.0
    """
    if published_at is None:
        return 0.5
    try:
        cleaned = published_at.replace("Z", "+00:00")
        pub_dt = datetime.fromisoformat(cleaned)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta_days = (now - pub_dt).days
    except (ValueError, TypeError):
        return 0.5

    if delta_days < 0:
        return 1.0
    return math.exp(-delta_days / tau)


def ddg_search(query: str, top_k: int = 5) -> list[RetrievedDoc]:
    """DuckDuckGo 兜底检索（第三级 fallback）。

    定位：免 key 免配置的保底源，仅在 Tavily+Exa 双源全挂时使用。只有 snippet 无
    raw_content——供 tool-loop 的按需精读与可引用摘录注册使用，
    证据质量降档但保住「有证据可写」。

    未受墙钟 deadline 约束：`_retrieve` 的协作式 deadline 只在轮次边界检查（见
    subagent._run_subagent_sync），本调用是一次同步网络请求，若恰好在双源全挂后、
    deadline 已过期时触发，仍会跑完这一次 DDGS().text() 才被下一次边界检查捕获——
    这是协作式 deadline 设计的既有特性（Task 1），不是本函数的缺陷；只在双源全挂
    这条罕见路径上才可能发生，超时幅度上限是一次 DDG 请求的耗时。"""
    from ddgs import DDGS  # 惰性 import：主路径不依赖它

    with DDGS() as d:
        results = list(d.text(query, max_results=top_k))
    return [
        RetrievedDoc(
            source_url=r.get("href"),
            title=r.get("title") or "",
            snippet=r.get("body") or "",
            raw_content=None,
            score=0.0,
        )
        for r in results
    ]
