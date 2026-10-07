"""提示词审核后的约定：模型只看到与本轮真实能力一致、不重复的说明与回执。"""
import json

import httpx2

from dra import evidence_judge as ej, simple_research as sr
from test_budget_wrapup import _turn
from test_evidence_judge import context as judge_context, install_transport, response


def test_citable_sources_follow_available_tools():
    plain = sr._lead_system(delegation=True, execution=False)
    with_execution = sr._lead_system(delegation=True, execution=True)
    assert "read_source 展示的原文" in plain and "bash 计算结果" not in plain
    assert "声明了输入来源（bash.source_refs）的 bash 计算结果" in with_execution
    # 子 Agent 没有代码容器，不能被告知可以引用 bash 结果。
    assert "bash" not in sr._SUBAGENT_SYSTEM
    assert "不靠你自觉" not in plain + sr._SUBAGENT_SYSTEM


def test_lead_prompt_starts_with_identity_and_defines_jev_once_up_front():
    system = sr._lead_system()
    assert system.startswith("你是研究主 Agent")
    assert system.index("Jev 是系统自动调用的引文核验模型") < system.index("交给 Jev 联合判断")
    for stale in ("某选手", "保留区", "【确认过的目标】", "语言与问题一致"):
        assert stale not in system


def test_wrapup_prompt_only_asks_for_what_wrapup_can_do():
    lead = sr._wrapup_system(role="lead", delegation=True)
    child = sr._wrapup_system(role="subagent", delegation=True)
    for prompt in (lead, child):
        assert "重新 save_evidence" not in prompt and "needs_selection" not in prompt
        assert "依据不足或矛盾的卡优先不用" in prompt
    assert "research_plan.scope_update" in lead and "update_research_plan 的 scope_update" not in lead
    assert "用户中途" not in child and "research_plan 提交最终任务清单" not in child
    assert "因果、比较与总结" in lead and "因果、比较与总结" not in child
    assert "子任务异常" not in sr._wrapup_system(role="lead", delegation=False)


def test_subagent_gets_its_own_finish_rules_and_single_mission(tmp_path):
    finish = next(s for s in sr.SUBAGENT_TOOL_SCHEMAS if s["function"]["name"] == "finish_research")
    assert "发现清单" in finish["function"]["description"]
    assert "因果" not in finish["function"]["description"]
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id="mission", query="主问题"), sr.SimpleConfig())
    child = ctx.fork_for_subagent(ctx.config, budget=None, delegation_id="sub1")
    mission = "核实 A 厂商的保修期\n【主 Agent 提供的背景】只看官网"
    text = sr._current_mission(child, [sr.Requirement(id="sub", text=mission, kind="goal")], mission, None)
    assert text.count("核实 A 厂商的保修期") == 1
    assert "【子任务目标】" in text and "requirement_id=sub" in text
    assert "【用户要求】" not in text and "output_formats" not in text and "已确认问题" not in text


def test_save_receipt_lists_each_quote_once_and_only_jev_conclusion(tmp_path, monkeypatch):
    async def handler(request):
        return httpx2.Response(200, json=response("A"))
    install_transport(monkeypatch, handler)
    ctx, ref = judge_context(tmp_path)
    text, ok = ctx.save_evidence({"cards": [{"claim": "产品只在室内用", "quote": "产品仅在室内使用。", "source_ref": ref}]})
    row = json.loads(text)["results"][0]
    assert ok and row["accepted"]
    assert "quote" not in row and "source_url" not in row and "found" not in row
    quote = row["quotes"][0]
    assert quote["quote"] == "产品仅在室内使用。"
    assert not {"submitted_quote", "deep_link", "source_url", "summary_ref"} & set(quote)
    assert row["judgment"] == {"status": "complete", "relation": "supported", "error_code": None}
    # 卡片数据仍保留完整核验记录与展示字段，界面照常使用。
    card = ctx.store.cards()[0]
    assert card.judgment.attempts and card.quotes[0].deep_link
    # 回执已带过的判定，不再以“Jev 判断更新”重复推送。
    exchange = [{"role": "assistant", "content": None, "tool_calls": [{"id": "save"}]},
                {"role": "tool", "tool_call_id": "save", "content": text}]
    messages = sr._build_messages([], ctx, [], [exchange], seconds_left=None)
    assert "Jev 引文支持判断更新" not in json.dumps(messages, ensure_ascii=False)


def test_reserve_rule_only_described_when_reserve_is_active():
    def card_id_help(config):
        schemas = sr._with_reserve_note(sr.LEAD_TOOL_SCHEMAS, config)
        tool = next(s for s in schemas if s["function"]["name"] == "inspect_research")
        return tool["function"]["parameters"]["properties"]["card_id"]["description"]
    assert "额度只剩" not in card_id_help(sr.SimpleConfig())
    assert "工具额度只剩最后 2 次" in card_id_help(sr.SimpleConfig(max_tool_calls=10))
    assert "保留区" not in json.dumps(sr.LEAD_TOOL_SCHEMAS, ensure_ascii=False)


def test_language_rule_appears_once(tmp_path):
    seen = []
    def model(messages, **_):
        seen.append(messages)
        return _turn(("finish_research", {"draft": "", "gaps": ["无"]}))
    # 空正文会被打回；调用额度让这轮在几次打回后收尾结束。
    sr.run_simple_research("Compare A and B", sr.requirements_from_query("Compare A and B"),
                           config=sr.SimpleConfig(max_llm_calls=3), run_dir=tmp_path, model_call=model, search_fn=lambda _q: [])
    request = json.dumps(seen[0], ensure_ascii=False)
    assert request.count("【回答语言】") == 1
    assert "语言与问题一致" not in request


def test_plan_rules_are_not_repeated_in_every_status_update(tmp_path):
    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id="plan", query="q"), sr.SimpleConfig())
    ctx.request_plan_review("user:u1", "收到用户补充 u1")
    text = json.dumps(sr._build_messages([], ctx, [], [], seconds_left=None), ensure_ascii=False)
    assert "【当前任务清单】" in text and "【路线核对提醒】" in text
    for repeated in ("写长报告前", "原样提交", "开始一项任务时标 doing"):
        assert repeated not in text
    plan = next(s for s in sr.LEAD_TOOL_SCHEMAS if s["function"]["name"] == "update_research_plan")
    assert "相同清单" not in plan["function"]["description"]


def test_jev_knows_what_computation_evidence_can_support():
    instructions = ej.QUESTIONS["relation"]["instructions"]
    assert ej.PROMPT_VERSION == "evidence-support-v4"
    assert "source_kind=computation" in instructions and "不是资料原文" in instructions
    assert "参赛" not in instructions
