"""V4 tool-loop 骨架：脚本化 tool_calls 序列驱动，全 mock 零 API 成本。

护栏矩阵：finish 正常收敛 / 预算强制收工 / 墙钟 timeout / 连续无效熔断 /
异常降级 / OpenAI tool_call_id 应答完整性。
"""
import time

import dra.toolloop as tl
from dra.llm import ToolCall, ToolTurn
from dra.models import RetrievedDoc, ResearchTask
from dra.subagent import SubAgentConfig


TASK = ResearchTask(node_id="test", objective="测试目标", search_query="seed query")


def _doc(id_="d1"):
    return RetrievedDoc(id=id_, source_url=f"https://ex.com/{id_}", title="t",
                        snippet="snippet", raw_content="固态电池将于2027年量产。")


def _turn(*calls, content=""):
    tcs = [ToolCall(id=f"c{i}", name=n, arguments_raw="", arguments=a)
           for i, (n, a) in enumerate(calls)]
    am = {"role": "assistant", "content": content}
    if tcs:
        am["tool_calls"] = [{"id": t.id, "type": "function",
                             "function": {"name": t.name, "arguments": "{}"}}
                            for t in tcs]
    return ToolTurn(content=content, tool_calls=tcs, assistant_message=am)


def _script(monkeypatch, turns):
    """让 toolloop.call_tools 按序吐 turns；记录每次收到的 messages 快照。"""
    seen = []

    def fake(messages, **kw):
        seen.append([dict(m) for m in messages])
        return turns[min(len(seen) - 1, len(turns) - 1)]

    monkeypatch.setattr(tl, "call_tools", fake)
    monkeypatch.setattr(tl, "_retrieve", lambda q, c, verbose=False: [_doc()])
    return seen


def _cfg(**kw):
    return SubAgentConfig(**kw)


def test_happy_path_search_fetch_save_finish(monkeypatch):
    _script(monkeypatch, [
        _turn(("search", {"query": "q1"})),
        _turn(("fetch_page", {"doc_id": "d1"})),
        _turn(("save_evidence", {"cards": [
            {"claim": "2027 量产", "excerpt_no": 1, "doc_id": "d1"}]})),
        _turn(("finish", {"summary": "已覆盖", "conflicts": [
            {"dimension": "口径", "description": "两源产能数字不同", "severity": "low"}]})),
    ])
    report = tl.run_tool_loop(TASK, _cfg())
    assert report.status == "ok"
    assert report.stop_reason == "sufficient"
    assert len(report.evidence) == 1
    assert report.summary == "已覆盖"
    assert report.conflicts[0].dimension == "口径"
    assert report.tool_calls == 4


def test_budget_exhausted_forced_finish(monkeypatch):
    # 模型永远想继续 search（每次换 query 不触发重复无效）。
    # save_reserve_calls=0 关掉降落保留区，单测「纯预算耗尽」这一条护栏。
    turns = [_turn(("search", {"query": f"q{i}"})) for i in range(99)]
    _script(monkeypatch, turns)
    report = tl.run_tool_loop(TASK, _cfg(max_tool_calls=3, save_reserve_calls=0))
    # M7（2026-07-09）：预算耗尽且**零证据**不再报 "ok"（那会让整轮误判 done），
    # 降级为 "empty" = 覆盖窟窿，orchestrator 据此标 partial。summary 仍说明"为何空"。
    assert report.status == "empty"
    assert report.stop_reason == "tool_budget"
    assert len(report.evidence) == 0
    assert report.tool_calls == 3
    assert "预算" in report.summary or "用尽" in report.summary


def test_clean_finish_zero_evidence_is_empty(monkeypatch):
    # M7：模型规规矩矩 search→finish，但一张证据都没 save → 仍是覆盖窟窿。
    # 即便 finish 干净收工，零证据也必须报 "empty"，不许拿 "ok" 骗上层。
    _script(monkeypatch, [
        _turn(("search", {"query": "q"})),
        _turn(("finish", {"summary": "没找到可用证据"})),
    ])
    report = tl.run_tool_loop(TASK, _cfg())
    assert report.status == "empty"
    assert len(report.evidence) == 0
    assert report.summary == "没找到可用证据"   # finish 的 summary 保留，只有 status 变


