"""可控追问 coverage 样本与交稿协议回归；模型边界完全离线。"""
import json
from pathlib import Path

import pytest

from dra import simple_research as sr
from dra.research_input import ResearchInput
from test_budget_wrapup import Script, _text_turn, _turn


REPORTS = json.loads((Path(__file__).parent / "fixtures/followup_coverage_reports.json").read_text())
FOLLOWUP = [sr.Requirement(id="followup", text="给一个简短总结")]
DRAFT = "已整理本轮问题；仍缺公开材料，暂不能给出确定结论。"


def setup(tmp_path, *, role="lead", **overrides):
    config = sr.SimpleConfig(**{"max_llm_calls": 5, "max_tool_calls": 4, **overrides})
    store = sr.open_run(tmp_path / "run", query="给一个简短总结", run_id="coverage-test")
    budget = sr.Budget(config, deadline=None)
    context = sr.ToolContext(store, config, budget=budget)
    context.tool_role = role
    return config, store, budget, context


def finish(coverage, **extra):
    return {"draft": DRAFT, "coverage": coverage, "gaps": [], **extra}


def reported(rid="followup", status="unanswered"):
    return {"requirement_id": rid, "status": status, "note": "仍缺公开材料"}


def run(context, config, store, budget, requirements, model, *, role="lead", wrapup=False):
    if wrapup:
        draft, coverage, gaps, error = sr._wrapup_turn(store, config, requirements, context,
            model_call=model, budget=budget, role=role)
        return {"draft": draft, "coverage": coverage, "gaps": gaps, "error": error}
    return sr.run_lead_loop(store, config, requirements, budget=budget, context=context,
                            model_call=model, role=role)


@pytest.mark.parametrize("sample", REPORTS, ids=lambda row: row["run_id"])
def test_bad_followup_coverage_keeps_body_without_repair(tmp_path, sample):
    """错误编号不能吞正文，也不能靠猜编号把它改报成已覆盖；只丢写错的条目。"""
    config, store, budget, context = setup(tmp_path)
    requirements = [sr.Requirement.model_validate(row) for row in sample["requirements"]]
    script = Script([_turn(("finish_research", finish(sample["coverage"])))])
    result = run(context, config, store, budget, requirements, script)
    assert len(script.seen) == budget.calls_used == 1
    assert result["draft"] == DRAFT and result["stop_reason"] == "finished"
    assert result["coverage"] == []
    projected, reasons = sr.project_coverage(requirements, result["coverage"])
    assert projected == {"followup": "unanswered"}
    assert "未报告" in " ".join(reasons["followup"])
    assert not [a for a in store.state["actions"] if a["action"] == "finish_rejected"]


@pytest.mark.parametrize("wrapup", [False, True])
@pytest.mark.parametrize("field,bad,note", [
    # 错的那条丢掉，对的那条照收
    ("coverage", [reported(), reported("unknown", "answered")], None),
    # 缺口写成对象：保留里面的文字
    ("gaps", {"note": "不是字符串数组"}, "不是字符串数组"),
    # 清单条目全部写错：保留上一版清单
    ("research_plan", {"items": [{"id": "summary", "text": "总结", "status": "invalid"}]}, "最终任务清单未同步"),
])
def test_bad_metadata_keeps_body_and_last_valid_plan(tmp_path, wrapup, field, bad, note):
    config, store, budget, context = setup(tmp_path)
    previous = [{"id": "summary", "text": "总结", "status": "doing"}]
    assert context.update_research_plan({"items": previous})[1]
    payload = {**finish([reported()]), field: bad}
    script = Script([_turn(("finish_research", payload))])
    result = run(context, config, store, budget, FOLLOWUP, script, wrapup=wrapup)
    assert result["draft"] == DRAFT and not result.get("error")
    assert len(script.seen) == budget.calls_used == 1, "辅助字段不触发额外修复回合"
    assert result["coverage"] == [reported()]
    assert note is None or any(note in gap for gap in result["gaps"])
    assert context.plan_items == previous and not context.final_plan_reported
    assert not [a for a in store.state["actions"] if a["action"] == "finish_rejected"]


