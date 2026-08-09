"""证据去重测试（P2-2 → path A：移除 bi-encoder 语义去重，只留归一化精确去重）。

为什么改：text2vec bi-encoder cos≥0.85 两题对抗审计误杀率 50%/100%，把"同主题不同事实"
（不同数字/年份/政策）误杀，调阈值无解（DEVLOG 去重审计）。改为只精确去重：
- 真·逐字重复（同 url + 归一化后同 claim）→ 合并，保留首条（confidence 已移除）；
- 相似但不同的 claim（哪怕只差一点）→ **都保留**（宁可重复进 writer，绝不误杀独立证据）；
- 不同 url 即便 claim 全同 → 保守保留两条（多一个来源，writer 自然合并）。

本地（默认跑，秒级、不联网，无需 sentence-transformers）。
"""

from dra.models import (
    EvidenceCard,
    deduplicate_evidence,
)


def _card(claim: str, url: str = "https://a.example") -> EvidenceCard:
    return EvidenceCard(
        claim=claim,
        support_quote=f"quote for: {claim}",
        source_url=url,
    )


# ---------------------------------------------------------------------------
# 精确去重：合并 + 保留首条
# ---------------------------------------------------------------------------


def test_exact_duplicate_by_url_and_claim_keeps_first():
    """相同 (url, claim) 只保留一条，保留首条。"""
    cards = [
        _card("RAG 减少幻觉", "https://a.example"),
        _card("RAG 减少幻觉", "https://a.example"),
    ]
    result = deduplicate_evidence(cards)
    assert len(result) == 1
    assert result[0].claim == "RAG 减少幻觉"


def test_exact_duplicate_keeps_first_preserving_canonical_id():
    """精确重复时保留首条，已落账的 canonical ID 不因去重而悬空。"""
    first = _card("RAG 减少幻觉", "https://a.example")
    first.id = "canonical-old"
    second = _card("RAG 减少幻觉", "https://a.example")
    second.id = "transient-new"

    result = deduplicate_evidence([first, second])

    assert len(result) == 1
    assert result[0].id == "canonical-old"


def test_normalized_exact_dup_merged():
    """claim 只差空白/大小写 → 归一化后视为同一条，合并。"""
    cards = [
        _card("RAG 减少幻觉", "https://a.example"),
        _card("  rag 减少幻觉 ", "https://a.example"),  # 空格 + 大小写差异
    ]
    result = deduplicate_evidence(cards)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# path A 核心：相似/不同的 claim 一律保留，绝不误杀
# ---------------------------------------------------------------------------


def test_similar_but_distinct_claims_both_kept():
    """语义相近但字面不同的 claim → 都保留（path A：不再 bi-encoder 误杀）。"""
    cards = [
        _card("健康成人每日咖啡因上限为400毫克", "https://a.example"),
        _card("健康成年人单次咖啡因摄入不超过200毫克", "https://b.example"),  # 不同事实
    ]
    result = deduplicate_evidence(cards)
    assert len(result) == 2  # 旧 bi-encoder 会把这两条误并；现在都留


def test_same_claim_different_urls_both_kept():
    """claim 全同但不同 url → 保守保留两条（多一个来源，不强行合并）。"""
    cards = [
        _card("RAG 减少幻觉是检索增强生成的核心优势", "https://a.example"),
        _card("RAG 减少幻觉是检索增强生成的核心优势", "https://b.example"),
    ]
    result = deduplicate_evidence(cards)
    assert len(result) == 2


def test_different_claims_same_url_kept():
    """同 url 但 claim 不同，两条都保留。"""
    cards = [
        _card("RAG 减少幻觉", "https://blog.example"),
        _card("RAG 的应用场景包括问答系统和知识密集任务", "https://blog.example"),
    ]
    result = deduplicate_evidence(cards)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_empty_list():
    assert deduplicate_evidence([]) == []


def test_single_card():
    assert len(deduplicate_evidence([_card("RAG 减少幻觉")])) == 1


def test_exact_dup_merged_distinct_kept():
    """两条精确重复 + 一条不同 claim → 精确重复合 1，不同的保留，共 2。"""
    cards = [
        _card("完全相同的 claim", "https://a.example"),
        _card("完全相同的 claim", "https://a.example"),  # 精确重复
        _card("完全相同的 claim 扩展说明", "https://a.example"),  # 不同 claim
    ]
    result = deduplicate_evidence(cards)
    assert len(result) == 2


# ---------------------------------------------------------------------------
# deduplicate_evidence 返回值契约
# ---------------------------------------------------------------------------


def test_deduplicate_evidence_returns_list():
    """deduplicate_evidence 返回 list[EvidenceCard]，调用方据此索引。"""
    cards = [_card("RAG 减少幻觉"), _card("RAG 的应用场景包括问答")]
    result = deduplicate_evidence(cards)
    assert isinstance(result, list)
    assert all(isinstance(c, EvidenceCard) for c in result)
