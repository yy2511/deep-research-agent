"""P1-1a Scoping 候选判定测试。

本步只测纯逻辑 `scope_query`，不联网、不调 LLM：
- 明显主观且缺标准的 query 应追问；
- 短对象的泛化评价应追问对象类型与维度；
- 短英文普通词定义可能多义，应追问；
- 已明确要求多含义/多身份，以及明确缩写术语，不应拦截。
"""

import pytest

from dra.nodes import scope_query


@pytest.mark.parametrize(
    ("query", "expected_fragment"),
    [
        ("最好的框架是什么？", "AI Agent"),
        ("哪个框架最强", "Web 开发"),
        ("如何评价王x1", "人物、产品/品牌"),
        ("Mercury 是什么？", "分别介绍所有含义"),
        ("", "具体主题"),
    ],
)
def test_ambiguous_query_requests_clarification(query, expected_fragment):
    result = scope_query(query)
    print(f"\n[需澄清] query={query!r} result={result.model_dump()}")

    assert result.needs_clarification is True
    assert result.clarification_question is not None
    assert expected_fragment in result.clarification_question
    assert result.reason


@pytest.mark.parametrize(
    "query",
    [
        "什么是 RAG？",
        "RAG 是什么？",
        "Mercury 的含义有哪些？",
        "不同的李明有哪些身份",
        "Python 的两种含义分别是什么？",
        "PostgreSQL 和 MongoDB 的事务机制有什么区别？",
    ],
)
def test_explicit_or_clear_query_passes_without_clarification(query):
    result = scope_query(query)
    print(f"\n[直接研究] query={query!r} reason={result.reason}")

    assert result.needs_clarification is False
    assert result.clarification_question is None
