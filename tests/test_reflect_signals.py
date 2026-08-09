"""全局审计信号工程：来源分布注入 / gap 模糊去重 / conflict severity。"""
from dra.models import Conflict, EvidenceCard
from dra.nodes import _source_stats
from dra.orchestrator import _gap_seen, _norm_gap


def test_source_stats():
    ev = [EvidenceCard(claim="a", support_quote="q",
                       source_url="https://www.arxiv.org/abs/1"),
          EvidenceCard(claim="b", support_quote="q",
                       source_url="https://arxiv.org/abs/2"),
          EvidenceCard(claim="c", support_quote="q",
                       source_url="https://who.int/x")]
    s = _source_stats(ev)
    assert "3 条证据" in s and "2 个域名" in s and "arxiv.org×2" in s


def test_gap_seen_fuzzy():
    attempted = {_norm_gap("补充 RAG chunk 粒度对召回率的量化数据")}
    # 归一化后仅差「（2026）」后缀，SequenceMatcher ratio≈0.86 → 阈值 0.85 判同
    assert _gap_seen("补充RAG chunk粒度对召回率的量化数据（2026）", attempted)   # 语义同
    assert not _gap_seen("补充混合架构的企业落地案例", attempted)               # 真新 gap


def test_conflict_severity_default():
    c = Conflict(dimension="d", card_ids=[1], description="x")
    assert c.severity == "medium"
