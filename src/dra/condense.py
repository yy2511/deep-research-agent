"""整页压缩：把超长正文压成「摘要 + 逐字片段」，供 ``read_source`` 首次精读使用。

**为什么不复用 ``nodes.summarize_doc``**：主线刻意不依赖 ``nodes``——那是已退役计划路径的
模块，当初把 ``clean_markdown`` 搬进 ``text.py`` 就是为了断开这条依赖链（见 ``text.py``
开头）。压缩需要自己的提示词与解析，所以单独放这里。

**一条硬约束**：只有能在原文里**逐字命中**的片段才配进可引用集合。摘要本身永远不可引用
——它是模型生成的文字，不是原文；摘要能被引用就等于允许"引一句没人说过的话"。
本模块只负责产出候选；逐字核验与编号登记由调用方（``simple_research``）完成，因为它
才持有 ``Reading``。
"""

from __future__ import annotations

_CHUNK_CHARS = 120_000
"""未配置模型窗口时沿用的单块大小；全文分块覆盖，不丢弃后文。"""

_CONDENSE_SYSTEM = """你要把一份**很长的网页正文**压成给研究 agent 用的材料。
你只做压缩，不回答问题、不补充原文里没有的事实。

输出 JSON，两个字段：

1. `summary`：说清本块材料讲什么、与研究问题的关系、未覆盖的条件；保留相关名单、数字、日期和限制。
   只写原文支持的内容；原文没说的不要推断。

2. `excerpts`：与问题相关的**逐字摘录**，每条是原文里**连续的一段**，不得改写、拼接、
   补字或省略中间部分（省略号也不行）。优先挑与研究问题直接相关、信息密度高的段落
   （结论、数字、限制条件、对比表）。按完整信息选择长度和数量，不为缩短而丢掉限定条件或名单条目。

硬要求：
- `excerpts` 里的每一条都必须能在原文里**一字不差**地找到；找不到的会被丢弃。
- 不要为了凑数给重复内容；不要给标题行或目录条目。
- 只输出 JSON，不要输出其他说明。
"""


def condense_text(
    query: str,
    text: str,
    *,
    call_json,
    max_input_chars: int = _CHUNK_CHARS,
    fits_request=None,
    **chat_kwargs,
) -> dict:
    """把长正文压成 ``{"summary": str, "excerpts": [str], "error": str}``。

    失败一律**如实返回 error**（调用方据容量返回原文或阅读入口），绝不抛异常打断研究：
    压缩是"省上下文"的优化，不是阅读的前置条件。
    """
    summaries, excerpts, errors, ranges, completed = [], [], [], [], []
    start = end = 0
    if max_input_chars <= 0:
        return {"summary": "", "excerpts": [], "error": "模型窗口不足以容纳网页压缩输入",
                "condense_input_range": None, "condense_input_ranges": [], "condense_input_truncated": True}
    def request(end):
        return [
            {"role": "system", "content": _CONDENSE_SYSTEM},
            {"role": "user", "content": f"【研究问题】{query}\n【本块范围】chars={start}-{end}；全文 {len(text)} 字符\n\n【网页正文】\n{text[start:end]}"},
        ]
    while start < len(text):
        end = min(start + max_input_chars, len(text)) if fits_request is None else len(text)
        if fits_request is not None and not fits_request(request(end)):
            lo, hi = start, end
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if fits_request(request(mid)):
                    lo = mid
                else:
                    hi = mid - 1
            end = lo
            if end == start:
                errors.append("当前模型窗口不足以容纳网页压缩请求")
                break
        if end < len(text):
            boundary = text.rfind("\n", start, end)
            if boundary > start:
                end = boundary + 1
        messages = request(end)
        ranges.append(f"chars={start}-{end}")
        try:
            data = call_json(messages, expect_keys=("summary",), **chat_kwargs)
        except Exception as exc:
            errors.append(type(exc).__name__)
            break
        if not isinstance(data, dict):
            errors.append("回执不是 JSON 对象")
            break
        summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
        raw_excerpts = data.get("excerpts")
        pieces = [piece.strip() for piece in raw_excerpts if isinstance(piece, str) and piece.strip()] if isinstance(raw_excerpts, list) else []
        if not summary and not pieces:
            errors.append(str(data.get("error") or "空回执"))
            break
        if summary:
            summaries.append(summary.strip() if len(text) <= max_input_chars else f"[chars={start}-{end}] {summary.strip()}")
        for piece in pieces:
            if piece not in excerpts:
                excerpts.append(piece)
        completed.append(f"chars={start}-{end}")
        start = end
    return {"condense_input_range": f"chars=0-{end}",
            "condense_input_ranges": ranges,
            "condense_completed_ranges": completed,
            "condense_input_truncated": end < len(text),
            "summary": "\n\n".join(summaries), "excerpts": excerpts,
            "error": "; ".join(errors)}