def test_deadline_expired_timeout(monkeypatch):
    _script(monkeypatch, [_turn(("search", {"query": "q"}))])
    report = tl.run_tool_loop(TASK, _cfg(), deadline=time.monotonic() - 1)
    assert report.status == "timeout"
    assert report.stop_reason == "timeout"
    assert report.tool_calls == 0


def test_consecutive_invalid_calls_fuse(monkeypatch):
    # 三连文本回复（无 tool_calls）→ 熔断
    _script(monkeypatch, [_turn(content="我觉得不用工具也行")] * 5)
    report = tl.run_tool_loop(TASK, _cfg(max_invalid_calls=3))
    assert report.status == "empty"   # M7：无效调用熔断且零证据 = 覆盖窟窿
    assert report.stop_reason == "no_progress"
    assert "无效" in report.summary or "熔断" in report.summary


def test_llm_exception_degrades_to_failed(monkeypatch):
    def boom(messages, **kw):
        raise RuntimeError("LLM 调用失败")
    monkeypatch.setattr(tl, "call_tools", boom)
    report = tl.run_tool_loop(TASK, _cfg())
    assert report.status == "failed"
    assert report.stop_reason is None
    assert report.evidence == []


def test_save_before_fetch_rejected_but_loop_continues(monkeypatch):
    _script(monkeypatch, [
        _turn(("search", {"query": "q"})),
        _turn(("save_evidence", {"cards": [
            {"claim": "c", "excerpt_no": 1, "doc_id": "d1"}]})),
        _turn(("finish", {"summary": "done"})),
    ])
    report = tl.run_tool_loop(TASK, _cfg())
    assert report.status == "empty"                # M7：save 被拒→零证据→覆盖窟窿
    assert report.evidence == []                   # 没 fetch 就引用 → 拒收
    assert report.summary == "done"


def test_every_tool_call_id_gets_response(monkeypatch):
    # 单条 assistant 消息带 3 个并行 tool_calls，预算只有 1——后 2 个也必须有占位应答。
    # 预算耗尽后不会再发生第二次 LLM 调用，所以不能靠快照观察：
    # 存 messages 的**引用**，loop 原地 append，事后验完整 history。
    multi = _turn(("search", {"query": "q1"}), ("search", {"query": "q2"}),
                  ("search", {"query": "q3"}))
    refs = []

    def fake(messages, **kw):
        refs.append(messages)
        return multi

    monkeypatch.setattr(tl, "call_tools", fake)
    monkeypatch.setattr(tl, "_retrieve", lambda q, c, verbose=False: [_doc()])
    tl.run_tool_loop(TASK, _cfg(max_tool_calls=1, save_reserve_calls=0))
    history = refs[0]
    tool_ids = [m["tool_call_id"] for m in history if m.get("role") == "tool"]
    assert tool_ids == ["c0", "c1", "c2"]   # 每个 id 都有应答（OpenAI 硬契约）
    assert "忽略" in history[-1]["content"]  # 后两个是预算占位应答


