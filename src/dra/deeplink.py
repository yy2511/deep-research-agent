"""引用深链：让"点引用"能跳到原文并**高亮被引的那一段**。

两半，可靠性完全不同：

1. **本地快照高亮**（我们自己的数据）：卡片自带 ``location=chars=a-b``，快照就在磁盘上，
   由 web 层的 ``?hl=`` 参数负责渲染；快照缺失或哈希不符时明确拒收。
2. **原网页高亮**（浏览器特性）：URL 里带 **text fragment**（``#:~:text=``），支持该特性的
   浏览器会滚动到并高亮匹配的文本，不需要页面作者配合（见 MDN / web.dev 的 Text Fragments）。
   本模块只负责**生成**这个链接。

原网页那一半有一个硬前提，必须说清：**那段文字必须逐字出现在目标页面上**。我们的 quote 来自
上游抓取的正文（快照零修改之后＝上游原样），还受页面块结构、词边界与浏览器支持限制；一旦
上游做过转义或改写（例如把词内下划线转义成 ``\\_``），浏览器就**静默失败**——不滚动、不高亮，
用户只看到一个普通页面。因此调用方必须把它当"尽力而为"：失败时退回普通链接。
"""

from __future__ import annotations

import re
from urllib.parse import quote as _urlquote
from urllib.parse import urlsplit

_MIN_QUOTE_CHARS = 8
"""短于这个长度的引用不做深链：太短的串在页面上到处都是，命中位置几乎必然不是我们要的那处。"""

_EDGE_CHARS = 48
"""首尾片段的目标长度；延伸到完整词边界，不能为了固定长度截断词。"""


def _encode(text: str) -> str:
    # urllib 即使 safe="" 仍保留 '-'，但 text fragment 将它用于上下文语法。
    return _urlquote(text, safe="").replace("-", "%2D")


def text_fragment_url(url: str, quote_text: str) -> str | None:
    """生成"跳到原网页并高亮这段引用"的链接；不适用时返回 ``None``。

    短单行引用用整段；长引用尽量用不重叠的完整词片段 ``start,end``。
    无法安全拆分的单行（例如无空格中文或长标识符）保留整段，避免词内截断。

    返回 ``None`` 的三种情况：URL 不是 http(s)、引用太短、引用里全是空白。
    """
    target = (url or "").strip()
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    text = (quote_text or "").strip()
    if len(text) < _MIN_QUOTE_CHARS:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    first, last = lines[0], lines[-1]
    # 只在已知的空白边界切分，不尝试自行实现各语言的分词规则。
    first_words = list(re.finditer(r"\S+", first))
    last_words = list(re.finditer(r"\S+", last))
    start_end = next((m.end() for m in first_words if m.end() >= _EDGE_CHARS), len(first))
    end_start = next((m.start() for m in reversed(last_words)
                      if len(last) - m.start() >= _EDGE_CHARS), 0)
    if len(lines) == 1 and (len(text) <= 2 * _EDGE_CHARS or start_end >= end_start):
        directive = _encode(text)
    else:
        directive = f"{_encode(first[:start_end])},{_encode(last[end_start:])}"
    base = target.split("#", 1)[0]
    return f"{base}#:~:text={directive}"
