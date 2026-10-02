"""派子任务开关：关掉时主 Agent 看不到协作工具，系统提示说明本轮独自完成。"""
from dra import simple_research as sr
from test_budget_wrapup import Script, _turn


def test_delegation_switch_removes_coordination_tools_and_says_so(tmp_path):
    """关掉派子任务：主 Agent 看不到四个协作工具，系统提示说明本轮独自完成。"""
    def tools_and_prompt(**config):
        script = Script([_turn(("finish_research", {"draft": "无。", "coverage": [], "gaps": []}))])
        sr.run_simple_research("q", sr.requirements_from_query("q"), config=sr.SimpleConfig(**config),
                               run_dir=tmp_path / str(config), model_call=script, search_fn=lambda _q: [])
        return set(script.tool_schemas[0]), script.seen[0][0]["content"]

    tools, prompt = tools_and_prompt()
    assert sr.DELEGATION_TOOL_NAMES <= tools and "本轮不派子任务" not in prompt
    tools, prompt = tools_and_prompt(delegation_enabled=False)
    assert not sr.DELEGATION_TOOL_NAMES & tools
    assert {"search_sources", "read_source", "save_evidence", "finish_research"} <= tools
    assert "本轮不派子任务" in prompt