def test_reserve_zone_rejects_reads_allows_save(monkeypatch):
    # 降落保留区（方案 A，DEVLOG 2026-07-04 调研）：额度只剩 save_reserve_calls 次时
    # search/fetch_page 拒收（不耗预算、计 strike），save_evidence/finish 放行——
    # 「读还是存」的无限偏读在最后两手被制度性拿走。
    # max=3, reserve=2：只有第 1 次调用自由，第 2、3 次进保留区。
    refs = []
    turns = [
        _turn(("search", {"query": "q1"})),                 # n=1，自由区
        _turn(("fetch_page", {"doc_id": "d1"})),            # 保留区 → 拒收，不耗预算
        _turn(("save_evidence", {"cards": [
            {"claim": "c", "excerpt_no": 1, "doc_id": "d1"}]})),
        _turn(("finish", {"summary": "landed"})),
    ]

    def fake(messages, **kw):
        refs.append(messages)
        return turns[min(len(refs) - 1, len(turns) - 1)]

    monkeypatch.setattr(tl, "call_tools", fake)
    monkeypatch.setattr(tl, "_retrieve", lambda q, c, verbose=False: [_doc()])
    report = tl.run_tool_loop(TASK, _cfg(max_tool_calls=3))
    history = refs[1]
    refusals = [m for m in history if m.get("role") == "tool" and "保留给" in m["content"]]
    assert len(refusals) == 1                       # fetch 在保留区被拒且有解释
    assert report.tool_calls == 3                    # 拒收不耗预算：search+save+finish=3
    assert report.status == "empty"                 # M7：save 入参校验拒→零证据
    assert report.summary == "landed"               # 模型自己 finish 的总结，非代填
    # save 被拒是因为「先读再引」（d1 从未 fetch 成功），但 save 这个动作本身放行了——
    # 保留区限制的是「读」，不豁免 save 的入参校验。
    assert report.evidence == []


def test_reserve_zone_stubborn_reader_fuses(monkeypatch):
    # 顽固读者兜底：保留区里连续硬要 fetch → 拒收计 strike → 熔断退出。
    # 最坏情况 = 现状的 0 张证据，有界不发散。
    turns = [_turn(("search", {"query": "q1"}))] + \
            [_turn(("fetch_page", {"doc_id": "d1"}))] * 9
    _script(monkeypatch, turns)
    report = tl.run_tool_loop(TASK, _cfg(max_tool_calls=3, max_invalid_calls=3))
    assert report.status == "empty"                 # M7：顽固读者熔断→零证据
    assert report.tool_calls == 1                    # 只有 search 真正执行过
    assert "熔断" in report.summary or "无效" in report.summary


def test_parallel_reserve_reads_count_as_one_turn_and_allow_followup(monkeypatch):
    """同一 turn 的并行硬读不能在模型看到拒收提示前耗尽 strike。"""
    refs = []
    turns = [
        _turn(("search", {"query": "q1"})),
        _turn(("fetch_page", {"doc_id": "d1"}),
              ("fetch_page", {"doc_id": "d1"})),
        _turn(("finish", {"summary": "saw reserve warning"})),
    ]

    def fake(messages, **kw):
        refs.append(messages)
        return turns[min(len(refs) - 1, len(turns) - 1)]

    monkeypatch.setattr(tl, "call_tools", fake)
    monkeypatch.setattr(tl, "_retrieve", lambda q, c, verbose=False: [_doc()])

    report = tl.run_tool_loop(
        TASK,
        _cfg(max_tool_calls=3, max_invalid_calls=2, save_reserve_calls=2),
    )

    refusals = [
        message for message in refs[1]
        if message.get("role") == "tool" and "保留给" in message["content"]
    ]
    assert len(refusals) == 2
    assert len(refs) == 3
    assert report.tool_calls == 2
    assert report.summary == "saw reserve warning"


