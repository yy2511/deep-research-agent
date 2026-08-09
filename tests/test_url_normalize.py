"""URL 归一化测试（Fix 1 References 按 URL 分组 + Fix 2 去重 key 归一化）。

背景：References 区块按证据卡编号逐行展示 source_url，但引用单元是证据卡
（claim+quote）而非网页，同一 URL 常在多个编号下重复出现；同时去重 key 用
原始 URL 字符串相等，trailing slash / utm_* 追踪参数 / http vs https / www.
这类"同页不同写法"的 URL 变体会逃过精确去重，产出字面不同但实为同一页的
「假重复」证据卡。

本文件覆盖：
- normalize_url 本身的归一化规则（纯函数，本地跑）；
- deduplicate_evidence 用归一化 key 后：URL 变体+相同 claim 合并，
  同 URL 不同 claim 依旧不合并（zero-误杀哲学不破）；
- render_report_markdown 的 References 按归一化 URL 分组展示（渲染层，
  不影响正文内联 [n] 与全局编号）。

全部纯逻辑、本地、秒级，不联网。
"""

from dra.models import (
    EvidenceCard,
    Report,
    ReportSection,
    deduplicate_evidence,
    normalize_url,
)
from dra.nodes import render_report_markdown


def _card(
    claim: str,
    url: str | None = "https://a.example/1",
) -> EvidenceCard:
    return EvidenceCard(
        claim=claim,
        support_quote=f"quote for: {claim}",
        source_url=url,
    )


# ---------------------------------------------------------------------------
# normalize_url 本身
# ---------------------------------------------------------------------------


def test_trailing_slash_equal():
    """路径尾部斜杠不影响归一化结果。"""
    assert normalize_url("https://example.com/post/") == normalize_url("https://example.com/post")


def test_utm_params_stripped():
    """utm_* 追踪参数被剥离，不参与归一化 key。"""
    a = normalize_url("https://example.com/post?utm_source=x&utm_medium=y")
    b = normalize_url("https://example.com/post")
    assert a == b


def test_real_query_param_kept():
    """非追踪查询参数是真实内容差异，必须保留（?page=2 ≠ 无 page）。"""
    a = normalize_url("https://example.com/list?page=2")
    b = normalize_url("https://example.com/list")
    assert a != b


def test_mixed_params_keeps_only_real_one():
    """真实参数 + utm 追踪参数混合 → 只剥 utm，保留真实参数。"""
    a = normalize_url("https://example.com/list?page=2&utm_source=x")
    b = normalize_url("https://example.com/list?page=2")
    assert a == b


def test_fragment_stripped():
    """#fragment 是页内锚点，不改变服务端内容，剥离。"""
    a = normalize_url("https://example.com/post#section2")
    b = normalize_url("https://example.com/post")
    assert a == b


def test_http_https_equal():
    """http → https 视为同页（协议统一）。"""
    assert normalize_url("http://example.com/post") == normalize_url("https://example.com/post")


def test_www_and_case_host_normalized():
    """host 大小写 + www. 前缀归一化。"""
    a = normalize_url("https://WWW.Example.COM/post")
    b = normalize_url("https://example.com/post")
    assert a == b


def test_path_case_preserved():
    """路径大小写是真实语义差异（大小写敏感的服务器），不能归一化掉。"""
    a = normalize_url("https://example.com/Post")
    b = normalize_url("https://example.com/post")
    assert a != b


def test_none_and_empty_return_empty_string():
    """None / 空串（含纯空白）→ 归一化为空串，与旧 (c.source_url or "") 兜底行为一致。"""
    assert normalize_url(None) == ""
    assert normalize_url("") == ""
    assert normalize_url("   ") == ""


# ---------------------------------------------------------------------------
# deduplicate_evidence：归一化 key 生效
# ---------------------------------------------------------------------------


def test_dedup_merges_url_variants_same_claim():
    """同 claim，URL 只差 trailing slash + utm 参数 → 视为同一页，合并保留首条。"""
    cards = [
        _card("RAG 减少幻觉", "https://a.example/post/"),
        _card("RAG 减少幻觉", "https://a.example/post?utm_source=x"),
    ]
    result = deduplicate_evidence(cards)
    assert len(result) == 1
    assert result[0].claim == "RAG 减少幻觉"


def test_dedup_does_not_merge_same_normalized_url_different_claim():
    """URL 归一化后相同，但 claim 不同 → 依旧不合并（zero-误杀哲学不因归一化改变）。"""
    cards = [
        _card("RAG 减少幻觉", "https://a.example/post/"),
        _card("RAG 的应用场景包括问答系统", "https://a.example/post?utm_source=x"),
    ]
    result = deduplicate_evidence(cards)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# render_report_markdown：References 按归一化 URL 分组
# ---------------------------------------------------------------------------


def test_references_groups_cited_numbers_by_normalized_url():
    """card 1/3 是同页两个 URL 变体，card 2 是另一 URL → References 出 "[1][3] " 一行 + "[2] " 一行。

    分组展示文本用组内最小编号那张卡的**原始**（未归一化）URL；旧的逐编号
    单独一行（如 "[3] https://a.com/x" 独立一行）不应再出现。
    """
    evidence = [
        _card("E1", "https://a.com/x"),
        _card("E2", "https://b.com/y"),
        _card("E3", "https://a.com/x/?utm_source=t"),  # 与 card1 同页变体
    ]
    report = Report(
        title="T",
        sections=[ReportSection(heading="S", markdown="第一句[1]，第二句[2]，第三句[3]。")],
    )
    md = render_report_markdown(report, evidence)
    print(f"\n[References 分组]\n{md}")
    assert "[1][3] https://a.com/x" in md
    assert "[2] https://b.com/y" in md
    # 旧格式：card3 独立一行不应再出现
    assert "[3] https://a.com/x/?utm_source=t" not in md


def test_references_no_url_cards_group_together():
    """无 URL 的卡片统一分到 "(无 URL)" 组。"""
    evidence = [_card("E1", None), _card("E2", None)]
    report = Report(
        title="T",
        sections=[ReportSection(heading="S", markdown="一句[1]，另一句[2]。")],
    )
    md = render_report_markdown(report, evidence)
    print(f"\n[References 无 URL 分组]\n{md}")
    assert "[1][2] (无 URL)" in md
