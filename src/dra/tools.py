"""检索工具层：Tavily、Exa、DuckDuckGo fallback 与可选本地 RAG。

工具把外部检索源的原始返回「翻译」成项目自己的数据结构（RetrievedDoc），
让上层节点与具体检索源解耦。
tool-loop 只消费 RetrievedDoc：web 和 local 的文档
进同一个 docs 列表后，quote 逐字核验、证据抽取逻辑零改即覆盖两条路径。
"""

import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from tavily import TavilyClient

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


def web_search(query: str, top_k: int = 5, *, max_retries: int = 3) -> list[RetrievedDoc]:
    """联网检索，返回粗排后的文档列表（已映射为 RetrievedDoc）。

    失败时重试 ≤ max_retries 次（指数退避 1s/2s/4s），仍失败则抛 RuntimeError
    交由上层处理。

    注：Tavily 偶发 SSL EOF（UNEXPECTED_EOF，疑与 include_raw_content 大 payload + 并发有关），
    实测频发会把整题打成 0 证据。这里把默认重试 2→3 做止血；根治留 A-2 搜索级联兜底（换 ddg 源）。
    """
    resp = None
    for attempt in range(max_retries + 1):
        try:
            # _get_client() 在循环内取：连接级故障会 _reset_client()，下一轮拿到全新 client。
            resp = _get_client().search(
                query=query,
                max_results=top_k,
                search_depth="basic",
                # A-1 读取层（叉 a）：让 Tavily 回填整页正文 raw_content，不止 snippet。
                # search_depth 仍用 basic（最省 1 credit）；advanced（2 credit，更狠的
                # 正文抽取）是另一档成本旋钮，效果不够再上，不进叉 a。
                include_raw_content=True,
            )
            break
        except Exception as e:  # 工具边界统一兜底，再决定重试/上抛
            # SSL EOF / 连接重置：重置 client，强制下一轮用全新连接重试，
            # 否则会一直复用同一条断连、连试都原地挂在同一个 SSL EOF。
            if _is_conn_error(e):
                _reset_client()
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"web_search 失败（已重试 {max_retries} 次）: {e}") from e

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


def exa_search(query: str, top_k: int = 5, *, max_retries: int = 3) -> list[RetrievedDoc]:
    """Exa neural search → list[RetrievedDoc]，镜像 web_search 签名。

    Exa 一次返回 URL、query highlights、全文 text 和可选 publishedDate。

    设计要点:
    - **不复用 Tavily 单例 client**:Exa 是 raw POST,无 client 概念
    - 复用 `_try_parse_date` 处理 publishedDate(Exa 实测可能返 ISO long/short/human 三种格式)
    - 失败重试 ≤ max_retries 次(指数退避 1s/2s/4s),仍失败抛 RuntimeError 交上层
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
                },
                timeout=30,
            )
            if r.status_code != 200:
                # 422/500 等 HTTP 错误也算失败,触发重试
                raise RuntimeError(f"exa_search HTTP {r.status_code}: {r.text[:200]}")
            resp = r.json()
            break
        except Exception as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"exa_search 失败（已重试 {max_retries} 次）: {e}") from e

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


# ---------------------------------------------------------------------------
# V2-1：local_rag_search —— 经典 RAG 本地语料检索（照 web_search 模式）
# ---------------------------------------------------------------------------

# 默认 local 语料索引路径（项目根 / data/corpus/arxiv/index）。
# A「用户论文库接入位」：换语料时改这里或传 corpus_dir，代码零改
# （EXPERIMENT_PLAN 路径 A：不为企业语料硬造规模，corpus_dir 指过去即可）。
_DEFAULT_LOCAL_INDEX = Path(__file__).resolve().parents[2] / "data" / "corpus" / "arxiv" / "index"


def local_rag_search(
    query: str,
    top_k: int = 5,
    *,
    corpus_dir: str | Path | None = None,
) -> list[RetrievedDoc]:
    """本地 RAG 检索：在已建好的 local 语料向量索引里召回 top_k，翻译成 RetrievedDoc。

    与 web_search 对齐：同样返回 list[RetrievedDoc]，供 tool-loop 统一消费。
    - raw_content = chunk 原文：可直接注册为可引用摘录
      （防幻觉机制零改覆盖 local 路径）。
    - source_url = arxiv abs 链接（可点开核验）；title = 摘要标题。
    - score = cosine 相似度（来自 retriever，范围 [-1,1]）。

    corpus_dir 缺省用 _DEFAULT_LOCAL_INDEX；索引不存在 → 抛 RuntimeError（不静默降级，
    明确告诉调用方语料未建库，比返回空假装成功更诚实）。
    """
    from dra.rag.retriever import get_index  # 惰性 import：避免无 local 需求时加载 sentence-transformers

    index_dir = Path(corpus_dir) if corpus_dir is not None else _DEFAULT_LOCAL_INDEX
    if not (index_dir / "embeddings.npy").exists():
        raise RuntimeError(
            f"local 语料索引未建库：{index_dir} 下无 embeddings.npy。"
            f"先跑 uv run python -m dra.rag.indexer 建库。"
        )
    index = get_index(index_dir)
    hits = index.search(query, top_k=top_k)
    return [
        RetrievedDoc(
            source_url=h.get("source_url"),
            title=h.get("title") or "",
            snippet=h.get("text") or "",       # chunk 原文同时放 snippet，便于检索展示
            raw_content=h.get("text") or "",   # quote 逐字核验读 raw_content
            score=h.get("score", 0.0),
            published_at=h.get("published"),
        )
        for h in hits
    ]
