"""抓取正文的清洗：剥 markdown 样板，同时**还原 markdown 转义**。

真实失败（历史运行）：模型想读 CPython 的 `tasks.py` 里
`async def wait_for` 这一段，用 `read_source(find="async def wait_for")` 定位——报"没有在原文
中找到该子串"。查快照发现存进去的是 `async def wait\\_for(fut, timeout):`：抓取器把它当
markdown 处理，把下划线转义了。**原文里明明有这一段，却因为没有还原转义而定位不到**，
模型于是被工具报错推着改写法，最后连正文都没交出来。
"""

from __future__ import annotations

import pytest

from dra.text import clean_markdown


@pytest.mark.parametrize(
    "escaped",
    [
        "async def wait\\_for(fut, timeout):",
        "snake\\_case\\_name",
        "\\*强调\\*",
        "\\`code\\`",
        "a \\[b\\] c",
        "x \\| y",
        "\\# 标题",
        "50\\% 覆盖",
    ],
)
def test_markdown_escapes_leave_no_backslash(escaped):
    """转义痕迹不该留在正文里——它是表示层的，不是原文。

    注意不能只断言"子串还在"：`\\# 标题` 里本来就含 `# 标题`，那样写会假通过。
    对下划线这类，还原后原样保留；对 `*` `` ` `` 这类，还原后被既有的剥语法规则删掉——
    两种情况都**不该留下反斜杠**。
    """
    assert "\\" not in clean_markdown(escaped)


def test_unescaped_source_is_findable_again():
    """这是那次失败的最小复现：还原之后，按原文的子串能定位到。"""
    fetched = "async def wait\\_for(fut, timeout):\n    \"\"\"Wait for ...\"\"\"\n"
    cleaned = clean_markdown(fetched)
    assert "async def wait_for" in cleaned


def test_backslash_before_ordinary_char_is_kept():
    """只还原 CommonMark 允许转义的标点；反斜杠出现在别处（如 Windows 路径）照旧保留。"""
    assert "C:\\Users\\x" in clean_markdown("C:\\Users\\x")
