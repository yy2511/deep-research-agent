"""通过网页接口完整跑一次研究（离线）。

这里的 isolated_web / _wait_done 等网页测试夹具被许多测试文件共用。

端到端用例只替换**外部边界**（模型回包、核查回包、检索结果）：确认问题的 HTTP 入口、
RunRegistry 的名额与取消、事件 SSE、simple 结果落盘、来源快照端点、历史读取全部走真实实现。

接线单测（mode 分流、容量、取消等待、resume 拒绝）用明确的边界替身，不冒充端到端。
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from dra import llm, runtime_config, web
from dra.runstore import PlanAttemptStore, RunStore

from test_budget_wrapup import QUERY, Script, _doc, _FINISH_SEQUENCE, _turn


def _config(**overrides):
    from dra.simple_research import SimpleConfig
    from dra.subagent import SubAgentConfig

    base = SimpleConfig(
        adapter=SubAgentConfig(
            model="test-model", provider="test", reasoning=False
        ),
        max_llm_calls=8,
        max_tool_calls=8,
        wall_timeout_s=30.0, wrapup_reserve_s=0,
    )
    return base.model_copy(update=overrides) if overrides else base


@pytest.fixture
def isolated_web(tmp_path, monkeypatch):
    """隔离运行目录与注册表；simple 预算走测试配置。"""
    store = RunStore(tmp_path / "runs")
    registry = web.RunRegistry(store, max_active_runs=1)
    monkeypatch.setattr(web, "STORE", store)
    monkeypatch.setattr(web, "REGISTRY", registry)
    monkeypatch.setattr(web, "PLAN_STORE", PlanAttemptStore(tmp_path / "plans"))
    monkeypatch.setattr(runtime_config, "current", runtime_config._code_defaults)
    # 主线只要子 Agent 配置，走的是独立入口；夹具要跟随 runtime_config 的对外接口。
    monkeypatch.setattr(runtime_config, "to_subagent_config", lambda _: _adapter_config())
    return store, registry


def _adapter_config(provider="test"):
    from dra.subagent import SubAgentConfig
    return SubAgentConfig(model="test-model", provider=provider, reasoning=False)


def _install_boundaries(monkeypatch, *, turns=None):
    """只替换外部边界：模型回包 / 检索结果。

    走的是产品入口 ``research.run_minimal_research``（Web 层真实调用路径），
    因此 simple 配置、运行目录、账本与状态投影都是真实的。
    """
    from dra import research as research_entry

    original = research_entry.run_minimal_research  # 先抓住真入口，避免包装自己
    script = Script(turns or _FINISH_SEQUENCE)
    calls = {"model": 0, "search": 0, "cancel_seen": []}

    def run_minimal(
        query,
        requirements=None,
        *,
        config=None,
        run_id="simple-research",
        checkpoint_dir=None,
        resume=False,
        **kwargs,
    ):
        def model_call(messages, **kw):
            calls["model"] += 1
            # 记录内核侧观察到的取消状态：这是"Event 真的传进研究入口"的证据。
            calls["cancel_seen"].append(llm.cancellation_requested())
            return script(messages, **kw)

        def search_fn(q):
            calls["search"] += 1
            return [_doc()]

        return original(
            query,
            requirements,
            config=config,
            # 开跑前确认过的目标 / 路线 / 假设走这条通路进研究内核；夹具原样透传，
            # 否则"用户确认过的东西研究时看不到"这个缺陷在测试里也看不出来。
            brief_context=kwargs.get("brief_context"),
            run_id=run_id,
            checkpoint_dir=checkpoint_dir,
            resume=resume,
            model_call=model_call,
            search_fn=search_fn,
            ref_resolver=script,
        )

    monkeypatch.setattr(research_entry, "run_minimal_research", run_minimal)
    return script, calls


def _wait_done(client, run_id, timeout=15.0):
    """等 run 收尾（SSE 之外的确定性等待：轮询 meta 状态）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        detail = client.get(f"/api/runs/{run_id}").json()
        status = (detail.get("meta") or {}).get("status")
        if status not in {"running", None}:
            return detail
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} 未在 {timeout}s 内结束")


