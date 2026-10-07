"""目标形态回归：**一个入口**——主 Agent 自己会搜/读/存证，也能按需派子 Agent。

用户 2026-09-15 明确：不要拆成两个入口；简单问题主 Agent 自己解决，需要分工时才派。
本文件覆盖真实的派发路径：同一份账本、独立子预算、不存在递归派发，子 Agent 存下的
证据卡可以被主 Agent 直接引用。

2026-09-17：主子异步执行，派发按输入顺序返回任务身份，结果按实际完成顺序通知。
串行假设的 ``Script`` 在并行下会错位（谁先调用不确定），所以并行用例用 ``GoalScript``
按"主线 / 哪个子任务"分发回合。
"""

import json
import threading
import time
from pathlib import Path

from dra import events, llm
from dra import simple_research as sr
from test_simple_research import QUERY, Script, _doc, _turn

SUB_GOAL = "核实同机两个文件能否直接同步"
SUB_CONTEXT = "只需要官方文档口径；范围限定在 sqlite3_rsync 的同机用法。"


def _names(schemas) -> set[str]:
    return {t["function"]["name"] for t in schemas}


def _ledger_actions(run_dir: Path) -> list[dict]:
    return json.loads((run_dir / "run.json").read_text())["actions"]


def _run(tmp_path, script, **overrides):
    settings = {"max_llm_calls": 12, "max_tool_calls": 12, "wall_timeout_s": 60.0, "wrapup_reserve_s": 0}
    settings.update(overrides)
    config = sr.SimpleConfig(**settings)
    run_dir = tmp_path / "run"
    result = sr.run_simple_research(
        QUERY,
        sr.requirements_from_query(QUERY),
        config=config,
        run_dir=run_dir,
        model_call=script,
        search_fn=lambda q: [_doc()],
        ref_resolver=script,
    )
    return result, run_dir


class GoalScript:
    """并行安全的脚本替身：主线与每个子任务各有自己的回合序列。

    - 用 ``messages[0]`` 区分主线与子 Agent（子 Agent 的 system prompt 是 ``_SUBAGENT_SYSTEM``）；
    - 子 Agent 之间用 mission 里的目标标记区分（测试里的 goal 带唯一标记）；
    - 每个序列各自计数，互不干扰，因此不依赖"谁先调用"。
    """

    def __init__(self, lead_turns, sub_turns_by_goal, *, finish=None):
        self.lead_turns = list(lead_turns)
        self.sub = {goal: list(turns) for goal, turns in sub_turns_by_goal.items()}
        self.finish = finish or (
            "finish_research",
            {
                "draft": "主线汇总：两个子任务都已核实 [c1]。",
                "coverage": [{"requirement_id": "goal", "status": "answered"}],
            },
        )
        self._lock = threading.Lock()
        self._lead_index = 0
        self._sub_index: dict[str, int] = {goal: 0 for goal in self.sub}
        self.seen: list[list[dict]] = []
        self.lead_calls: list[list[dict]] = []
        self.on_subagent_call = None  # 可选钩子：并发峰值 / 计时统计用
        self.hits: list[str] = []

    def _goal_of(self, messages) -> str | None:
        text = str(messages[1].get("content", ""))
        for goal in self.sub:
            if goal in text:
                return goal
        return None

    def __call__(self, messages, **kwargs):
        system = str(messages[0].get("content", "")) if messages else ""
        is_subagent = "你是研究子 Agent" in system
        if not is_subagent:
            with self._lock:
                self.seen.append(messages)
                self.lead_calls.append(messages)
                index = self._lead_index
                self._lead_index += 1
            if index < len(self.lead_turns):
                return self.lead_turns[index]
            return _turn(self.finish)
        goal = self._goal_of(messages)
        if self.on_subagent_call is not None:
            self.on_subagent_call(goal)
        with self._lock:
            self.seen.append(messages)
            self.hits.append(str(goal))
            index = self._sub_index.get(str(goal), 0)
            self._sub_index[str(goal)] = index + 1
            turns = self.sub.get(str(goal), [])
        if index < len(turns):
            return turns[index]
        return _turn(
            (
                "finish_research",
                {"draft": f"{goal} 已核实完毕。", "gaps": []},
            )
        )

    def resolve(self, args):
        return args