def test_tool_call_events_carry_save_verdicts(monkeypatch):
    # 观测增强（用户实测反馈）：save_evidence 事件必须带结构化验收结果
    # （accepted/rejected/reject_reasons），finish 事件的 result_summary 带模型总结——
    # 否则前端看不到「最后一手 save 到底收没收」。
    from dra import events

    long_quote = "长证据" * 120
    captured = []
    handle = events.subscribe(
        lambda e: captured.append(e) if e["type"] == "subagent_tool_call" else None)
    try:
        _script(monkeypatch, [
            _turn(("search", {"query": "q1"})),
            _turn(("fetch_page", {"doc_id": "d1"})),
            _turn(("save_evidence", {"cards": [
                {"claim": "第一卡", "excerpt_no": 1, "doc_id": "d1"},
                {"claim": "好卡", "excerpt_no": 1, "doc_id": "d1"},
                {"claim": "坏卡", "excerpt_no": 404, "doc_id": "d1"}]})),
            _turn(("finish", {"summary": "已覆盖"})),
        ])
        monkeypatch.setattr(tl, "_retrieve", lambda q, c, verbose=False: [
            RetrievedDoc(
                id="d1", source_url="https://ex.com/d1", title="长文来源",
                snippet="snippet", raw_content=long_quote,
                published_at="2026-08-06T00:00:00+00:00",
            )
        ])
        tl.run_tool_loop(TASK, _cfg())
    finally:
        events.unsubscribe(handle)
    save = [e for e in captured if e["tool"] == "save_evidence"][0]
    assert save["accepted"] == 2 and save["rejected"] == 1
    assert any("excerpt_no" in r for r in save["reject_reasons"])
    assert save["saved_cards"] == [
        {
            "card_no": 1,
            "claim": "第一卡",
            "support_quote": long_quote[:300],
            "quote_truncated": True,
            "source_title": "长文来源",
            "source_url": "https://ex.com/d1",
            "published_at": "2026-08-06T00:00:00+00:00",
        },
        {
            "card_no": 2,
            "claim": "好卡",
            "support_quote": long_quote[:300],
            "quote_truncated": True,
            "source_title": "长文来源",
            "source_url": "https://ex.com/d1",
            "published_at": "2026-08-06T00:00:00+00:00",
        },
    ]
    assert all(card["claim"] != "坏卡" for card in save["saved_cards"])
    fin = [e for e in captured if e["tool"] == "finish"][0]
    assert "已覆盖" in fin["result_summary"]


def test_tool_call_events_carry_source_links(monkeypatch):
    # 前端超链接数据源（2026-07-27）：result_summary 600 字符截断最多恢复 1-2 条,
    # 完整来源必须走结构化附加字段——search 带 links[{title,url}](仅 http(s)),
    # fetch_page 带 url + n_excerpts。旧回放无此字段,前端正则兜底。
    from dra import events

    captured = []
    handle = events.subscribe(
        lambda e: captured.append(e) if e["type"] == "subagent_tool_call" else None)
    try:
        _script(monkeypatch, [
            _turn(("search", {"query": "q1"})),
            _turn(("fetch_page", {"doc_id": "d1"})),
            _turn(("finish", {"summary": "done"})),
        ])
        # 第二篇是 DDG 相对跳转链——不是 http(s),不该出现在 links 里
        monkeypatch.setattr(tl, "_retrieve", lambda q, c, verbose=False: [
            _doc(), RetrievedDoc(id="d2", source_url="/goto?url=x", title="t2",
                                 snippet="s", raw_content="body")])
        tl.run_tool_loop(TASK, _cfg())
    finally:
        events.unsubscribe(handle)
    search = [e for e in captured if e["tool"] == "search"][0]
    assert search["links"] == [{"title": "t", "url": "https://ex.com/d1"}]
    fetch = [e for e in captured if e["tool"] == "fetch_page"][0]
    assert fetch["url"] == "https://ex.com/d1"
    assert isinstance(fetch["n_excerpts"], int) and fetch["n_excerpts"] >= 1


def test_worker_lifecycle_events_carry_plan_node_id(monkeypatch):
    """前端必须能把每个 Worker 精确挂回所属 Plan Node。"""
    from dra import events

    long_summary = "完整总结" * 180
    captured = []
    handle = events.subscribe(
        lambda e: captured.append(e)
        if e["type"] in {"subagent_start", "subagent_done"} else None
    )
    try:
        _script(monkeypatch, [_turn(("finish", {"summary": long_summary}))])
        tl.run_tool_loop(TASK, _cfg())
    finally:
        events.unsubscribe(handle)
    assert [event["type"] for event in captured] == ["subagent_start", "subagent_done"]
    assert all(event["node_id"] == "test" for event in captured)
    assert captured[-1]["summary"] == long_summary[:600]