# ---------------------------------------------------------------------------
# 端到端（只 mock 外部边界）
# ---------------------------------------------------------------------------


class TestSimpleEndToEnd:
    def test_bad_finish_metadata_keeps_answer_and_reports_missing_attachment(self, isolated_web, monkeypatch):
        draft = "sqlite3_rsync 可同步本机数据库文件 [c1]。"
        turns = [*_FINISH_SEQUENCE[:-1], _turn(("finish_research", {
            "draft": draft,
            "coverage": [{"requirement_id": "wrong", "status": "answered"}],
            "gaps": "缺口不是数组",
            "research_plan": "任务清单不是对象",
            "artifacts": [{"path": "/workspace/output/report.pdf", "title": "报告"}],
        }))]
        _, calls = _install_boundaries(monkeypatch, turns=turns)
        # 替身修不好附件，也不再因连续无效被叫停；用调用次数上限让这轮在预算内结束。
        monkeypatch.setattr(runtime_config, "current",
                            lambda: runtime_config._code_defaults().model_copy(update={"call_limits_enabled": True}))
        with TestClient(web.app) as client:
            response = client.post("/api/research", json={"query": QUERY, "mode": "simple"})
            assert response.status_code == 200, response.text
            run_id = response.json()["run_id"]
            detail = _wait_done(client, run_id)
            payload = client.get(f"/api/runs/{run_id}/simple").json()
        assert payload["draft"] == draft and not payload["draft_rejected"]
        assert payload["research_coverage"] == {"goal": "unanswered"}
        assert len(payload["gaps"]) >= 3
        assert payload["artifact_errors"][0]["error_code"] == "workspace_unavailable"
        assert payload["delivery_status"] == "limited" and not payload["artifacts"]
        assert calls["model"] > len(turns), "附件错误须在预算内交回模型修正，有效正文仍须保留"
        assert draft in detail["report_md"] == payload["markdown"]

    def test_normal_delivery_via_http_entry(self, isolated_web, monkeypatch):
        store, registry = isolated_web
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            resp = client.post("/api/research", json={"query": QUERY, "mode": "simple"})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["mode"] == "simple"
            run_id = body["run_id"]

            detail = _wait_done(client, run_id)
            assert detail["meta"]["status"] == "done"

            payload = client.get(f"/api/runs/{run_id}/simple").json()
            assert payload["stop_reason"] == "finished"
            assert payload["delivery_status"] == "delivered"
            assert payload["research_coverage"] == {"goal": "answered"}
            assert payload["draft"] and payload["cards"] and payload["sources"]

            # 四状态与材料都能被历史读取（刷新/回看同一份真相）
            assert detail["report_md"] == payload["markdown"]
            assert detail["executed_tool_calls"] >= 1

    def test_events_show_single_lead_status_not_workers(self, isolated_web, monkeypatch):
        store, registry = isolated_web
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            _wait_done(client, run_id)
            events = [
                json.loads(line)
                for line in client.get(f"/api/runs/{run_id}/events").text.splitlines()
                if line.strip()
            ]
        kinds = [e["type"] for e in events]
        assert "research_status" in kinds
        statuses = [e for e in events if e["type"] == "research_status"]
        assert statuses[0]["phase"] == "starting"
        finished = statuses[-1]
        assert finished["phase"] == "finished"
        assert finished["stop_reason"] == "finished"
        assert finished["delivery"] == "delivered"
        # 零 Worker：不得出现任何 Worker 派发/子代理事件
        for forbidden in (
            "worker_dispatched",
            "subagent_start",
            "subagent_done",
            "task_batch_dispatched",
        ):
            assert forbidden not in kinds, forbidden
        # 允许上限真实显示（来自 simple 配置，不是 Worker 旋钮）
        assert finished["allowed"]["max_llm_calls"] is None
        assert finished["allowed"]["max_tool_calls"] is None

    def test_source_snapshot_endpoint_serves_saved_text_and_blocks_traversal(
        self, isolated_web, monkeypatch
    ):
        store, registry = isolated_web
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            _wait_done(client, run_id)
            payload = client.get(f"/api/runs/{run_id}/simple").json()
            ref = next(s["ref"] for s in payload["sources"] if s["snapshot_kind"] == "full")

            ok = client.get(f"/api/runs/{run_id}/sources/{ref}")
            assert ok.status_code == 200 and ok.text.strip()

            for bad in ("../../etc/passwd", "s1/../../meta.json", "run.json", "s999999"):
                resp = client.get(f"/api/runs/{run_id}/sources/{bad}")
                assert resp.status_code == 404, bad

    def test_rejected_draft_is_not_published_as_answer(self, isolated_web, monkeypatch):
        store, registry = isolated_web
        # 回执指向不存在的卡片：引用门必须拒绝发布
        script = Script(
            [
                _turn(("search_sources", {"query": "q"})),
                _turn(("read_source", {"source_ref": "s1"})),
                _turn(
                    (
                        "save_evidence",
                        {"cards": [{"claim": "c", "source_ref": "s1", "excerpt_no": 1}]},
                    )
                ),
                _turn(("finish_research", {"draft": "断言引用了不存在的卡片 [c9]。"})),
                # 前两次可重交；第三次才进入最终引用门，不依赖被拒 draft 残留。
                _turn(("finish_research", {"draft": "断言引用了不存在的卡片 [c9]。"})),
                _turn(("finish_research", {"draft": "断言引用了不存在的卡片 [c9]。"})),
            ]
        )
        _install_boundaries(monkeypatch, turns=script.turns)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            detail = _wait_done(client, run_id)
            payload = client.get(f"/api/runs/{run_id}/simple").json()
            report = detail["report_md"]
        assert payload["draft_rejected"] is True
        assert payload["delivery_status"] == "limited"
        # 受限交付不能写成"完成"：列表/刷新状态与四状态必须一致
        assert detail["meta"]["status"] == "partial", detail["meta"]["status"]
        assert "断言引用了不存在的卡片" not in (report or ""), "被拒断言不得进入正式正文"

    def test_failed_delivery_maps_to_failed_status(self, isolated_web, monkeypatch):
        """没有任何正文的失败交付，运行状态必须是 failed，而不是 done。"""
        store, registry = isolated_web
        calls = {"n": 0}

        def slow_search(_q):
            calls["n"] += 1
            time.sleep(0.5)
            return [_doc()]

        from dra import research as research_entry

        original = research_entry.run_minimal_research
        script = Script([_turn(("search_sources", {"query": "q"}))])

        def run_minimal(
            query,
            requirements=None,
            *,
            config=None,
            run_id="x",
            checkpoint_dir=None,
            resume=False,
            **kwargs,
        ):
            # 墙钟极短：拉到 deadline，既不交稿也没有核查
            capped = config.model_copy(update={"wall_timeout_s": 0.2, "wrapup_reserve_s": 0})
            return original(
                query,
                requirements,
                config=capped,
                run_id=run_id,
                checkpoint_dir=checkpoint_dir,
                resume=resume,
                model_call=script,
                search_fn=slow_search,
                ref_resolver=script,
            )

        monkeypatch.setattr(research_entry, "run_minimal_research", run_minimal)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            detail = _wait_done(client, run_id)
            payload = client.get(f"/api/runs/{run_id}/simple").json()
        assert payload["delivery_status"] == "failed"
        assert payload["stop_reason"] in {"deadline", "no_draft", "failed"}
        assert detail["meta"]["status"] == "failed", detail["meta"]["status"]