class SingleChildScript(GoalScript):
    """Preserve the fixture turns, route by role instead of scheduler ordering."""
    def __init__(self, turns):
        dispatches=[i for i,t in enumerate(turns) if t.tool_calls and t.tool_calls[0].name == "delegate_research"]
        if not dispatches:
            super().__init__(turns,{})
            return
        start=dispatches[-1]+1
        end=next((i for i,t in enumerate(turns[start:],start)
                  if t.tool_calls and t.tool_calls[0].name == "finish_research"), len(turns)-1)
        parent=turns[:start]+turns[end+1:]
        finish=(parent[-1].tool_calls[0].name,parent[-1].tool_calls[0].arguments) if len(parent)>start else ("finish_research",{"draft":"主结论","gaps":[]})
        super().__init__(parent, {SUB_GOAL: turns[start:end+1]},finish=finish)


def _card_for(sub_goal: str) -> dict:
    return {"claim": f"{sub_goal} 的结论", "source_ref": "s1", "excerpt_no": 1}


def _sub_turns(sub_goal: str, *, delay_s: float = 0.0):
    """一个子任务的完整回合序列：搜 → 读 → 存证 →（可选延时）→ 交稿。"""

    class _Delay:
        def __init__(self, seconds):
            self.seconds = seconds

    def _sleep(index):
        if delay_s:
            time.sleep(delay_s)
        return _turn(("search_sources", {"query": f"{sub_goal}-{index}"}))

    return [
        _sleep(0),
        _turn(("read_source", {"source_ref": "s1"})),
        _turn(("save_evidence", {"cards": [_card_for(sub_goal)]})),
        _turn(
            (
                "finish_research",
                {"draft": f"{sub_goal} 的发现。", "gaps": []},
            )
        ),
    ]


def _delegation_actions(run_dir: Path) -> list[dict]:
    return [a for a in _ledger_actions(run_dir) if a["action"] == "delegation"]


def _two_task_script(goals, *, delays=None):
    delays = delays or {}
    return GoalScript(
        lead_turns=[
            _turn(
                (
                    "delegate_research",
                    {"tasks": [{"goal": goal, "context": ""} for goal in goals]},
                )
            )
        ],
        sub_turns_by_goal={
            goal: _sub_turns(goal, delay_s=delays.get(goal, 0.0)) for goal in goals
        },
    )


def test_two_subagents_run_in_parallel_and_keep_task_order(tmp_path):
    """并行执行、按 tasks 顺序返回：慢的那个排在第一位，也仍然是第一个结果。"""
    slow, fast = "任务甲", "任务乙"
    script = _two_task_script([slow, fast], delays={slow: 0.3})
    result, run_dir = _run(tmp_path, script)

    by_id = {a["payload"]["delegation_id"]: a["payload"] for a in _delegation_actions(run_dir)}
    assert set(by_id) == {"sub1", "sub2"}, by_id
    assert slow in by_id["sub1"]["goal"] and fast in by_id["sub2"]["goal"]

    # 墙钟证据：两个子任务的执行区间**重叠**（串行时不可能重叠）。
    first, second = by_id["sub1"], by_id["sub2"]
    assert max(first["started_at"], second["started_at"]) < min(
        first["ended_at"], second["ended_at"]
    ), {"sub1": (first["started_at"], first["ended_at"]),
        "sub2": (second["started_at"], second["ended_at"])}

    # 顺序证据：回执里 results 的顺序与 tasks 一致，不随完成先后变化。
    receipt = None
    for messages in script.lead_calls:
        for message in messages:
            content = str(message.get("content") or "")
            if message.get("role") == "tool" and content.startswith('{"accepted": true, "tasks"'):
                receipt = json.loads(content)
    assert receipt is not None, "主线必须收到派发回执"
    assert [row["goal"] for row in receipt["tasks"]] == [slow, fast]
    assert result.delivery_status == "delivered"