def test_budget_feedback_appended_to_tool_results(monkeypatch):
    # 结构性观测缺口修复（DEVLOG 2026-07-04 flash 冒烟）：模型数不准自己用了几次调用，
    # 每条工具结果尾部附「已用 N/M」油表，否则 save 会被拖到预算最后一口。
    refs = []

    def fake(messages, **kw):
        refs.append(messages)
        turns = [_turn(("search", {"query": "q1"})), _turn(("finish", {"summary": "s"}))]
        return turns[min(len(refs) - 1, 1)]

    monkeypatch.setattr(tl, "call_tools", fake)
    monkeypatch.setattr(tl, "_retrieve", lambda q, c, verbose=False: [_doc()])
    tl.run_tool_loop(TASK, _cfg())
    search_tools = [m for m in refs[0] if m.get("role") == "tool"]
    finish_tools = [m for m in refs[1] if m.get("role") == "tool"]
    assert "已用 1/12" in search_tools[0]["content"]  # search 结果带油表
    assert "已用 2/12" in finish_tools[-1]["content"]  # finish 应答也带


def test_worker_context_evicts_old_raw_history_after_save(monkeypatch):
    """保存成功后，旧 search/fetch 全文退出；claim 台账与最近完整 save 交换仍保留。"""
    seen = _script(monkeypatch, [
        _turn(("search", {"query": "q1"})),
        _turn(("fetch_page", {"doc_id": "d1"})),
        _turn(("save_evidence", {"cards": [
            {"claim": "2027 年进入量产", "excerpt_no": 1, "doc_id": "d1"}]})),
        _turn(("finish", {"summary": "已覆盖"})),
    ])

    report = tl.run_tool_loop(TASK, _cfg())

    final_input = seen[3]
    rendered = "\n".join(str(message.get("content", "")) for message in final_input)
    assistant_tools = [
        call["function"]["name"]
        for message in final_input if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
    ]
    assistant_call_ids = [
        call["id"]
        for message in final_input if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
    ]
    tool_call_ids = [
        message["tool_call_id"]
        for message in final_input if message.get("role") == "tool"
    ]
    assert report.evidence
    assert "【研究目标】测试目标" in rendered
    assert "【已保存证据台账" in rendered and "2027 年进入量产" in rendered
    assert "固态电池将于2027年量产。" not in rendered
    assert assistant_tools == ["save_evidence"]
    assert assistant_call_ids == tool_call_ids == ["c0"]


def test_open_doc_excerpt_survives_when_recent_exchange_changes(monkeypatch):
    """未保存精读材料由 registry 重新注入，不依赖 fetch 必须是紧邻上一轮。"""
    seen = _script(monkeypatch, [
        _turn(("search", {"query": "q1"})),
        _turn(("fetch_page", {"doc_id": "d1"})),
        _turn(("search", {"query": "q2"})),
        _turn(("save_evidence", {"cards": [
            {"claim": "2027 年进入量产", "excerpt_no": 1, "doc_id": "d1"}]})),
        _turn(("finish", {"summary": "已覆盖"})),
    ])

    report = tl.run_tool_loop(TASK, _cfg())

    save_input = seen[3]
    rendered = "\n".join(str(message.get("content", "")) for message in save_input)
    assistant_tools = [
        call["function"]["name"]
        for message in save_input if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
    ]
    assert "[excerpt_no=1] 固态电池将于2027年量产。" in rendered
    assert "【待处理精读材料" in rendered
    assert assistant_tools == ["search"]
    assert len(report.evidence) == 1


def test_worker_prompt_does_not_limit_unsaved_document_count(monkeypatch):
    """不在提示词里重新引入“最多保留 N 篇未保存文档”的流程约束。"""
    seen = _script(monkeypatch, [
        _turn(("finish", {"summary": "已覆盖"})),
    ])

    tl.run_tool_loop(TASK, _cfg())

    system_prompt = str(seen[0][0].get("content", ""))
    assert "最多保留" not in system_prompt
    assert "未保存的精读文档" not in system_prompt