# ---------------------------------------------------------------------------
# 事件归因（零 Worker 不能在界面上被画成 Worker）
# ---------------------------------------------------------------------------


class TestSimpleEventAttribution:
    def test_tool_receipts_are_marked_as_lead_not_worker(self, isolated_web, monkeypatch):
        """simple 内核复用 subagent_tool_call 类型做兼容，但必须带 role/mode 标记。"""
        store, registry = isolated_web
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            _wait_done(client, run_id)
            events = [
                json.loads(line)
                for line in client.get(f"/api/runs/{run_id}/events").text.splitlines()
                if line.strip()
            ]
        receipts = [e for e in events if e["type"] == "subagent_tool_call"]
        assert receipts, "工具回执应当仍然发出（保持协议向后兼容）"
        for evt in receipts:
            assert evt.get("mode") == "simple", evt
            assert evt.get("role") == "lead", evt
            assert evt.get("run_id") == run_id, "工作线程里必须显式绑定 run_id，否则事件会串号"

    def test_simple_result_carries_timestamps_and_materials(self, isolated_web, monkeypatch):
        """四状态 + 起止时间 + 材料：刷新/历史回看据此显示已结束，而不是一直 running。"""
        store, registry = isolated_web
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            _wait_done(client, run_id)
            payload = client.get(f"/api/runs/{run_id}/simple").json()
        assert payload["stop_reason"] and payload["delivery_status"]
        assert payload["research_coverage"] == {"goal": "answered"}
        assert payload["started_at"] and payload["finished_at"], "必须给出起止时间"
        assert payload["finished_at"] >= payload["started_at"]
        assert payload["sources"] and payload["cards"] and payload["draft"]