def test_parallel_delegation_labels_events_with_delegation_id(tmp_path):
    """并行之后事件必须能归属：子 Agent 的事件带 delegation_id + parent_call_id，主线为空。"""
    captured: list[dict] = []
    handle = events.subscribe(captured.append)
    try:
        script = _two_task_script(["任务甲", "任务乙"])
        _result, run_dir = _run(tmp_path, script)
    finally:
        events.unsubscribe(handle)

    tools = [e for e in captured if e.get("type") == "subagent_tool_call"]
    assert tools, "必须有工具事件"
    sub_events = [e for e in tools if e.get("delegation_id")]
    assert {e["delegation_id"] for e in sub_events} == {"sub1", "sub2"}
    assert all(e.get("parent_call_id") for e in sub_events), "子 Agent 事件要挂回派发它的那次调用"
    lead_events = [e for e in tools if not e.get("delegation_id")]
    assert lead_events and all(e.get("delegation_id") == "" for e in lead_events)
    assert all(e.get("parent_call_id") == "" for e in lead_events)


def test_delegate_event_carries_task_goals_in_display(tmp_path):
    """派发那次事件要带每个子任务的 goal 与 delegation_id。

    前端要靠它给子 Agent 的视图起名（"代价侧核实"那种标题）。不能从 ``args_summary`` 取——
    那份被截断到 400 字符，一次派 3 个任务时后面的 goal 就丢了。所以从**完整回执**里另取一份。
    """
    captured: list[dict] = []
    handle = events.subscribe(captured.append)
    try:
        script = _two_task_script(["任务甲", "任务乙"])
        _result, _run_dir = _run(tmp_path, script)
    finally:
        events.unsubscribe(handle)

    delegated = [
        e for e in captured
        if e.get("type") == "subagent_tool_call"
        and e.get("tool") == "delegate_research"
        and e.get("phase") == "done"
    ]
    assert len(delegated) == 1
    tasks = delegated[0]["display"]["tasks"]
    assert [t["delegation_id"] for t in tasks] == ["sub1", "sub2"]
    assert [t["goal"] for t in tasks] == ["任务甲", "任务乙"]
    assert all(t["status"] for t in tasks)
    assert delegated[0]["display"]["delegation_ids"] == ["sub1", "sub2"]


