"""A1 query 语言分流的 prompt 契约测试（四方对比修复①）。

动机：四方对比归因——glm 拆解的 search_query 全中文 → 命中中文 SEO 聚合站 →
引用域名层级垫底、无机制材料。修复是在两处 prompt（Research Plan / loop worker）
加「query 语言按信息源所在地选」规则：全球性/技术/财报类走英文 +
机构性关键词直命中一手来源，仅中国本土议题用中文。

诚实边界：本测试只守「规则还在 prompt 里」（防后续改 prompt 时静默撤销），
不担保模型行为；这里只保护确定性的语言路由契约。
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

from dra.nodes import _RESEARCH_PLAN_SYSTEM  # noqa: E402
from dra.toolloop import _LOOP_SYSTEM  # noqa: E402


def test_research_plan_prompt_routes_query_language_by_source_locale():
    """Research Plan：search_query 语言按信息源所在地选，全球议题走英文。"""
    assert "信息源所在地" in _RESEARCH_PLAN_SYSTEM
    assert "英文" in _RESEARCH_PLAN_SYSTEM


def test_loop_prompt_routes_query_language():
    """loop worker：模型自主 search 的 query 也吃同一规则。"""
    assert "信息源所在地" in _LOOP_SYSTEM