def test_worker_prompt_requires_context_complete_non_overgeneralized_claims(monkeypatch):
    seen = _script(monkeypatch, [_turn(("finish", {"summary": "done"}))])

    tl.run_tool_loop(TASK, _cfg())

    system_prompt = str(seen[0][0].get("content", ""))
    assert "脱离当前对话仍能独立理解" in system_prompt
    assert "不得把单个" in system_prompt and "提升为普遍规律" in system_prompt
    assert "不要机械拆成失去语境的关键词" in system_prompt


def test_bad_arguments_is_strike(monkeypatch):
    bad = ToolTurn(content="", tool_calls=[
        ToolCall(id="c0", name="search", arguments_raw="not json", arguments=None)],
        assistant_message={"role": "assistant", "content": "", "tool_calls": [
            {"id": "c0", "type": "function",
             "function": {"name": "search", "arguments": "not json"}}]})
    _script(monkeypatch, [bad] * 5)
    report = tl.run_tool_loop(TASK, _cfg(max_invalid_calls=2))
    assert report.status == "empty"   # M7：坏参数 strike 熔断且零证据
    assert "无效" in report.summary or "熔断" in report.summary


def test_toolloop_worker_initial_message_hides_global_evidence_ids(monkeypatch):
    """Worker 使用已验证前置结论，但看不到不能用于本轮工具的全局证据 ID。"""
    seen = _script(monkeypatch, [_turn(("finish", {"summary": "done"}))])
    task = ResearchTask(
        node_id="m-select",
        objective="筛选候选",
        search_query="methods satisfying A B C",
        round_index=1,
        prerequisite_context="采用 A/B/C 作为判定标准",
        prerequisite_evidence_ids=["deadbeef"],
    )

    report = tl.run_tool_loop(task, _cfg())

    user_message = seen[0][1]["content"]
    assert "【研究目标】筛选候选" in user_message
    assert "【已验证的前置范围与结论】采用 A/B/C 作为判定标准" in user_message
    assert "【前置证据 ID】" not in user_message
    assert "deadbeef" not in user_message
    assert "不得静默换定义" in user_message
    assert report.objective == "筛选候选"
    # round_index 和依赖上下文只属于已执行任务，不在 report 中重复持久化。
    assert report.research_task_id == task.id


def test_parallel_failures_in_one_turn_count_single_strike(monkeypatch):
    """同一轮并行发 3 个重复 query 只计 1 次无效——模型要先看到警告才谈得上纠错。

    2026-07-26 真实 run 实证(run 9f2b8383 / sid 49d57fbf):三条拒收在模型看到
    首条警告之前攒满熔断,worker 9 手全 search、零证据下班。保留区分支早就为
    同一问题做过"同轮只计一次"(见 reserve 注释),本修复把该语义推广到全部失败。
    """
    _script(monkeypatch, [
        _turn(("search", {"query": "q1"})),
        _turn(("search", {"query": "q1"}), ("search", {"query": "q1"}),
              ("search", {"query": "q1"})),          # 并行 3 个重复 → 全拒收
        _turn(("finish", {"summary": "收工"})),        # 必须还活着走到这
    ])
    report = tl.run_tool_loop(TASK, _cfg())
    assert report.stop_reason == "sufficient"          # 而不是被一轮熔断成 no_progress
    assert report.tool_calls == 5                      # 重复仍消耗预算槽位,语义不动


def test_three_consecutive_all_fail_turns_still_fuse(monkeypatch):
    """连续 3 轮全无效仍熔断——每轮一计只是给纠错机会,下界不放松。"""
    _script(monkeypatch, [
        _turn(("search", {"query": "q1"})),
        _turn(("search", {"query": "q1"})),            # 轮2 全失败 → 1
        _turn(("search", {"query": "q1"})),            # 轮3 全失败 → 2
        _turn(("search", {"query": "q1"})),            # 轮4 全失败 → 3 → 熔断
        _turn(("finish", {"summary": "不应到达"})),
    ])
    report = tl.run_tool_loop(TASK, _cfg())
    assert report.stop_reason == "no_progress"
    assert report.status == "empty"     # 零证据熔断的诚实降级