@pytest.mark.parametrize("status", ["answered", "partial", "unanswered", "unavailable"])
def test_valid_ids_and_all_statuses_are_preserved(tmp_path, status):
    config, store, budget, context = setup(tmp_path)
    requirements = [*FOLLOWUP, sr.Requirement(id="inherited-1", text="仅官方来源", kind="constraint")]
    coverage = [reported("inherited-1", "answered"), reported(status=status)]
    script = Script([_turn(("finish_research", finish(coverage)))])
    result = run(context, config, store, budget, requirements, script)
    assert result["coverage"] == coverage and len(script.seen) == 1
    projected, _ = sr.project_coverage(requirements, result["coverage"])
    assert projected == {"followup": status, "inherited-1": "answered"}


@pytest.mark.parametrize("coverage", [None, [], [reported("inherited-1", "answered")]])
def test_missing_reports_never_receive_an_inferred_answered_status(tmp_path, coverage):
    config, store, budget, context = setup(tmp_path)
    requirements = [*FOLLOWUP, sr.Requirement(id="inherited-1", text="仅官方来源", kind="constraint")]
    payload = finish(coverage)
    if coverage is None:
        payload.pop("coverage")
    result = run(context, config, store, budget, requirements,
                 Script([_turn(("finish_research", payload))]))
    projected, reasons = sr.project_coverage(requirements, result["coverage"])
    assert projected["followup"] == "unanswered"
    assert "Lead 未报告该要求的覆盖情况" in reasons["followup"]


@pytest.mark.parametrize("wrapup", [False, True])
def test_subagent_does_not_apply_parent_coverage_to_itself(tmp_path, wrapup):
    config, store, budget, context = setup(tmp_path, role="subagent")
    context.effective_requirements = FOLLOWUP
    child = [sr.Requirement(id="sub", text="核对一个子问题")]
    script = Script([_turn(("finish_research", finish([reported()])))])
    result = run(context, config, store, budget, child, script, role="subagent", wrapup=wrapup)
    assert result["draft"] == DRAFT and result["coverage"] == []
    assert len(script.seen) == 1


def scope_plan(context):
    context.user_input = ResearchInput("coverage-test", lambda _: None)
    context.user_input.accept("u1", "只查软件兼容")
    sr._receive_user_input(context, "initial")
    return {"items": [{"id": "software", "text": "查软件兼容", "status": "todo"}],
            "scope_update": {"goal": "查软件兼容", "reason": "用户缩小范围",
                             "requirements": [{"id": "software", "text": "查软件兼容", "kind": "goal"}]}}


@pytest.mark.parametrize("wrapup", [False, True])
def test_valid_scope_survives_bad_finish_metadata(tmp_path, wrapup):
    config, store, budget, context = setup(tmp_path)
    previous = [{"id": "summary", "text": "整理旧范围", "status": "doing"}]
    assert context.update_research_plan({"items": previous})[1]
    plan = scope_plan(context)
    plan["items"][0]["status"] = "invalid"
    script = Script([_turn(("finish_research", finish([reported()], research_plan=plan)))])
    result = run(context, config, store, budget, FOLLOWUP, script, wrapup=wrapup)
    assert result["draft"] == DRAFT and result["coverage"] == []
    assert len(script.seen) == 1
    assert [r.id for r in context.effective_requirements] == ["software"]
    assert len(context.scope_history) == 1
    assert context.scope_history[0]["revision"] == 1
    assert context.scope_history[0]["user_message_ids"] == ["u1"]
    assert context.plan_items == previous and not context.final_plan_reported


def test_invalid_scope_still_rejects_finish_atomically(tmp_path):
    """正常研究回合：范围变更写错时整次交稿打回，模型还能改好再交。"""
    config, store, budget, context = setup(tmp_path, max_llm_calls=1, reserve_wrapup_calls=0)
    plan = scope_plan(context)
    plan["scope_update"]["requirements"] = [{"id": "software"}]
    script = Script([_turn(("finish_research", finish([reported()], research_plan=plan)))])
    result = run(context, config, store, budget, FOLLOWUP, script)
    assert result["draft"] == ""
    assert not context.scope_history and not context.plan_items
    assert not [a for a in store.state["actions"] if a["action"] in {"research_plan", "finish_requested", "wrapup_submitted"}]


