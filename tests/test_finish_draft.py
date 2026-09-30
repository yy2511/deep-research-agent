"""交稿必须是完整正文：指针式 draft 与空 draft 同类，一律拒收并让模型重交。

真实运行 历史运行：模型在过程消息里写了 7400 字答案，到
finish_research 只交了"（正文见上）结论：…"加 37 个引用堆在末尾，draft 仅 268 字。
而 draft 非空、引用门也过，于是四个状态全绿、用户拿到空壳——过程说明不是交付物。

只拦短正文开头明确声明正文在别处的空壳；正常转承用语不应触发重交。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dra import simple_research as sr

from test_budget_wrapup import QUERY, Script, _doc, _FINISH_SEQUENCE, _turn


def _config():
    from dra.simple_research import SimpleConfig
    from dra.subagent import SubAgentConfig

    return SimpleConfig(
        adapter=SubAgentConfig(model="m", provider="p", reasoning=False),
        max_llm_calls=14,
        max_tool_calls=14,
        wall_timeout_s=30.0,
    )


HEAD = _FINISH_SEQUENCE[:3]          # search_sources → read_source → save_evidence
FULL_FINISH = _FINISH_SEQUENCE[3]
FULL_DRAFT = FULL_FINISH.tool_calls[0].arguments["draft"]
# 按 brief-probe-20260916/REVIEW.md 的真实故障记录还原正文并展开 37 个引用。
POINTER_SAMPLE = (
    '（正文见上）结论：默认不迁、维持 MySQL，并给出该建议成立的前提与转为"有条件迁"的触发条件；'
    '同时给出收益侧（区分当下必需/可选）与代价侧清单、决策规则、分阶段路径与替代优化方向。'
    + ''.join(f'[c{i}]' for i in range(1, 38))
)


def _finish(draft: str):
    return _turn(
        (
            "finish_research",
            {
                "draft": draft,
                "coverage": [{"requirement_id": "goal", "status": "answered"}],
            },
        )
    )


def _run(tmp_path: Path, turns):
    script = Script(turns)
    run_dir = tmp_path / "run"
    result = sr.run_simple_research(
        QUERY,
        sr.requirements_from_query(QUERY),
        config=_config(),
        run_dir=run_dir,
        model_call=script,
        search_fn=lambda q: [_doc()],
        ref_resolver=script,
    )
    ledger = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    return result, ledger


# --- 判定 -------------------------------------------------------------------


@pytest.mark.parametrize(
    "draft",
    [
        POINTER_SAMPLE,
        "正文见上，结论是两文件都能同步。[c1]",
        "完整答案见上文，本机两个文件也能同步。[c1]",
    ],
)
def test_pointer_shaped_draft_is_recognized(draft):
    assert sr._pointer_draft_reason(draft)


@pytest.mark.parametrize(
    "draft",
    [
        # 简单题的答案本来就可能很短：短本身不是罪状。
        "不要求一端为远程，两文件都在本机也能同步 [c1]。",
        # 常见行文里的"如上所述"出现在长文里是正常连接词，不该被当成指针。
        "## 分析\n\n如上所述，本机两个文件也能同步。[c1]\n\n" + "补充说明段落。" * 120,
        "前述研究说明本机两个文件也能同步 [c1]。",
        "如上文所述，结论是本机两个文件也能同步 [c1]。",
    ],
)
def test_normal_draft_is_not_flagged(draft):
    assert sr._pointer_draft_reason(draft) is None


# --- 端到端 -----------------------------------------------------------------


def test_pointer_draft_is_rejected_and_model_can_resubmit(tmp_path):
    result, ledger = _run(
        tmp_path,
        [*HEAD, _finish("（正文见上）结论：两文件都能同步。[c1]"), FULL_FINISH],
    )
    rejected = [a for a in ledger["actions"] if a["action"] == "finish_rejected"]
    assert len(rejected) == 1, "指针式交稿必须被拒收，而不是照收"
    assert "见上" in rejected[0]["payload"]["reason"]
    assert rejected[0]["payload"]["draft_chars"] < 400
    assert result.draft == FULL_DRAFT, "重交的那份才是交付物"
    assert result.stop_reason == "finished"


@pytest.mark.parametrize("short", [
    "前述研究说明本机两个文件也能同步 [c1]。",
    "如上文所述，结论是本机两个文件也能同步 [c1]。",
])
def test_normal_short_draft_is_still_accepted(tmp_path, short):
    """简单问题的精炼短答照收：这道闸只拦"指向别处"，不拦"说得少"。"""
    result, ledger = _run(tmp_path, [*HEAD, _finish(short)])
    assert [a for a in ledger["actions"] if a["action"] == "finish_rejected"] == []
    assert result.draft == short
    assert result.stop_reason == "finished"
    assert result.draft_rejected is False
    assert result.draft_rejected_note == ""


# --- 引用编号：交稿时就查，而不是事后降级 -------------------------------------


def _bad_citation():
    """只存了 c1，却引用 c9——真实运行里那一趟引的是不存在的 [c27]。"""
    return _finish("结论：两文件也能同步 [c9]。")


def test_unknown_citation_is_rejected_at_submit_time(tmp_path):
    """交稿那一刻就查编号：此时模型还有机会自己改，事后降级则没有。"""
    result, ledger = _run(tmp_path, [*HEAD, _bad_citation(), FULL_FINISH])
    rejected = [a for a in ledger["actions"] if a["action"] == "finish_rejected"]
    assert len(rejected) == 1
    reason = rejected[0]["payload"]["reason"]
    assert "c9" in reason
    assert "c1" in reason, "要给出本次可用的编号范围，否则模型只能瞎猜"
    assert result.draft == FULL_DRAFT, "改了之后重交的那份才算交付"
    assert result.draft_rejected is False


def test_unknown_citation_is_accepted_after_the_repair_budget(tmp_path):
    """连着写错也不能卡死：到上限就照收，退回事后降级那条路。"""
    result, ledger = _run(tmp_path, [*HEAD, _bad_citation(), _bad_citation(), _bad_citation()])
    rejected = [a for a in ledger["actions"] if a["action"] == "finish_rejected"]
    assert len(rejected) == 2, "最多拒两次"
    assert result.draft_rejected is True            # 第三次照收，但照实降级
    assert "c9" in result.draft_rejected_note
    assert "不存在的卡片" in result.draft_rejected_note
    assert "核查" not in result.draft_rejected_note, "核查没跑，就不能说『未通过核查』"
