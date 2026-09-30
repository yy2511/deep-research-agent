"""检索与澄清共用的数据类型；证据和研究状态由自主循环维护。"""

import uuid
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


def _short_id() -> str:
    """uuid4 短码，作为各对象的可读 id。"""
    return uuid.uuid4().hex[:8]


class RetrievedDoc(BaseModel):
    """一次检索召回的文档；可来自 Web provider 或本地 RAG。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=_short_id)
    source_url: str | None = None
    title: str
    snippet: str                       # 检索返回的摘要片段
    raw_content: str | None = None     # 整页正文（A-1 叉a，逐字门核验的 ground truth）
    score: float = 0.0                 # 检索分（粗排）
    published_at: str | None = None    # P2-1 发布日期 ISO 字符串，从 Tavily/snippet 解析
    document: dict | None = None       # 本地文档版本、页映射与命中区间；不是网页 URL


_TRACKING_PARAM_KEYS = frozenset(
    {"fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "igshid"}
)
"""公认纯追踪查询参数（精确匹配，不含 utm_* 前缀——那个用 startswith 单独判）。"""


def normalize_url(url: str | None) -> str:
    """URL 归一化（仅用于比较 key，绝不改写展示/存储的原始 URL）。

    只清洗确定不承载内容语义的部分——宁可漏合并也不误合并（对齐去重
    「误杀不可逆」哲学）：
    - 协议统一（http→https 视为同页）、host 小写、剥 host 的 "www." 前缀
    - 剥路径尾部斜杠（根路径 "/" 保留语义等价于空）
    - 剥公认纯追踪查询参数（utm_* 前缀、fbclid、gclid、msclkid、mc_cid、
      mc_eid、igshid），其余查询参数原样保留（?page=2 是真内容差异）
    - 剥 #fragment（页内锚点不改变服务端内容）
    - 空/None → ""
    """
    if not url or not url.strip():
        return ""

    parts = urlsplit(url.strip())

    scheme = "https" if parts.scheme.lower() == "http" else parts.scheme.lower()

    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[len("www."):]

    path = parts.path.rstrip("/")

    kept_query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAM_KEYS
    ]
    query = urlencode(kept_query)

    return urlunsplit((scheme, netloc, path, query, ""))


PlanId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=8000, pattern=r"\S")]
