"""停止研究的行为，以及运行目录不被覆盖、不被重复打开。

覆盖的是**真实故障**，不是字段镜像：

1. 取消到达收尾边界：``finish_research`` 回包与取消同时发生时，不得再进入收尾，
   不得把未发出的请求登记成外部尝试，终态必须反映取消，已提交的来源与卡片保留。
2. 取消后到达的迟到模型回包不产生新证据、不产生完成动作。
3. ``resume=True`` 一律明确拒绝（含不存在/空目录）；已有痕迹（无台账但有原文）
   不得被覆盖；同一目录并发打开只能成功一个；初始化失败不继续外调。

只替换外部边界（模型调用、检索）；核心循环、落盘、占用与状态投影用真实实现。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from dra import llm
from dra import simple_research as sr

from test_budget_wrapup import (  # noqa: F401
    BASE,
    QUERY,
    Script,
    _doc,
    _FINISH_SEQUENCE,
    _TIRE_SEQUENCE,
    _turn,
)


def _cancel_on_finish(script_cls=Script):
    """包一层脚本：模型回包里出现 finish_research 时**立刻**置位取消。"""

    class CancelOnFinish(script_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stop_event = threading.Event()

        def __call__(self, messages, **kwargs):
            turn = super().__call__(messages, **kwargs)
            for call in getattr(turn, "tool_calls", []) or []:
                if call.name == "finish_research":
                    self.stop_event.set()
            return turn

    return CancelOnFinish


def _run_cancelled_on_finish(*, config=None):
    """跑到"finish 回包同时取消"，返回 (结果, 脚本)。"""
    factory = _cancel_on_finish()
    script = factory(_FINISH_SEQUENCE)

    with llm.execution_scope(cancel_event=script.stop_event):
        result = sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            config=config or BASE,
            model_call=script,
            search_fn=lambda q: [_doc()],
            ref_resolver=script,
        )
    return result, script


class TestCancellationAtFinishBoundary:
    """取消优先于收尾：不回包成"已完成"。"""

    def test_finish_response_that_triggers_cancel_does_not_wrap_up(self):
        result, _ = _run_cancelled_on_finish()
        assert not [c for c in result.calls if c.step in {"final-check", "wrapup"}], (
            "取消后不得登记收尾调用"
        )
        assert result.stop_reason == "cancelled", result.stop_reason
        assert result.delivery_status != "delivered", "取消的运行不得标为完整交付"
        assert any("取消" in w for w in result.warnings), result.warnings

    def test_cancellation_keeps_committed_sources_and_cards(self):
        result, _ = _run_cancelled_on_finish()
        assert [s.ref for s in result.sources if s.snapshot_kind == "full"], (
            "取消不得抹掉已落盘的原文"
        )
        assert result.cards, "取消不得抹掉已提交的证据卡"
        artifact = Path(result.artifact_dir)
        assert (artifact / sr.RunStore.filename).exists(), "台账必须保留"
        assert list((artifact / "sources").glob("*.txt")), "原文快照必须保留"

    def test_cancellation_is_recorded_in_ledger_terminal_state(self):
        result, _ = _run_cancelled_on_finish()
        ledger = json.loads(
            (Path(result.artifact_dir) / sr.RunStore.filename).read_text(encoding="utf-8")
        )
        final = ledger.get("final") or {}
        assert final.get("stop_reason") == "cancelled", final.get("stop_reason")
        assert final.get("delivery_status") != "delivered"
        assert ledger.get("finished") is True, "运行仍然正常收尾落盘"

    def test_calls_already_sent_are_accounted_not_hidden(self):
        """取消前真正发出的调用必须留在账本里（已知/未知费用不能被抹掉）。"""
        result, _ = _run_cancelled_on_finish()
        assert result.calls, "已发出的调用必须记账"
        assert all(call.step != "final-check" for call in result.calls)
        # 费用口径：要么有 token，要么明确标 unknown，不允许静默为 0
        for call in result.calls:
            if call.status == "returned":
                continue
            assert call.usage_unknown or call.input_tokens is not None or call.error

    def test_late_turn_does_not_add_actions_or_finish(self):
        """第一次模型回包时就取消：这一轮的工具调用不得执行，也不得进入完成态。"""
        stop = threading.Event()

        class CancelAfterFirstTurn(Script):
            def __call__(self, messages, **kwargs):
                turn = super().__call__(messages, **kwargs)
                stop.set()  # 回包已到，但取消已经置位
                return turn

        script = CancelAfterFirstTurn(
            [
                _turn(("search_sources", {"query": "q"})),
                _turn(("finish_research", {"draft": "迟到的结论 [c1]。"})),
            ]
        )
        verifier_calls: list[int] = []
        with llm.execution_scope(cancel_event=stop):
            result = sr.run_simple_research(
                QUERY,
                sr.requirements_from_query(QUERY),
                config=BASE,
                model_call=script,
                search_fn=lambda q: [_doc()],
                ref_resolver=script,
            )

        ledger = json.loads(
            (Path(result.artifact_dir) / sr.RunStore.filename).read_text(encoding="utf-8")
        )
        tool_actions = [a for a in ledger["actions"] if a["action"] == "tool_call"]
        assert tool_actions == [], "取消后迟到回包里的工具调用不得执行"
        assert not [a for a in ledger["actions"] if a["action"] == "finish_requested"]
        assert verifier_calls == []
        assert result.stop_reason == "cancelled"
        assert result.draft == "", "取消后不得采用迟到回包里的正文"
        assert result.sources == [] or all(s.snapshot_kind != "full" for s in result.sources), (
            "取消后不得新增可引用正文"
        )


class TestRunDirectoryProtection:
    """resume 一律拒绝；已有痕迹不得覆盖；占用原子；初始化失败不外调。"""

    def test_resume_is_refused_even_for_missing_or_empty_directory(self, tmp_path):
        missing = tmp_path / "missing"
        with pytest.raises(sr.IncompatibleRunError) as error:
            sr.open_run(missing, run_id="r", query="q", resume=True)
        assert "拒绝自动恢复" in str(error.value)
        assert not missing.exists(), "被拒绝的 resume 不该顺手建目录"

        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(sr.IncompatibleRunError):
            sr.open_run(empty, run_id="r", query="q", resume=True)
        assert list(empty.iterdir()) == [], "被拒绝的 resume 不该留下任何文件"

    def test_interrupted_directory_without_ledger_is_refused_and_preserved(self, tmp_path):
        """无台账但有原文（中断窗口）：拒绝，且旧原文逐字保留。"""
        directory = tmp_path / "interrupted"
        (directory / "sources").mkdir(parents=True)
        body = directory / "sources" / "s5.txt"
        body.write_text("上一轮已取得的原文", encoding="utf-8")

        with pytest.raises(sr.IncompatibleRunError):
            sr.open_run(directory, run_id="r", query="q")
        assert body.read_text(encoding="utf-8") == "上一轮已取得的原文"
        assert not (directory / "run.json").exists(), "拒绝后不得写入新台账"

    def test_concurrent_open_of_same_directory_admits_only_one(self, tmp_path):
        directory = tmp_path / "race"
        directory.mkdir()
        first = sr.open_run(directory, run_id="a", query="q")
        with pytest.raises(sr.RunDirBusyError):
            sr.open_run(directory, run_id="b", query="q")
        first.release_occupancy()
        # 释放占用后目录里仍有台账，因此后续打开被"已有运行"拒绝而不是再次占用
        with pytest.raises(sr.IncompatibleRunError):
            sr.open_run(directory, run_id="c", query="q")

    def test_threaded_concurrent_open_admits_exactly_one(self, tmp_path):
        """并发打开：恰好一个成功；其余必须在 winner **仍持有占用**时被拒。

        用屏障保证 winner 一直占着标记直到所有竞争者都尝试过，因此这里不允许出现
        "因为 winner 已释放标记而合法拿到 IncompatibleRunError"的误判。
        """
        directory = tmp_path / "threads"
        directory.mkdir()
        competitors = 5
        barrier = threading.Barrier(competitors + 1, timeout=10)
        winner_holding = threading.Event()
        outcomes: list[str] = []
        lock = threading.Lock()

        def winner() -> None:
            store = sr.open_run(directory, run_id="winner", query="q")
            winner_holding.set()
            barrier.wait()  # 等所有竞争者都到齐（此时仍持有占用）
            barrier.wait()  # 等它们全部尝试完
            store.release_occupancy()

        def competitor(index: int) -> None:
            barrier.wait()  # 等 winner 先占住
            assert winner_holding.is_set(), "winner 必须先拿到占用"
            try:
                sr.open_run(directory, run_id=f"c{index}", query="q")
            except sr.RunDirBusyError:
                outcome = "busy"
            except sr.IncompatibleRunError:
                outcome = "incompatible"  # 只可能是 winner 提前释放，测试要抓住这种误判
            else:
                outcome = "ok"
            with lock:
                outcomes.append(outcome)
            barrier.wait()  # 通知 winner 可以释放了

        threads = [threading.Thread(target=winner)]
        threads += [threading.Thread(target=competitor, args=(i,)) for i in range(competitors)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        assert outcomes == ["busy"] * competitors, outcomes
        assert (directory / sr.RunStore.occupancy_file).exists() is False, "释放后不应留标记"

    def test_ledger_exists_before_any_external_call(self, tmp_path):
        """初始化先落盘：第一次外部调用发生时，台账已经在盘上。"""
        directory = tmp_path / "ordering"
        seen_at_first_call: list[bool] = []

        def search(_query):
            seen_at_first_call.append((directory / sr.RunStore.filename).exists())
            return [_doc()]

        sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            config=BASE,
            run_dir=directory,
            model_call=Script(_FINISH_SEQUENCE),
            search_fn=search,
        )
        assert seen_at_first_call == [True], "首次外调前台账必须已落盘"

    def test_initialization_failure_stops_before_any_call(self, tmp_path, monkeypatch):
        """初始化失败：模型、检索、核查三类外调都必须为 0。"""
        directory = tmp_path / "initfail"
        original = sr._atomic_write
        external_calls: list[str] = []

        def failing_write(path, text):
            if Path(path).name == sr.RunStore.filename:
                raise OSError("模拟台账写入失败")
            return original(path, text)

        def model_call(*args, **kwargs):
            external_calls.append("model")
            raise AssertionError("初始化失败后不得发起模型调用")

        def verify_call(*args, **kwargs):
            external_calls.append("verify")
            raise AssertionError("初始化失败后不得发起核查调用")

        def search_fn(*args, **kwargs):
            external_calls.append("search")
            raise AssertionError("初始化失败后不得发起检索调用")

        monkeypatch.setattr(sr, "_atomic_write", failing_write)
        with pytest.raises(OSError):
            sr.run_simple_research(
                QUERY,
                sr.requirements_from_query(QUERY),
                config=BASE,
                run_dir=directory,
                model_call=model_call,
                search_fn=search_fn,
            )
        assert external_calls == [], f"不得有任何外部调用：{external_calls}"
        assert not (directory / sr.RunStore.occupancy_file).exists(), "占用标记必须释放"
        assert not (directory / sr.RunStore.filename).exists(), "失败的初始化不留下台账"


class TestBoundsAreActuallyEnforced:
    """上界确实有界：模型/工具/来源/卡片都不会被突破。"""

    def test_model_and_tool_call_counts_stay_within_config(self):
        config = sr.SimpleConfig(max_llm_calls=6, max_tool_calls=4, wall_timeout_s=60.0, wrapup_reserve_s=0)
        script = Script(_TIRE_SEQUENCE, wrapup_text="收尾")
        result = sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            config=config,
            model_call=script,
            search_fn=lambda q: [_doc()],
            ref_resolver=script,
        )
        assert len(result.calls) <= config.max_llm_calls
        ledger = json.loads(
            (Path(result.artifact_dir) / sr.RunStore.filename).read_text(encoding="utf-8")
        )
        executed = [
            a for a in ledger["actions"] if a["action"] == "tool_call" and a["payload"]["ok"]
        ]
        assert len(executed) <= config.max_tool_calls

    def test_source_reads_are_no_longer_capped(self):
        """来源数量不再有上限（2026-09-16 用户决定）：三次阅读各自落盘，全部可引用。

        原因见 STATUS §4.1.15 / §4.1.16：那个 8 在委派之后成了"母子互抢"的共享池，
        代价是拒绝阅读、削覆盖，而它保护的是不到 100KB 的磁盘。真正的界仍是工具额度
        （每次 read_source 占一次工具调用）。
        """
        docs = [
            _doc(id=f"d{i}", url=f"https://e.org/{i}", raw="s" * 200 + str(i)) for i in range(4)
        ]
        config = sr.SimpleConfig(max_llm_calls=10, max_tool_calls=10, wall_timeout_s=60.0, wrapup_reserve_s=0)
        script = Script(
            [
                _turn(("search_sources", {"query": "q"})),
                _turn(("read_source", {"source_ref": "s1"})),
                _turn(("read_source", {"source_ref": "s2"})),
                _turn(("read_source", {"source_ref": "s3"})),
            ],
            wrapup_text="收尾",
        )
        result = sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            config=config,
            model_call=script,
            search_fn=lambda q: docs,
            ref_resolver=script,
        )
        fulls = [s for s in result.sources if s.snapshot_kind == "full"]
        assert len(fulls) == 3, [s.ref for s in fulls]
        ledger = json.loads(
            (Path(result.artifact_dir) / sr.RunStore.filename).read_text(encoding="utf-8")
        )
        assert not [
            a
            for a in ledger["actions"]
            if a["action"] == "tool_call"
            and not a["payload"]["ok"]
            and "上限" in str(a["payload"].get("reason") or "")
        ], "不应再有来源数量上限造成的拒收"

    def test_cards_have_no_run_level_cap_and_oversized_batch_is_rejected(self):
        """多次单卡调用都能存；单次超批整批拒收（运行级总量上限已移除）。

        历史运行 的真实失败：运行级 60 张被子 Agent 用光后，
        主线已经读到原文的材料连存都存不进去，覆盖因此缺了厂商。
        """
        config = sr.SimpleConfig(
            max_llm_calls=10, max_tool_calls=10, max_cards_per_call=1, wall_timeout_s=60.0, wrapup_reserve_s=0
        )
        script = Script(
            [
                _turn(("search_sources", {"query": "q"})),
                _turn(("read_source", {"source_ref": "s1"})),
                _turn(
                    (
                        "save_evidence",
                        {"cards": [{"claim": "第一张", "source_ref": "s1", "excerpt_no": 1}]},
                    )
                ),
                _turn(
                    (
                        "save_evidence",
                        {"cards": [{"claim": "第二张", "source_ref": "s1", "excerpt_no": 1}]},
                    )
                ),
                _turn(
                    (
                        "save_evidence",
                        {
                            "cards": [
                                {"claim": "超批一", "source_ref": "s1", "excerpt_no": 1},
                                {"claim": "超批二", "source_ref": "s1", "excerpt_no": 1},
                            ]
                        },
                    )
                ),
            ],
            wrapup_text="收尾",
        )
        result = sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            config=config,
            model_call=script,
            search_fn=lambda q: [_doc()],
            ref_resolver=script,
        )
        assert [c.claim for c in result.cards] == ["第一张", "第二张"], [
            c.claim for c in result.cards
        ]
        ledger = json.loads(
            (Path(result.artifact_dir) / sr.RunStore.filename).read_text(encoding="utf-8")
        )
        rejected = [
            a for a in ledger["actions"] if a["action"] == "tool_call" and not a["payload"]["ok"]
        ]
        assert rejected, "单次超批必须显式拒收"
        assert any("单次最多存 1 张卡" in a["payload"]["reason"] for a in rejected), [
            a["payload"]["reason"] for a in rejected
        ]

    def test_empty_finish_draft_is_rejected_instead_of_ending_without_an_answer(self):
        """空 draft 的交稿如实拒收：先前照收，整轮研究就"成功地没有答案"。

        历史运行：主线交了一份带 draft 的 finish，但参数没能解析成
        JSON 对象，`draft` 读成空串 → `finished=True` 而 draft 为空 → 走收尾回合、标
        `no_draft+wrapup`、覆盖记 `unanswered`，还额外花掉预留的收尾调用。
        """
        config = sr.SimpleConfig(max_llm_calls=10, max_tool_calls=10, wall_timeout_s=60.0, wrapup_reserve_s=0)
        script = Script(
            [
                _turn(("search_sources", {"query": "q"})),
                _turn(("finish_research", {})),
                _turn(
                    (
                        "finish_research",
                        {
                            "draft": "答案：同机也能同步。",
                            "coverage": [{"requirement_id": "goal", "status": "answered"}],
                        },
                    )
                ),
            ],
            wrapup_text="收尾",
        )
        result = sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            config=config,
            model_call=script,
            search_fn=lambda q: [_doc()],
            ref_resolver=script,
        )
        assert result.draft.startswith("答案：同机也能同步。"), result.draft
        assert result.stop_reason == "finished", result.stop_reason
        ledger = json.loads(
            (Path(result.artifact_dir) / sr.RunStore.filename).read_text(encoding="utf-8")
        )
        rejected = [a for a in ledger["actions"] if a["action"] == "finish_rejected"]
        assert rejected, "空 draft 的交稿必须落一条显式拒收"
        assert "draft 不能为空" in rejected[0]["payload"]["reason"], rejected[0]["payload"]

    def test_ledger_records_how_many_http_attempts_a_call_used(self):
        """调用账本要记"这次实际发了几次 HTTP"。

        空流/连接重试有没有触发，此前只能靠翻日志猜；``attempts`` 让它变成可核对的事实，
        同时也把"一次预算调用最多两次 HTTP 尝试"这条边界写进账本。
        """
        config = sr.SimpleConfig(max_llm_calls=10, max_tool_calls=10, wall_timeout_s=60.0, wrapup_reserve_s=0)
        retried = _turn(("search_sources", {"query": "q"}))
        retried.attempts = 2  # 模拟：第一次空流 → 换连接重发成功
        result = sr.run_simple_research(
            QUERY,
            sr.requirements_from_query(QUERY),
            config=config,
            model_call=Script([retried], wrapup_text="收尾"),
            search_fn=lambda q: [_doc()],
            ref_resolver=Script([retried], wrapup_text="收尾"),
        )
        assert result.calls[0].attempts == 2, [c.attempts for c in result.calls]
        assert result.calls[0].role == "lead"