def test_wrapup_keeps_body_when_scope_update_is_invalid(tmp_path):
    """收尾只有一次机会：范围变更写错时忽略它，正文照收，并留下说明。"""
    config, store, budget, context = setup(tmp_path, max_llm_calls=1, reserve_wrapup_calls=0)
    plan = scope_plan(context)
    plan["scope_update"]["requirements"] = [{"id": "software"}]
    script = Script([_turn(("finish_research", finish([reported()], research_plan=plan)))])
    result = run(context, config, store, budget, FOLLOWUP, script, wrapup=True)
    assert result["draft"] == DRAFT and not result["error"]
    assert not context.scope_history, "写错的范围变更不能生效"
    assert any("研究范围变更格式有误" in gap for gap in result["gaps"])


def test_already_updated_scope_does_not_accept_old_coverage(tmp_path):
    config, store, budget, context = setup(tmp_path)
    script = Script([_turn(("update_research_plan", scope_plan(context))),
                     _turn(("finish_research", finish([reported()])))])
    result = run(context, config, store, budget, FOLLOWUP, script)
    assert result["draft"] == DRAFT and result["coverage"] == []
    assert len(script.seen) == 2
    assert [r.id for r in context.effective_requirements] == ["software"]


def test_text_only_wrapup_keeps_missing_coverage_explicit(tmp_path):
    config, store, budget, context = setup(tmp_path)
    body, coverage, gaps, error = sr._wrapup_turn(store, config, FOLLOWUP, context,
        model_call=lambda *a, **k: _text_turn(DRAFT), budget=budget)
    assert body == DRAFT and coverage == [] and not error
    assert any("未报告覆盖" in gap for gap in gaps)
    projected, _ = sr.project_coverage(FOLLOWUP, coverage)
    assert projected == {"followup": "unanswered"}


def test_mission_distinguishes_requirement_id_and_kind():
    prompt = sr._render_mission("给一个简短总结", FOLLOWUP)
    assert "id=followup" in prompt and "kind=goal" in prompt
    assert "通常为 goal" not in sr._LEAD_SYSTEM
    assert "通常为 goal" not in json.dumps(sr.FINISH_SCHEMA, ensure_ascii=False)


def test_coverage_synonyms_and_partial_mistakes_are_repaired(tmp_path):
    """近义状态按意思归一；编号大小写不同也认；写错的条目只丢它自己。"""
    config, store, budget, context = setup(tmp_path)
    requirements = [*FOLLOWUP, sr.Requirement(id="inherited-1", text="仅官方来源", kind="constraint")]
    coverage = [reported("FollowUp", "partially_answered"), reported("inherited-1", "已回答"),
                reported("unknown", "answered")]
    result = run(context, config, store, budget, requirements,
                 Script([_turn(("finish_research", finish(coverage)))]))
    assert [(c["requirement_id"], c["status"]) for c in result["coverage"]] == [
        ("followup", "partial"), ("inherited-1", "answered")]


def test_final_plan_repairs_status_synonyms_and_bad_ids(tmp_path):
    config, store, budget, context = setup(tmp_path)
    plan = {"items": [
        {"id": "查资料", "text": "查官方资料", "status": "completed"},
        {"id": "verify", "text": "交叉核对", "status": "in_progress"},
        {"id": "verify", "text": "重复编号的一条", "status": "pending"},
        {"id": "bad", "text": "", "status": "done"},
    ]}
    result = run(context, config, store, budget, FOLLOWUP,
                 Script([_turn(("finish_research", finish([reported()], research_plan=plan)))]))
    assert result["draft"] == DRAFT
    assert [(i["id"], i["status"]) for i in context.plan_items] == [
        ("t1", "done"), ("verify", "doing"), ("t3", "todo")]
    assert context.final_plan_reported
    assert any("1 条格式有误" in gap for gap in result["gaps"])
