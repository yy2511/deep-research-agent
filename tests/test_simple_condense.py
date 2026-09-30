"""超长正文的压缩视图：阈值直读、超阈值压缩、失败降级。

覆盖的是**真实行为**：
1. 短正文（≤ 阈值）照旧整篇直读——压缩在短页上必须是隐形的（零额外调用）；
2. 超阈值首读给压缩视图（摘要 + 逐字片段），片段可引用；
3. 压缩产出的片段必须逐字命中原文，改写的剔除；全被剔除 → 降级代码切片并说明；
4. 压缩失败/无额度 → 降级切片，阅读不中断；
5. 显式 range/find 不触发压缩；
6. 压缩调用进同一份预算、有账本记录，但不占收尾预留；同一来源只压缩一次。
"""

from __future__ import annotations

import json
import pytest

from dra import simple_research as sr

from test_simple_research import Script, _doc, _turn

THRESHOLD = 1000  # 测试里把阈值调小，逻辑与默认 40000 完全一致
HEAD = "第一章 结论\n\n"
TAIL = "\n\n第五章 限制\n\n这里写着限制条件：只支持 SSH。\n"
MID = "这是用于撑长正文的段落。" * 200  # 约 2400 字符
LONG_BODY = HEAD + MID + TAIL
SHORT_BODY = "短正文一句。\n\n第二句。\n"

SAVE = ("save_evidence", {"cards": [{"claim": "c", "source_ref": "s2", "excerpt_no": None}]})


def _recipe(*pieces):
    """压缩替身：返回固定配方，可选带 error。"""
    def factory():
        if len(pieces) == 1 and isinstance(pieces[0], Exception):
            raise pieces[0]  # 模拟压缩调用本身失败
        return {"summary": "这份材料讲了一件事。", "excerpts": list(pieces), "error": ""}
    return factory


def _run(turns, *, raw, config=None, recipe=None, run_dir=None):
    """跑一个流程；``recipe`` 为 None 时表示"不该发起压缩"。"""
    script = Script(turns)
    calls: list[dict] = []

    def condense_fn(messages, **kwargs):
        calls.append(kwargs)
        if recipe is None:
            raise AssertionError("这个用例不该发起压缩调用")
        return recipe()

    result = sr.run_simple_research(
        "q",
        sr.requirements_from_query("q"),
        config=config or sr.SimpleConfig(condense_over_chars=THRESHOLD),
        model_call=script,
        condense_fn=condense_fn,
        search_fn=lambda _q: [_doc(raw=raw)],
        ref_resolver=script,
        run_dir=run_dir,
    )
    return result, script, calls


def _read_receipts(script, index):
    """第 index 轮模型输入里的工具回执（解析 JSON）。"""
    out = []
    for message in script.seen[index]:
        if message.get("role") != "tool":
            continue
        try:
            payload = json.loads(message.get("content") or "")
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("source_ref"):
            out.append(payload)
    return out


def _receipt_by_tool(script, tool: str):
    """从账本回执里取最后一次该工具的回执（不依赖后续是否还有回合）。"""
    return next(
        json.loads(r["payload"])
        for r in reversed(script.context.receipts)
        if r["tool"] == tool
    )


def test_short_body_is_read_verbatim_without_condensing():
    """短正文不触发压缩：零额外调用，回执就是整篇。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    _result, script, calls = _run(turns, raw=SHORT_BODY)
    assert calls == [], "短正文不该发起压缩调用"
    payload = _read_receipts(script, 2)[-1]
    assert payload.get("view") != "condensed"
    assert "短正文一句。" in json.dumps(payload, ensure_ascii=False)


def test_long_body_first_read_returns_condensed_view():
    """超阈值首读：只调一次压缩，回执是压缩视图而不是全文。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    _result, script, calls = _run(turns, raw=LONG_BODY, recipe=_recipe(TAIL.strip()))
    assert len(calls) == 1, "同一来源只压缩一次"
    assert "max_tokens" in calls[0] and calls[0]["max_tokens"] is None
    payload = _read_receipts(script, 2)[-1]
    assert payload["view"] == "condensed"
    assert payload["summary"] == "这份材料讲了一件事。"
    assert payload["total_chars"] == len(LONG_BODY)
    assert payload["excerpts"][0]["quote"] == TAIL.strip()
    assert "压缩视图" in payload["note"]
    # 片段是**可引用**的：编号能解析回同一段原文
    reading = script.context.readings["s2"]
    no = payload["excerpts"][0]["excerpt_no"]
    assert reading.quote(no)[0] == TAIL.strip()
    # 整篇原文没有被灌进上下文
    assert LONG_BODY not in json.dumps(script.seen[2], ensure_ascii=False)


def test_condensed_excerpt_can_be_saved_as_card():
    """压缩视图里给出的片段，能直接存成卡（这就是"可引用"的端到端证明）。"""
    first_excerpt = sr._excerpt_table(LONG_BODY)[0][2]
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
        _turn(
            (
                "save_evidence",
                {"cards": [{"claim": "首段", "source_ref": "s2", "excerpt_no": 1}]},
            )
        ),
    ]
    result, script, calls = _run(turns, raw=LONG_BODY, recipe=_recipe(first_excerpt))
    assert len(calls) == 1
    payload = _read_receipts(script, 2)[-1]
    assert payload["excerpts"][0]["quote"] == first_excerpt
    assert len(result.cards) == 1, "压缩视图里的片段必须能直接存证"
    assert result.cards[0].quote == first_excerpt