class TestSourceSnapshotBinding:
    """来源端点必须绑定已登记全文快照，并校验内容未被改动。"""

    def test_unregistered_or_tampered_sources_are_refused(self, isolated_web, monkeypatch):
        store, registry = isolated_web
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            _wait_done(client, run_id)
            payload = client.get(f"/api/runs/{run_id}/simple").json()
            full_ref = next(s["ref"] for s in payload["sources"] if s["snapshot_kind"] == "full")
            assert client.get(f"/api/runs/{run_id}/sources/{full_ref}").status_code == 200

            # 孤立文件：存在但账本里没登记
            orphan = store._dir(run_id) / "sources" / "s999.txt"
            orphan.write_text("孤立文件", encoding="utf-8")
            assert client.get(f"/api/runs/{run_id}/sources/s999").status_code == 404

            # 摘要快照不能当引用正文
            summary = next((s for s in payload["sources"] if s["snapshot_kind"] == "summary"), None)
            if summary:
                assert client.get(f"/api/runs/{run_id}/sources/{summary['ref']}").status_code == 404

            # 正文被改动 → 哈希对不上，拒绝
            body = store._dir(run_id) / "sources" / f"{full_ref}.txt"
            original = body.read_text(encoding="utf-8")
            body.write_text("篡改后的正文", encoding="utf-8")
            assert client.get(f"/api/runs/{run_id}/sources/{full_ref}").status_code == 404
            body.write_text(original, encoding="utf-8")
            assert client.get(f"/api/runs/{run_id}/sources/{full_ref}").status_code == 200


# ---------------------------------------------------------------------------
# 启动前确认投入范围
# ---------------------------------------------------------------------------


