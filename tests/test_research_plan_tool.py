"""运行期自报路线：主 Agent 全量提交 `items[{id,text,status}]`（EXPERIMENT_PLAN §7 第 1 项）。

这里锁住的四条：

- **全量替换**，不是增量合并——它交的是"现在的路线"这张快照；
- 服务端**只做结构校验**（id 唯一且合法、status 合法、条数上限），
  **不判断方向对错**；校验失败时**不改状态**（不能把一次坏调用变成路线的半截更新）；
- **不参与完成度计算**——覆盖仍以原问题要求为准，否则会重新长出两套完成度；
- 子 Agent 拿不到这个工具（§3：子 Agent 只有那五个研究工具）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dra import events as dra_events
from dra import simple_research as sr

from test_budget_wrapup import QUERY, Script, _doc, _FINISH_SEQUENCE, _turn

HEAD = _FINISH_SEQUENCE[:2]           # search_sources → read_source（路线更新不需要存证）
FULL_FINISH = _FINISH_SEQUENCE[3]
FULL_DRAFT = FULL_FINISH.tool_calls[0].arguments["draft"]


def _config():
    from dra.simple_research import SimpleConfig
    from dra.subagent import SubAgentConfig

    return SimpleConfig(
        adapter=SubAgentConfig(model="m", provider="p", reasoning=False),
        max_llm_calls=16,
        max_tool_calls=16,
        wall_timeout_s=30.0,
    )


def plan_turn(items, note=None):
    args: dict = {"items": items}
    if note is not None:
        args["note"] = note
    return _turn(("update_research_plan", args))


def _run(tmp_path: Path, turns, *, initial_route=None):
    script = Script(turns)
    run_dir = tmp_path / "run"
    # 直连调内核时没有 web 层的订阅者，事件不会落盘——测试自己订一份。
    captured: list[dict] = []
    handle = dra_events.subscribe(lambda event: captured.append(dict(event)))
    try:
        result = sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            config=_config(),
            run_dir=run_dir,
            initial_route=initial_route,
            model_call=script,
            search_fn=lambda q: [_doc()],
            ref_resolver=script,
        )
    finally:
        dra_events.unsubscribe(handle)
    ledger = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    return result, ledger, captured


def plan_actions(ledger):
    return [a for a in ledger["actions"] if a["action"] == "research_plan"]


def plan_events(events):
    return [e for e in events if e.get("type") == "research_plan_updated"]


def plan_rejections(events):
    """只取"这一次路线提交没通过"的事件。

    工具参数非法走的是通用失败路径（`status="failed"`）；`rejected` 是留给交稿拒收、
    预算挡下这类语义的。所以这里按 `ok is False` + 终态来筛，别把交稿那道混进来。
    """
    return [
        e
        for e in events
        if e.get("type") == "subagent_tool_call"
        and e.get("tool") == "update_research_plan"
        and e.get("phase") == "done"
        and e.get("ok") is False
    ]


ITEMS = [
    {"id": "notes", "text": "先读官方文档", "status": "done"},
    {"id": "cost", "text": "再比成本与维护代价", "status": "doing"},
    {"id": "verdict", "text": "最后给结论", "status": "todo"},
]


# --- 基本行为 ---------------------------------------------------------------


def test_plan_tool_records_items_and_emits_event(tmp_path):
    # 必须先真实存入 c1；过去该夹具依赖“被拒交稿仍残留 draft”的错误行为。
    result, ledger, events = _run(tmp_path, [*_FINISH_SEQUENCE[:3], plan_turn(ITEMS, note="官方文档够用了"), FULL_FINISH])
    assert result.draft == FULL_DRAFT          # 路线更新不打断正常交稿

    actions = plan_actions(ledger)
    assert len(actions) == 1
    assert [item["id"] for item in actions[0]["payload"]["items"]] == ["notes", "cost", "verdict"]
    assert actions[0]["payload"]["note"] == "官方文档够用了"

    emitted = plan_events(events)
    assert len(emitted) == 1
    assert [item["status"] for item in emitted[0]["items"]] == ["done", "doing", "todo"]


def test_plan_tool_is_full_replace_not_merge(tmp_path):
    """第二次提交的是"现在的路线"整张快照：没提到的条目就是被丢掉了。"""
    _, ledger, events = _run(
        tmp_path,
        [
            *HEAD,
            plan_turn(ITEMS),
            plan_turn([{"id": "verdict", "text": "最后给结论", "status": "doing"}]),
            FULL_FINISH,
        ],
    )
    actions = plan_actions(ledger)
    assert [item["id"] for item in actions[-1]["payload"]["items"]] == ["verdict"]
    # 服务端算出"改了什么"，不靠模型自述。
    diff = plan_events(events)[-1]["diff"]
    assert set(diff["removed"]) == {"notes", "cost"}
    assert diff["changed"] == ["verdict"]
    assert diff["added"] == []


def test_initial_route_seeds_the_first_plan(tmp_path):
    """开跑前确认的那份路线就是初始值——不要另起一套播种逻辑。"""
    _, ledger, events = _run(
        tmp_path,
        [*HEAD, plan_turn([{"id": "capability", "text": "看清能力边界", "status": "doing"}]), FULL_FINISH],
        initial_route=[
            {"id": "capability", "text": "看清能力边界", "status": "todo"},
            {"id": "cost", "text": "再比成本与维护代价", "status": "todo"},
        ],
    )
    initial, first = plan_events(events)
    assert [item["id"] for item in initial["items"]] == ["capability", "cost"]
    assert all(item["status"] == "todo" for item in initial["items"])
    assert [item["id"] for item in first["items"]] == ["capability"]
    assert set(first["diff"]["removed"]) == {"cost"}
    assert first["diff"]["changed"] == ["capability"]


# --- 结构校验：拒绝时不改状态 ------------------------------------------------


@pytest.mark.parametrize(
    "items, match",
    [
        ([{"id": "a", "text": "x", "status": "doing"}, {"id": "a", "text": "y", "status": "todo"}], "id 重复"),
        ([{"id": "a", "text": "x", "status": "unknown"}], "status"),
        ([{"id": "", "text": "x", "status": "todo"}], "id"),
        ([{"id": "a", "text": "  ", "status": "todo"}], "text"),
    ],
)
def test_invalid_plan_is_rejected(items, match, tmp_path):
    """坏调用只失败那一次：路线保持上一次的合法状态，不会被半截更新污染。"""
    _, ledger, events = _run(
        tmp_path,
        [*HEAD, plan_turn(ITEMS), plan_turn(items), FULL_FINISH],
    )
    actions = plan_actions(ledger)
    assert len(actions) == 1, "第二次应当被拒，不落账"
    assert [item["id"] for item in actions[0]["payload"]["items"]] == ["notes", "cost", "verdict"]
    assert len(plan_events(events)) == 1

    rejected = plan_rejections(events)
    assert rejected, "拒收要如实发事件"
    assert match in json.dumps(rejected[0], ensure_ascii=False)


def test_plan_tool_rejects_too_many_items(tmp_path):
    many = [{"id": f"s{i}", "text": f"步骤 {i}", "status": "todo"} for i in range(sr._MAX_PLAN_ITEMS + 1)]
    _, ledger, events = _run(tmp_path, [*HEAD, plan_turn(many), FULL_FINISH])
    assert plan_actions(ledger) == []
    rejected = plan_rejections(events)
    assert str(sr._MAX_PLAN_ITEMS) in json.dumps(rejected[0], ensure_ascii=False)


# --- 不参与完成度 -----------------------------------------------------------


def test_plan_does_not_change_coverage(tmp_path):
    """路线条目不是要求：它全标 done 也不影响覆盖，覆盖只按原问题要求算。"""
    result, _, _ = _run(
        tmp_path,
        [*HEAD, plan_turn([{**item, "status": "done"} for item in ITEMS]), FULL_FINISH],
    )
    assert set(result.research_coverage) == {"goal"}


def test_subagent_does_not_get_the_plan_tool():
    """子 Agent 只拿那五个研究工具（§3）：它不维护路线。"""
    lead_tools = {t["function"]["name"] for t in sr.LEAD_TOOL_SCHEMAS}
    sub_tools = {t["function"]["name"] for t in sr.TOOL_SCHEMAS}
    assert {"update_research_plan", "delegate_research"} <= lead_tools
    assert sub_tools == {
        "search_sources", "read_source", "save_evidence", "inspect_research", "finish_research",
    }
    assert "update_research_plan" not in sub_tools
