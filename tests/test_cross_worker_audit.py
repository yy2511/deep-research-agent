"""冻结证据后的跨 Worker 审查测试。"""

import json
from unittest.mock import MagicMock

from dra.models import EvidenceCard
from dra.nodes import run_cross_worker_audit


def _card(claim: str = "占位 claim", quote: str = "占位 quote") -> EvidenceCard:
    return EvidenceCard(
        claim=claim,
        support_quote=quote,
        source_url="https://example.com",
    )


def test_empty_evidence_is_explicitly_flagged_without_model_call():
    result = run_cross_worker_audit("任意问题", [])

    assert result.has_findings is True
    assert "没有可审计的证据" in result.reason
    assert result.conflicts == []


def test_audit_parses_conflicts_and_findings(monkeypatch):
    cards = [_card("COVID 死亡 690 万"), _card("COVID 死亡 700 万")]
    monkeypatch.setattr("dra.llm.chat", MagicMock(return_value=json.dumps({
        "has_findings": True,
        "reason": "证据充分但存在矛盾",
        "conflicts": [{
            "dimension": "死亡人数",
            "card_ids": [1, 2],
            "description": "WHO 官方 690 万 vs 实时统计 700 万，口径不同",
        }],
    }, ensure_ascii=False)))

    result = run_cross_worker_audit("COVID 死亡人数", cards)

    assert result.has_findings is True
    assert result.conflicts[0].dimension == "死亡人数"
    assert result.conflicts[0].card_ids == [1, 2]


def test_audit_filters_malformed_conflicts(monkeypatch):
    monkeypatch.setattr("dra.llm.chat", MagicMock(return_value=json.dumps({
        "has_findings": False,
        "reason": "未见明显覆盖风险",
        "conflicts": [
            {"dimension": "", "card_ids": [1], "description": "缺维度"},
            {"dimension": "定义", "card_ids": [1], "description": "合法矛盾"},
            {"dimension": "越界", "card_ids": [99], "description": "越界"},
        ],
    }, ensure_ascii=False)))

    result = run_cross_worker_audit("q", [_card("c")])

    assert result.has_findings is True  # 合法矛盾本身就是发现
    assert [conflict.dimension for conflict in result.conflicts] == ["定义"]
