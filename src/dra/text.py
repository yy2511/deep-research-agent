"""抓取正文的确定性清洗：把 markdown 样板噪声剥成纯文本。

**为什么单独成模块**：它原先寄居在 ``nodes.py``（计划路径的模块，3300+ 行）。主线只是要用这
一个字符串函数，却因此把整条旧架构依赖链拖进了运行时——``simple_research`` / ``subagent``
→ ``toolloop`` → ``nodes``。搬出来之后，删旧路径时不必再为它做特殊处理。
"""

from __future__ import annotations

import re


def clean_markdown(text: str) -> str:
    """剥掉 raw_content 的 markdown 样板噪声，只留正文文本。

    Tavily 整页 raw_content 头部常是导航 / 图片 / 标题 / tag 链接等样板（例如前
    300 字全是 `[❯作者](url)`+图片 gif+`# 标题`+一排 `[tag](/tags/..)`）。头部截断会把
    6000 预算喂给这些垃圾。这里用确定性正则把 markdown 语法剥成纯文字（不上 per-page
    LLM 摘要——那是每篇一次调用的贵方案，先看确定性清洗够不够）。
    只剥语法、不动正文内容；防造假不受影响（内容词原样保留）。
    """
    # 先还原 markdown 转义。抓取器把纯文本当 markdown 处理时会给标点加反斜杠（`\_` `\*` `\[`…），
    # 那**不是原文内容**：留着会让 `read_source(find=...)` 与引用核对在原文明明有这一段时定位
    # 失败（例如快照里是 `async def wait\_for`，模型按原文找 `async def wait_for` 永远找不到）。
    # 只还原 CommonMark 允许转义的标点；反斜杠出现在别处（如路径 C:\Users）照旧保留。
    text = re.sub(r"\\([!\"#$%&'()*+,\-./:;<=>?@\[\]\\^_`{|}~])", r"\1", text)
    # 图片整段删（纯噪声）；链接只留可见文字；裸 URL 去掉
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"<[^>\n]+>", " ", text)                    # 残留 HTML 标签
    # 行首结构符号：标题 # / 引用 > / 列表 -*+ 或 1.
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s{0,3}>\s?", "", text)
    text = re.sub(r"(?m)^\s{0,3}(?:[-*+]|\d+\.)\s+", "", text)
    # 保留下划线：sqlite3_rsync / SQLITE_BUSY 等标识符的字符不能当作强调语法删掉。
    # 有意允许少量 _强调_ 标记残留，优先保持来源里的技术名称。
    text = re.sub(r"[*`~]+", "", text)
    text = re.sub(r"\|", " ", text)
    # 折叠空白与空行
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