def test_verbatim_gate_drops_rewritten_excerpts():
    """改写过的"摘录"进不了可引用集合；全被剔除时降级为代码切片。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    _result, script, _calls = _run(
        turns, raw=LONG_BODY, recipe=_recipe("原文里根本没有这句话。")
    )
    payload = _read_receipts(script, 2)[-1]
    assert payload["view"] == "slice", "全都过不了逐字门时必须如实降级"
    assert "代码切片" in payload["note"]
    assert all(row["quote"] in LONG_BODY for row in payload["excerpts"])


def test_partial_verbatim_keeps_the_real_one():
    """合法/非法混合：只留合法的那条。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    _result, script, _calls = _run(
        turns, raw=LONG_BODY, recipe=_recipe("编的片段。", TAIL.strip())
    )
    payload = _read_receipts(script, 2)[-1]
    assert payload["view"] == "condensed"
    quotes = [row["quote"] for row in payload["excerpts"]]
    assert quotes == [TAIL.strip()]


def test_condense_failure_degrades_to_slice():
    """压缩报错不阻断阅读：降级成代码切片，照样能存证。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
        _turn(("save_evidence", {"cards": [{"claim": "c", "source_ref": "s2", "excerpt_no": 1}]})),
    ]
    result, script, _calls = _run(turns, raw=LONG_BODY, recipe=_recipe(TimeoutError("boom")))
    payload = _read_receipts(script, 2)[-1]
    assert payload["view"] == "slice"
    assert "TimeoutError" in payload["note"]
    assert len(result.cards) == 1, "降级不等于读不到"


def test_no_budget_degrades_without_calling_model():
    """额度不够时连压缩调用都不发，直接降级切片。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    # 3 次额度：研究回合用掉 2 次后，还要留 1 次收尾 → 压缩无处取额度
    config = sr.SimpleConfig(condense_over_chars=THRESHOLD, max_llm_calls=3)
    _result, script, calls = _run(turns, raw=LONG_BODY, config=config, recipe=_recipe("x"))
    assert calls == [], "没有额度就不该发起压缩调用"
    # 额度刚好用尽：这一轮之后没有下一轮，所以从账本回执读，而不是从后续输入读。
    payload = _receipt_by_tool(script, "read_source")
    assert payload["view"] == "slice"
    assert "额度" in payload["note"]


def test_explicit_range_skips_condensing():
    """显式 range 是精确请求：不压缩，直读该区间。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1", "range": "0-200"})),
    ]
    _result, script, calls = _run(turns, raw=LONG_BODY)
    assert calls == []
    payload = _read_receipts(script, 2)[-1]
    assert payload.get("view") != "condensed"
    assert payload["shown_range"].startswith("chars=0-")


def test_condense_call_is_recorded_in_budget():
    """压缩是真实的模型调用：进账本、计入额度，但不占收尾预留。"""
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    config = sr.SimpleConfig(condense_over_chars=THRESHOLD, max_llm_calls=6)
    result, _script, calls = _run(turns, raw=LONG_BODY, config=config, recipe=_recipe(TAIL.strip()))
    assert len(calls) == 1
    steps = [(c.role, c.step) for c in result.calls]
    assert ("condense", "condense") in steps, steps
    assert len(result.calls) <= config.max_llm_calls


def test_default_threshold_is_40k():
    """默认阈值就是要上线的那一档：40k 以下直读、以上压缩。"""
    assert sr.SimpleConfig().condense_over_chars == 40_000
    body = "填充。" * 8_500  # 约 25500 字符：仍在阈值以下
    turns = [
        _turn(("search_sources", {"query": "q"})),
        _turn(("read_source", {"source_ref": "s1"})),
    ]
    _result, script, calls = _run(turns, raw=body, config=sr.SimpleConfig())
    assert calls == []
    assert _read_receipts(script, 2)[-1].get("view") != "condensed"

@pytest.mark.parametrize('output_cap', [None, 4096])
def test_condense_output_cap_and_call_identity_are_explicit(tmp_path, output_cap):
    from dra import timing
    from dra.subagent import SubAgentConfig
    recorded = []
    def condense(messages, **kwargs):
        recorded.append((kwargs, timing.get_ctx()))
        return _recipe(TAIL.strip())()
    script = Script([
        _turn(('search_sources', {'query': 'q'})),
        _turn(('read_source', {'source_ref': 's1'})),
    ])
    sr.run_simple_research('q', run_dir=tmp_path, model_call=script,
        ref_resolver=script, condense_fn=condense, search_fn=lambda _: [_doc(raw=LONG_BODY)],
        config=sr.SimpleConfig(condense_over_chars=THRESHOLD, max_output_tokens=output_cap,
            adapter=SubAgentConfig(reasoning=True, effort='medium')))
    kwargs, ctx = recorded[0]
    assert kwargs['max_tokens'] == output_cap
    assert kwargs['reasoning'] is True and kwargs['effort'] == 'medium'
    assert ctx['usage_role'] == 'condense'
    assert ctx['call_id'] != ctx['parent_model_call_id']
    assert ctx['parent_model_call_id'] == 'lead-2'
    stats = json.loads((tmp_path / 'call_stats.json').read_text())
    row = next(c for c in stats['rounds'] if c['role'] == 'condense')
    assert row['call_id'] == ctx['call_id']
    assert row['agent_id'] == 'lead' and row['tool_calls'] == 0