class TestSimpleBudgetPreview:
    def test_api_config_exposes_real_simple_budget(self, isolated_web, monkeypatch):
        """启动前能确认投入范围：数字来自服务端实际配置，不是前端手写。"""
        store, registry = isolated_web
        # /api/config 把运行配置原样铺开；这里只要模型设置可序列化，预算摘要走真实 SimpleConfig。
        monkeypatch.setattr(runtime_config, "current", runtime_config._code_defaults)
        with TestClient(web.app) as client:
            body = client.get("/api/config").json()
        budget = body["simple_budget"]
        from dra.simple_research import SimpleConfig
        from dra.subagent import SubAgentConfig

        expected = SimpleConfig(
            adapter=SubAgentConfig(
                model="test-model", provider="test", reasoning=False
            )
        )
        assert budget["max_llm_calls"] == expected.max_llm_calls
        assert budget["max_tool_calls"] == expected.max_tool_calls
        assert budget["wall_timeout_s"] == expected.wall_timeout_s
        assert budget["reserve_wrapup_calls"] == 0
        assert body["simple_model"]["model"] == "test-model"

    def test_api_config_budget_matches_cli_simple_defaults(self, isolated_web, monkeypatch):
        """真实 /api/config 的投入范围与 simple CLI 缺省一致（实测配置=用户入口）。

        对齐契约锚定值：默认次数不限，也不设总时限，与 Web 相同。
        沿用同文件预算预览测试的 settings fixture 姿势：isolated_web 只替换了
        runtime_config 适配函数，current() 仍需可序列化替身。
        """
        import importlib.util

        monkeypatch.setattr(runtime_config, "current", runtime_config._code_defaults)
        spec = importlib.util.spec_from_file_location(
            "run_research_script_cfg", "scripts/run_research.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(runtime_config, "current", runtime_config._code_defaults)
        args = module.parser().parse_args(["--mode", "simple", "某问题"])
        cli_cfg = module.simple_config(args)

        with TestClient(web.app) as client:
            body = client.get("/api/config").json()
        budget = body["simple_budget"]
        assert budget["max_llm_calls"] == cli_cfg.max_llm_calls
        assert budget["max_tool_calls"] == cli_cfg.max_tool_calls
        assert budget["wall_timeout_s"] == cli_cfg.wall_timeout_s
        assert (
            budget["max_llm_calls"],
            budget["max_tool_calls"],
            budget["wall_timeout_s"],
        ) == (None, None, None)

        # 显式极小值原样使用，不放大的行为同样适用 CLI。
        args = module.parser().parse_args(
            ["--mode", "simple", "--max-lead-turns", "1", "某问题"]
        )
        cli_cfg = module.simple_config(args)
        assert cli_cfg.max_llm_calls == 1 and cli_cfg.max_tool_calls == 1


# ---------------------------------------------------------------------------
# 取消与容量
# ---------------------------------------------------------------------------


class TestSimpleCancelAndCapacity:
    def test_cancel_event_reaches_the_research_kernel(self, isolated_web, monkeypatch):
        """取消必须真的传进内核：内核侧要观察到取消并据此立刻停下。

        直接证据：包一层 ``llm.cancellation_requested`` 统计"内核在运行期观察到取消"的
        次数——只有 Event 真的经 ``execution_scope`` 传进研究入口，这个计数才会 > 0。
        """
        store, registry = isolated_web
        release = threading.Event()
        entered = threading.Event()
        script = Script(_FINISH_SEQUENCE)
        model_calls = []

        from dra import research as research_entry

        original = research_entry.run_minimal_research
        original_checked = llm.cancellation_requested
        observed = {"true_in_kernel": 0}

        def counting_checked() -> bool:
            value = original_checked()
            if value:
                observed["true_in_kernel"] += 1
            return value

        def run_minimal(
            query,
            requirements=None,
            *,
            config=None,
            run_id="x",
            checkpoint_dir=None,
            resume=False,
            **kwargs,
        ):
            def model_call(messages, **kw):
                model_calls.append(1)
                if len(model_calls) == 1:
                    entered.set()
                    release.wait(10)  # 第一次回包前等取消
                return script(messages, **kw)

            return original(
                query,
                requirements,
                config=config,
                run_id=run_id,
                checkpoint_dir=checkpoint_dir,
                resume=resume,
                model_call=model_call,
                search_fn=lambda q: [_doc()],
                ref_resolver=script,
            )

        monkeypatch.setattr(research_entry, "run_minimal_research", run_minimal)
        monkeypatch.setattr(llm, "cancellation_requested", counting_checked)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            assert entered.wait(10)
            client.post(f"/api/research/{run_id}/cancel")
            release.set()
            detail = _wait_done(client, run_id)

        assert observed["true_in_kernel"] > 0, "内核运行期从未观察到取消：Event 没传进研究入口"
        assert len(model_calls) == 1, f"取消后不得再发起模型调用：{model_calls}"
        assert detail["meta"]["status"] == "cancelled"

    def test_cancel_during_run_keeps_slot_until_background_exits(self, isolated_web, monkeypatch):
        """取消后名额保持占用：后台仍在跑时第二次启动必须被拒。"""
        store, registry = isolated_web
        release = threading.Event()
        entered = threading.Event()
        script = Script(_FINISH_SEQUENCE)

        from dra import research as research_entry

        original = research_entry.run_minimal_research

        def run_minimal(
            query,
            requirements=None,
            *,
            config=None,
            run_id="x",
            checkpoint_dir=None,
            resume=False,
            **kwargs,
        ):
            entered.set()
            assert release.wait(10), "测试没有放行后台线程"
            return original(
                query,
                requirements,
                config=config,
                run_id=run_id,
                checkpoint_dir=checkpoint_dir,
                resume=resume,
                model_call=script,
                search_fn=lambda q: [_doc()],
                ref_resolver=script,
            )

        monkeypatch.setattr(research_entry, "run_minimal_research", run_minimal)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            assert entered.wait(10), "后台没有进入研究"

            cancel = client.post(f"/api/research/{run_id}/cancel")
            assert cancel.json()["status"] == "cancelling"

            # 关键断言：后台仍未退出 → 名额没释放 → 第二次启动被容量限制拒绝
            second = client.post("/api/research", json={"query": QUERY, "mode": "simple"})
            assert second.status_code == 429, second.text
            assert "避免重复付费" in second.json()["detail"]
            assert registry.active_count() == 1, "后台未退出时名额必须仍被占用"

            release.set()
            _wait_done(client, run_id)
            assert registry.active_count() == 0, "线程退出后名额才释放"

            # 名额释放后可以再启动
            third = client.post("/api/research", json={"query": QUERY, "mode": "simple"})
            assert third.status_code == 200

    def test_cancel_before_start_is_rejected_by_capacity_not_released(
        self, isolated_web, monkeypatch
    ):
        """第一次取消后、后台仍在等待时，第二次启动不能因为"取消过"而提前放行。"""
        store, registry = isolated_web
        release = threading.Event()
        entered = threading.Event()
        script = Script(_FINISH_SEQUENCE)

        from dra import research as research_entry

        original = research_entry.run_minimal_research

        def run_minimal(
            query,
            requirements=None,
            *,
            config=None,
            run_id="x",
            checkpoint_dir=None,
            resume=False,
            **kwargs,
        ):
            entered.set()
            release.wait(10)
            return original(
                query,
                requirements,
                config=config,
                run_id=run_id,
                checkpoint_dir=checkpoint_dir,
                resume=resume,
                model_call=script,
                search_fn=lambda q: [_doc()],
                ref_resolver=script,
            )

        monkeypatch.setattr(research_entry, "run_minimal_research", run_minimal)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            assert entered.wait(10)
            client.post(f"/api/research/{run_id}/cancel")
            assert registry.active_count() == 1, "后台未退出时名额必须仍被占用"
            release.set()
            _wait_done(client, run_id)
            assert registry.active_count() == 0

    def test_simple_resume_is_refused(self, isolated_web, monkeypatch):
        store, registry = isolated_web
        _install_boundaries(monkeypatch)
        with TestClient(web.app) as client:
            run_id = client.post("/api/research", json={"query": QUERY, "mode": "simple"}).json()[
                "run_id"
            ]
            _wait_done(client, run_id)
            resp = client.post(f"/api/research/{run_id}/resume")
        assert resp.status_code in (404, 405)  # 静态站点挂载时未知 POST 返回 405

    def test_simple_mode_rejects_plan_payload(self, isolated_web):
        store, registry = isolated_web
        with TestClient(web.app) as client:
            resp = client.post(
                "/api/research", json={"query": QUERY, "mode": "simple", "plan": {"nodes": []}}
            )
        assert resp.status_code == 422
        assert "plan" in str(resp.json()["detail"])

    def test_missing_mode_without_plan_is_rejected_explicitly(self, isolated_web):
        store, registry = isolated_web
        with TestClient(web.app) as client:
            resp = client.post("/api/research", json={"query": QUERY})
        assert resp.status_code == 422
        assert "mode" in str(resp.json()["detail"])


# ---------------------------------------------------------------------------
# 旧路径不受影响
# ---------------------------------------------------------------------------


class TestShutdownAndCapacity:
    """真实 task.cancel()（不是 API 取消）时，名额与收尾都必须等后台线程结束。"""

    def test_task_cancel_does_not_release_slot_until_worker_exits(self, isolated_web, monkeypatch):
        import asyncio

        store, registry = isolated_web
        entered = threading.Event()
        release = threading.Event()
        script = Script(_FINISH_SEQUENCE)

        from dra import research as research_entry

        original = research_entry.run_minimal_research

        def run_minimal(query, requirements=None, *, config=None, run_id="x",
                        checkpoint_dir=None, resume=False, **kwargs):
            entered.set()
            release.wait(10)
            return original(
                query, requirements, config=config, run_id=run_id, checkpoint_dir=checkpoint_dir,
                resume=resume, model_call=script,
                search_fn=lambda q: [_doc()], ref_resolver=script,
            )

        monkeypatch.setattr(research_entry, "run_minimal_research", run_minimal)

        async def scenario() -> None:
            run_id = registry.start_simple_research(QUERY, config=_config())
            assert await asyncio.to_thread(entered.wait, 10), "后台没有进入研究"
            task = registry.runs[run_id].task
            assert task is not None
            # 模拟服务关停：先置共享 Event（等价于取消路径），再取消协程任务
            registry.runs[run_id].cancel_event.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

            # 线程还没退出 → 名额必须仍被占用（不依赖任何私有收尾结构）
            assert registry.active_count() == 1, "worker 未结束就释放了名额"
            await asyncio.sleep(0.05)
            assert registry.active_count() == 1, "协程被取消后名额被提前释放"

            # 放行 worker：线程真正结束后名额才释放、运行才善终
            release.set()
            for _ in range(100):
                if registry.active_count() == 0:
                    break
                await asyncio.sleep(0.05)
            assert registry.active_count() == 0, "worker 结束后名额仍未释放"
            assert registry.runs[run_id].done is True
            assert store.get_meta(run_id)["status"] == "cancelled"

        asyncio.run(scenario())


class TestFullLlmIoTraceReachesWorker:
    def test_debug_flag_actually_reaches_executor_thread(self, isolated_web, monkeypatch):
        """``full_llm_io`` 是 contextvar：必须显式复制进 executor 线程，否则调试档丢 I/O。

        直接观察工作线程里读到的开关值。这里替换的是**外部边界**（模型/核查/检索），
        并顺带记录该边界执行时的上下文状态——正是真实调用会读到的那个值。
        """
        store, registry = isolated_web
        from dra import llm, research as research_entry

        original = research_entry.run_minimal_research
        script = Script(_FINISH_SEQUENCE)
        seen: list[bool] = []

        def run_minimal(query, requirements=None, *, config=None, run_id="x",
                        checkpoint_dir=None, resume=False, **kwargs):
            def model_call(messages, **kw):
                # 真实调用就是在这个位置读 ``_trace_full_io`` 决定是否记录完整 I/O
                seen.append(llm._trace_full_io.get())
                return script(messages, **kw)

            return original(
                query, requirements, config=config, run_id=run_id, checkpoint_dir=checkpoint_dir,
                resume=resume, model_call=model_call,
                search_fn=lambda q: [_doc()], ref_resolver=script,
            )

        monkeypatch.setattr(research_entry, "run_minimal_research", run_minimal)
        with TestClient(web.app) as client:
            run_id = client.post(
                "/api/research",
                json={"query": QUERY, "mode": "simple", "trace_full_llm_io": True},
            ).json()["run_id"]
            _wait_done(client, run_id)
        assert seen, "模型边界没有被调用"
        assert all(seen), f"调试档开关没有传进 executor 线程：{seen}"


class TestRetiredPlanPath:
    def test_plan_mode_is_rejected(self, isolated_web):
        store, registry = isolated_web
        with TestClient(web.app) as client:
            resp = client.post("/api/research", json={"query": "q", "mode": "plan"})
        assert resp.status_code == 422

    def test_simple_result_endpoint_404_for_non_simple_run(self, isolated_web):
        store, registry = isolated_web
        run_id = store.create_run("旧问题", {"mission": {}, "plan_nodes": []}, {"planner": "p"})
        with TestClient(web.app) as client:
            resp = client.get(f"/api/runs/{run_id}/simple")
        assert resp.status_code == 404
