"""V2-5 reranker 纯逻辑测试：排序、截断、字段、边界。

真实 CrossEncoder 加载标 live；这里 monkeypatch 掉模型，只测 rerank 的
打分→排序→截断逻辑（不下 2.3GB 模型、秒级）。
"""
from __future__ import annotations

import pytest

from dra.rag import reranker as R


class _FakeCE:
    """假 CrossEncoder：按预设 {text: score} 给分，验证 rerank 的排序逻辑。"""

    def __init__(self, score_map: dict[str, float]):
        self.score_map = score_map

    def predict(self, pairs, show_progress_bar=False):
        # pairs = [(query, text), ...]，按 text 查预设分
        return [self.score_map.get(t, 0.0) for _, t in pairs]


@pytest.fixture
def patch_reranker(monkeypatch):
    """把 _get_reranker 替换成返回指定假分数的 CE。"""
    def _install(score_map: dict[str, float]):
        monkeypatch.setattr(R, "_get_reranker", lambda model_name=None: _FakeCE(score_map))
    return _install


class TestRerank:
    def test_reorders_by_cross_encoder_score(self, patch_reranker):
        """召回顺序被 cross-encoder 分数推翻：召回排最后的若 CE 分最高 → 升到第一。"""
        candidates = [
            {"id": "a", "text": "doc A", "score": 0.9},  # 召回第 1
            {"id": "b", "text": "doc B", "score": 0.8},
            {"id": "c", "text": "doc C", "score": 0.7},  # 召回最后
        ]
        # CE 认为 C 最相关、A 最不相关 → 推翻召回顺序
        patch_reranker({"doc A": 0.1, "doc B": 0.5, "doc C": 0.95})
        out = R.rerank("q", candidates, top_k=3)
        assert [c["id"] for c in out] == ["c", "b", "a"]
        # rerank_score 字段写入且降序
        assert out[0]["rerank_score"] == 0.95
        assert out[0]["rerank_score"] >= out[1]["rerank_score"] >= out[2]["rerank_score"]

    def test_preserves_original_score_field(self, patch_reranker):
        """原召回 score 保留，新增 rerank_score（两个分都在，供对照）。"""
        candidates = [{"id": "a", "text": "x", "score": 0.42}]
        patch_reranker({"x": 0.88})
        out = R.rerank("q", candidates, top_k=1)
        assert out[0]["score"] == 0.42       # 召回分保留
        assert out[0]["rerank_score"] == 0.88  # rerank 分新增

    def test_top_k_truncates(self, patch_reranker):
        """top_k=2 但有 5 候选 → 只返 rerank 分最高的 2 个。"""
        candidates = [{"id": str(i), "text": f"t{i}", "score": 0.5} for i in range(5)]
        patch_reranker({f"t{i}": i / 10 for i in range(5)})  # t4 最高
        out = R.rerank("q", candidates, top_k=2)
        assert [c["id"] for c in out] == ["4", "3"]

    def test_empty_candidates_returns_empty(self, patch_reranker):
        patch_reranker({})
        assert R.rerank("q", [], top_k=5) == []

    def test_empty_query_returns_empty(self, patch_reranker):
        patch_reranker({"x": 0.9})
        assert R.rerank("", [{"id": "a", "text": "x"}], top_k=5) == []

    def test_custom_text_key(self, patch_reranker):
        """支持自定义 text_key（如去重场景传 claim 字段）。"""
        candidates = [{"id": "a", "claim": "C1"}, {"id": "b", "claim": "C2"}]
        patch_reranker({"C1": 0.2, "C2": 0.9})
        out = R.rerank("q", candidates, top_k=2, text_key="claim")
        assert [c["id"] for c in out] == ["b", "a"]