class _Peak:
    """记录同时在跑的子 Agent 峰值。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def touch(self, seconds: float) -> None:
        with self._lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(seconds)
        finally:
            with self._lock:
                self.active -= 1


def test_parallel_delegation_caps_concurrency_by_runtime(tmp_path):
    """并发上限由运行时给：一次派 5 个任务，同时在跑的最多 3 个，其余排队。"""
    goals = [f"任务{i}" for i in range(5)]
    peak = _Peak()
    script = GoalScript(
        lead_turns=[
            _turn(
                (
                    "delegate_research",
                    {"tasks": [{"goal": goal, "context": ""} for goal in goals]},
                )
            )
        ],
        sub_turns_by_goal={goal: _sub_turns(goal) for goal in goals},
    )
    script.on_subagent_call = lambda _goal: peak.touch(0.05)
    _result, run_dir = _run(tmp_path, script)

    assert peak.peak <= sr._SUBAGENT_MAX_PARALLEL, peak.peak
    assert peak.peak >= 2, "上限是 3，5 个任务必须出现真正的并行"
    assert len(_delegation_actions(run_dir)) == len(goals)


def test_parallel_subagents_do_not_duplicate_card_ids(tmp_path):
    """并发写入账本不能撞号：两个子 Agent 各存两张卡，编号必须唯一。"""
    goals = ["任务甲", "任务乙"]

    def two_cards(goal):
        return [
            _turn(("search_sources", {"query": goal})),
            _turn(("read_source", {"source_ref": "s1"})),
            _turn(
                (
                    "save_evidence",
                    {
                        "cards": [
                            # 使用现役存证合同，不能把并发上下文的来源别名硬编码为同一个 s1。
                            {"claim": f"{goal}-1", "quote": _doc().raw_content},
                            {"claim": f"{goal}-2", "quote": _doc().raw_content},
                        ]
                    },
                )
            ),
            _turn(("finish_research", {"draft": f"{goal} 完成。", "gaps": []})),
        ]

    script = GoalScript(
        lead_turns=[
            _turn(
                (
                    "delegate_research",
                    {"tasks": [{"goal": goal, "context": ""} for goal in goals]},
                )
            )
        ],
        sub_turns_by_goal={goal: two_cards(goal) for goal in goals},
    )
    result, _run_dir = _run(tmp_path, script)

    assert len(result.cards) == 4, [c.card_id for c in result.cards]
    assert len({c.card_id for c in result.cards}) == 4, "并发保存的卡片编号必须唯一"
    assert sorted(c.created_by for c in result.cards) == ["subagent"] * 4


def test_cancellation_reaches_worker_threads(tmp_path):
    """取消必须传进 worker 线程：contextvars 不显式拷贝的话，子 Agent 里取消失效。"""
    cancel = threading.Event()
    script = _two_task_script(["任务甲", "任务乙"])
    seen_calls: list[int] = []

    def hook(_goal):
        # 子 Agent 跑到一半时置位取消：只有上下文真的传进去了，它才会在下一个检查点停。
        time.sleep(0.1)
        cancel.set()
        seen_calls.append(1)

    script.on_subagent_call = hook
    with llm.execution_scope(cancel_event=cancel):
        result, run_dir = _run(tmp_path, script)

    assert result.stop_reason == "cancelled", result.stop_reason
    # 两个子任务都登记了（都真的跑过），且没有任何一个把 4 回合的序列跑完。
    assert len(_delegation_actions(run_dir)) == 2
    assert max(script._sub_index.values()) <= 2, script._sub_index


def test_lead_can_delegate_and_subagent_cannot():
    """主 Agent 的工具表既有自己的活、也有派发；子 Agent 拿不到派发（无递归）。"""
    assert "delegate_research" in _names(sr.LEAD_TOOL_SCHEMAS)
    assert {"search_sources", "read_source", "save_evidence"} <= _names(sr.LEAD_TOOL_SCHEMAS)
    assert "delegate_research" not in _names(sr.TOOL_SCHEMAS)
    # 子 Agent 的额度沿用既有 Worker 策略（config.adapter = SubAgentConfig），
    # 不另造 subagent_* 字段：12 次工具、最后 2 次只放行存证/收工、3 次无效轮次熔断。
    adapter = sr.SimpleConfig().adapter
    assert (adapter.max_tool_calls, adapter.save_reserve_calls) == (12, 2)
    assert adapter.max_invalid_calls == 3
    assert not [n for n in sr.SimpleConfig.model_fields if n.startswith("subagent_max")]


def test_delegate_takes_goal_and_context_not_a_bare_question():
    """参数形状是结构约束：子任务必须写 goal（目标）与 context（背景），不给"只扔一个问题"的口子。

    依据见 docs/multi-agent-research-harness-review.md §13.4：Hermes 与 CrewAI 都用
    goal/question + context 两个字段逼模型说清子 Agent 需要知道什么。
    """
    item = sr.DELEGATE_SCHEMA["function"]["parameters"]["properties"]["tasks"]["items"]
    assert set(item["properties"]) == {"goal", "context"}
    assert set(item["required"]) == {"goal", "context"}
    assert "question" not in item["properties"]


def test_delegate_without_goal_is_rejected_and_empty_context_is_allowed(tmp_path):
    """没有 goal 的派发如实拒收；context 为空字符串是合法调用（真的没有额外背景）。"""
    script = SingleChildScript(
        [
            _turn(("delegate_research", {"tasks": [{"context": "有背景没目标"}]})),
            _turn(
                (
                    "delegate_research",
                    {"tasks": [{"goal": SUB_GOAL, "context": ""}]},
                )
            ),
            _turn(("search_sources", {"query": "sqlite3_rsync 同机"})),
            _turn(
                (
                    "finish_research",
                    {
                        "draft": "没有启用派发 [c1]。",
                        "coverage": [{"requirement_id": "goal", "status": "partial"}],
                    },
                )
            ),
            _turn(
                (
                    "finish_research",
                    {
                        "draft": "同机两文件可同步 [c1]。",
                        "coverage": [{"requirement_id": "goal", "status": "answered"}],
                    },
                )
            ),
        ]
    )
    result, run_dir = _run(tmp_path, script)
    actions = [
        action["payload"]
        for action in _ledger_actions(run_dir)
        if action["action"] == "tool_call"
        and action["payload"].get("tool") == "delegate_research"
    ]
    rejected = [p for p in actions if p.get("ok") is False]
    assert rejected, "缺 goal 必须留下拒收回执"
    assert "goal" in rejected[0]["reason"]
    accepted = [p for p in actions if p.get("ok") is True]
    assert accepted and accepted[0]["delegated"] == [SUB_GOAL], accepted
    assert result.delivery_status in {"delivered", "limited"}


def test_delegated_findings_are_citable_from_the_same_ledger(tmp_path):
    """派发一个子问题：子 Agent 自己搜、读、存证；主 Agent 直接引用它存的卡交卷。"""
    script = SingleChildScript(
        [
            # 主 Agent：派发
            _turn(
                (
                    "delegate_research",
                    {"tasks": [{"goal": SUB_GOAL, "context": SUB_CONTEXT}]},
                )
            ),
            # 子 Agent：搜 → 读 → 存证 → 交结论（4 轮，正好等于默认回合上限）
            _turn(("search_sources", {"query": "sqlite3_rsync 同机"})),
            _turn(("read_source", {"source_ref": "s1"})),
            _turn(
                (
                    "save_evidence",
                    {"cards": [{"claim": "同机两文件可同步", "source_ref": "s1", "excerpt_no": 1}]},
                )
            ),
            _turn(
                (
                    "finish_research",
                    {
                        "draft": "子问题结论：不要求一端为远程，同机两文件也能同步 [c1]。",
                        "gaps": ["未覆盖 Windows 场景"],
                    },
                )
            ),
            # 主 Agent：引用子 Agent 存下的 c1
            _turn(
                (
                    "finish_research",
                    {
                        "draft": "子 Agent 已核实：同机两文件也能同步 [c1]。",
                        "coverage": [{"requirement_id": "goal", "status": "answered"}],
                    },
                )
            ),
        ]
    )
    result, run_dir = _run(tmp_path, script)

    # 子 Agent 存的卡进了同一份账本 → 主 Agent 的 [c1] 引用成立。
    assert [c.card_id for c in result.cards] == ["c1"]
    assert result.cards[0].created_by == "subagent", "卡片要记明是哪一侧存的（此前一律写 lead）"
    assert "[c1]" in result.draft
    assert result.delivery_status == "delivered"
    assert result.research_coverage == {"goal": "answered"}

    # 账本分得清谁花的：子 Agent 的 4 次模型调用单独记 role=subagent。
    roles = [c.role for c in result.calls]
    assert roles.count("subagent") == 4 and roles.count("lead") >= 2, roles

    # 子 Agent 的发现以回执形式交给主 Agent，且明确提示按编号引用。
    receipts = [a for a in _ledger_actions(run_dir) if a["action"] == "tool_call"]
    assert any(r["payload"].get("tool") == "delegate_research" for r in receipts)

    # 账本的幂等键必须带执行者：主线与子 Agent 各交一次稿，两条记录都得留下。
    # 2026-09-16 attempt3 现场：键只带 run_id 时被"首写生效"吞掉，主线实际交付的
    # 9655 字交稿在账本里没有记录，只剩子 Agent 那份。
    finishes = [a for a in _ledger_actions(run_dir) if a["action"] == "finish_requested"]
    assert len(finishes) == 2, [a.get("action_id") for a in finishes]
    assert len({a["action_id"] for a in finishes}) == 2, "主线与子 Agent 不能共用同一个 action_id"
    assert {a["payload"]["draft_chars"] for a in finishes} == {
        len("子问题结论：不要求一端为远程，同机两文件也能同步 [c1]。"),
        len("子 Agent 已核实：同机两文件也能同步 [c1]。"),
    }, [a["payload"] for a in finishes]


def test_subagent_tool_budget_and_reserve_come_from_the_worker_adapter(tmp_path):
    """子 Agent 的工具额度与降落保留区取 adapter 的 Worker 策略：到保留区只放行存证/收工。"""
    from dra.subagent import SubAgentConfig

    script = SingleChildScript(
        [
            _turn(
                (
                    "delegate_research",
                    {"tasks": [{"goal": SUB_GOAL, "context": SUB_CONTEXT}]},
                )
            ),
            _turn(("search_sources", {"query": "a"})),
            _turn(("search_sources", {"query": "b"})),
            _turn(("search_sources", {"query": "c"})),  # 落在子 Agent 自己的保留区里
            _turn(("finish_research", {"draft": "子结论", "gaps": []})),
            _turn(
                (
                    "finish_research",
                    {
                        "draft": "主结论",
                        "coverage": [{"requirement_id": "goal", "status": "partial"}],
                    },
                )
            ),
        ]
    )
    adapter = SubAgentConfig(
        model="test-model",
        provider="test",
        reasoning=False,
        max_tool_calls=3,
        save_reserve_calls=1,
        max_invalid_calls=3,
    )
    result, run_dir = _run(tmp_path, script, adapter=adapter)
    sub_tools = [
        a
        for a in _ledger_actions(run_dir)
        if a["action"] == "tool_call" and ":sub1:" in a["action_id"]
    ]
    accepted = [a for a in sub_tools if a["payload"].get("ok") is True]
    rejected = [a for a in sub_tools if a["payload"].get("ok") is False]
    # 3 次额度、保留区 1 → 最多 2 次真执行；第 3 次被保留区挡下且不耗额度。
    assert len(accepted) == 2, [a["action_id"] for a in accepted]
    assert rejected and "保留区" in str(rejected[0]["payload"].get("reason")), rejected


def test_subagent_wrapup_is_attributed_to_the_subagent_not_the_lead(tmp_path):
    """子 Agent 自己的收尾必须记在它名下（回归：曾硬编码 lead/wrapup + turn 0）。"""
    from dra.subagent import SubAgentConfig

    script = SingleChildScript(
        [
            _turn(
                (
                    "delegate_research",
                    {"tasks": [{"goal": SUB_GOAL, "context": SUB_CONTEXT}]},
                )
            ),
            _turn(("search_sources", {"query": "a"})),
            _turn(("search_sources", {"query": "b"})),  # 用满子 Agent 工具额度 → 子 Agent 收尾
        ]
    )
    adapter = SubAgentConfig(
        model="test-model",
        provider="test",
        reasoning=False,
        max_tool_calls=2,
        save_reserve_calls=0,
        max_invalid_calls=1,
    )
    result, _ = _run(tmp_path, script, adapter=adapter)
    wrapups = [c for c in result.calls if c.step == "wrapup"]
    assert wrapups, [(c.role, c.step) for c in result.calls]
    assert all(c.role == "subagent" for c in wrapups), [(c.role, c.step) for c in result.calls]


def test_empty_task_list_is_rejected_without_spending_subagent_calls(tmp_path):
    """tasks 里没有可执行的 goal：如实拒收，不派发、不产生子 Agent 调用。"""
    script = SingleChildScript(
        [
            _turn(("delegate_research", {"tasks": []})),
            _turn(
                (
                    "finish_research",
                    {
                        "draft": "没有可派发的子问题。",
                        "coverage": [{"requirement_id": "goal", "status": "partial"}],
                    },
                )
            ),
        ]
    )
    result, run_dir = _run(tmp_path, script)
    assert not [c for c in result.calls if c.role == "subagent"]
    rejected = [
        action
        for action in _ledger_actions(run_dir)
        if action["action"] == "tool_call"
        and action["payload"].get("tool") == "delegate_research"
        and action["payload"].get("ok") is False
    ]
    assert rejected, "空 tasks 必须留下一条拒收回执"


def test_subagent_has_its_own_budget_so_the_parent_stays_alive(tmp_path):
    """子 Agent 用自己的额度：主线额度再小也派得出去，而且它花完自己的额度后主线还活着。

    回归 STATUS §4.1.13：共用一份额度时，子 Agent 会把主线吃光，等它回来主线连自己
    的补读都发不出去，母子双方一起被拒。
    """
    script = SingleChildScript(
        [
            _turn(
                (
                    "delegate_research",
                    {"tasks": [{"goal": SUB_GOAL, "context": SUB_CONTEXT}]},
                )
            ),
            # 子 Agent：两次搜索后收尾（第三次搜索会被它自己的工具额度挡下）
            _turn(("search_sources", {"query": "a"})),
            _turn(("search_sources", {"query": "b"})),
            _turn(("search_sources", {"query": "c"})),
            _turn(("finish_research", {"draft": "子结论：查不动了。", "gaps": ["工具额度不足"]})),
            _turn(
                (
                    "finish_research",
                    {
                        "draft": "主结论（沿用子 Agent 的发现）。",
                        "coverage": [{"requirement_id": "goal", "status": "partial"}],
                    },
                )
            ),
        ]
    )
    # 主线模型额度只给 5：如果子 Agent 与主线共用，它根本跑不起来。
    result, run_dir = _run(tmp_path, script, max_llm_calls=5)
    assert [c for c in result.calls if c.role == "subagent"], "子 Agent 必须真的跑起来"
    assert [c for c in result.calls if c.role == "lead"], "它花完自己的额度后，主线仍要能收尾"
    actions = _ledger_actions(run_dir)
    sub_tools = [
        a for a in actions if a["action"] == "tool_call" and ":sub1:" in a["action_id"]
    ]
    assert len([a for a in sub_tools if a["payload"].get("ok") is True]) >= 3
    assert result.delivery_status in {"delivered", "limited"}


def test_inspect_cards_returns_full_quote_only_when_one_card_is_named(tmp_path):
    """引用别人的卡之前要能核对原文：列表视图不带 quote，单卡视图返回完整 quote。

    这是「主 Agent 可以引用子 Agent 存的卡」能成立的前提——它必须能先看到那张卡的原文，
    否则只能照子 Agent 的转述写细节，正是 §4.1.8 那类越界的成因。
    """
    script = SingleChildScript(
        [
            # 检索 → 先读后引：没有 read_source 落盘全文，save_evidence 会被拒收。
            _turn(("search_sources", {"query": "sqlite3_rsync 同机"})),
            _turn(("read_source", {"source_ref": "s1"})),
            _turn(
                (
                    "save_evidence",
                    {"cards": [{"claim": "同机两文件可同步", "source_ref": "s1", "excerpt_no": 1}]},
                )
            ),
            _turn(("inspect_research", {"view": "cards"})),
            _turn(("inspect_research", {"view": "cards", "card_id": "c1"})),
            _turn(("inspect_research", {"view": "cards", "card_id": "c99"})),
            _turn(
                (
                    "finish_research",
                    {
                        "draft": "同机两文件可同步 [c1]。",
                        "coverage": [{"requirement_id": "goal", "status": "answered"}],
                    },
                )
            ),
        ]
    )
    result, _ = _run(tmp_path, script)
    tool_receipts = [
        json.loads(m["content"])
        for m in script.seen[-1]
        if m.get("role") == "tool" and str(m.get("content", "")).startswith("{")
    ]
    listing = next(r for r in tool_receipts if "cards" in r and isinstance(r["cards"], list))
    assert all("quote" not in card for card in listing["cards"]), "列表视图不该带上原文"
    single = next(r for r in tool_receipts if r.get("card_id") == "c1" and r.get("found"))
    assert single["quotes"][0]["quote"] == result.cards[0].quote
    missing = next(r for r in tool_receipts if r.get("card_id") == "c99")
    assert missing["found"] is False
    assert result.delivery_status == "delivered"
