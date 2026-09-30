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

_MAX_INPUT_CHARS = 120_000
"""送去压缩的正文上限：压缩本身也必须是有界输入，不能把 20 万字整份塞进提示词。"""

_MAX_EXCERPTS = 8
"""最多要几条片段。多要无用：这些片段是要进模型上下文的，不是越全越好。"""

_MAX_EXCERPT_CHARS = 1_400
"""单条片段上限（比阅读片段 700 宽一倍）：超过就丢弃——长片段会把压缩视图撑成又一份全文。

长度上限靠**丢弃**而不是截断实现：截断出来的片段仍然是原文子串、可以引用，但它会让
"这条片段为什么只有一半"变得不可解释。宁缺勿滥。"""

_CONDENSE_SYSTEM = """你要把一份**很长的网页正文**压成给研究 agent 用的材料。
你只做压缩，不回答问题、不补充原文里没有的事实。

输出 JSON，两个字段：

1. `summary`：3–6 句，说清这份材料讲什么、由哪几部分组成、与研究问题的关系。
   只写原文支持的内容；原文没说的不要推断。

2. `excerpts`：3–8 条**逐字摘录**，每条是原文里**连续的一段**，不得改写、拼接、
   补字或省略中间部分（省略号也不行）。优先挑与研究问题直接相关、信息密度高的段落
   （结论、数字、限制条件、对比表）。每条 100–700 字符。

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
    max_input_chars: int = _MAX_INPUT_CHARS,
    **chat_kwargs,
) -> dict:
    """把长正文压成 ``{"summary": str, "excerpts": [str], "error": str}``。

    失败一律**如实返回 error**（调用方据此降级为代码切片），绝不抛异常打断研究：
    压缩是"省上下文"的优化，不是阅读的前置条件。
    """
    body = text[:max_input_chars]
    coverage = {
        "condense_input_range": f"chars=0-{len(body)}",
        "condense_input_truncated": len(body) < len(text),
    }
    messages = [
        {"role": "system", "content": _CONDENSE_SYSTEM},
        {"role": "user", "content": f"【研究问题】{query}\n\n【网页正文】\n{body}"},
    ]
    try:
        data = call_json(messages, expect_keys=("summary",), **chat_kwargs)
    except Exception as exc:  # 压缩失败不阻断阅读，如实记原因
        return {**coverage, "summary": "", "excerpts": [], "error": f"{type(exc).__name__}"}
    if not isinstance(data, dict):
        return {**coverage, "summary": "", "excerpts": [], "error": "回执不是 JSON 对象"}
    summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
    raw_excerpts = data.get("excerpts")
    if not isinstance(raw_excerpts, list):  # 护栏：字符串会被逐字符遍历成单字"摘录"
        raw_excerpts = []
    excerpts: list[str] = []
    for item in raw_excerpts:
        if not isinstance(item, str):
            continue
        piece = item.strip()
        if not piece or len(piece) > _MAX_EXCERPT_CHARS:
            continue
        if piece not in excerpts:
            excerpts.append(piece)
        if len(excerpts) >= _MAX_EXCERPTS:
            break
    if not summary and not excerpts:
        # 注入的边界可能自己带 error（如离线替身）；有就如实透传，别覆盖成"空回执"。
        return {**coverage, "summary": "", "excerpts": [], "error": str(data.get("error") or "空回执")}
    return {**coverage, "summary": summary.strip(), "excerpts": excerpts, "error": ""}
