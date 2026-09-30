"""多轮历史的来源隔离、逐字导入和有界上下文；全部离线。"""
import asyncio
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from pathlib import Path

import pytest

from dra import web
from dra.runstore import RunStore as WebStore
from dra.simple_research import RunStore, ToolContext, SimpleConfig, EvidenceCard
from dra.models import RetrievedDoc
from dra.research_history import prepare_context, ResearchHistory, HistoryError
from dra.durable import atomic_json


def history_fixture(tmp_path, *, answer="已有结论 [c1]", status="done"):
    ws = WebStore(tmp_path / "runs")
    parent = ws.create_run("研究日志与备份", None, {"mode": "simple", "source_mode": "web"})
    ps = RunStore(ws._dir(parent), run_id=parent, query="研究日志与备份")
    raw = "前言。\n日志保留 43 天。\n备份单独处理。😀\r\n结尾。"
    src, _ = ps.write_source(RetrievedDoc(title="规则", source_url="https://example.org/rules", raw_content=raw, snippet=""))
    quote = "日志保留 43 天。"
    start = raw.index(quote)
    ps.add_card(EvidenceCard(card_id=ps.next_card_id(), claim="日志保留 43 天", quote=quote,
        source_ref=src.ref, source_url=src.url, location=f"chars={start}-{start+len(quote)}"))
    ps.commit()
    ws.save_simple_result(parent, {"draft": answer, "delivery_status": "delivered", "draft_rejected": False,
        "effective_query": "研究日志与备份", "requirements": [{"id":"r1","text":"只用官方资料","kind":"constraint"}], "gaps": []})
    ws.finalize(parent, status, {})
    return ws, parent, ps, raw


def child_context(ws, parent, text="解释第一点", request="req-1"):
    payload = prepare_context(ws, parent, text, request)
    run_id = ws.create_run(text, None, {"mode":"simple"})
    atomic_json(ws._dir(run_id) / "followup_context.json", payload)
    store = RunStore(ws._dir(run_id), run_id=run_id, query=text)
    return ToolContext(store, SimpleConfig()), payload


def test_initial_context_namespaces_citations_keeps_constraints(tmp_path):
    ws, parent, _, _ = history_fixture(tmp_path)
    ctx, payload = child_context(ws, parent)
    assert "⟦第1轮·c1⟧" in payload["prompt"]
    assert "[c1]" not in payload["prompt"]
    assert "只用官方资料" in payload["prompt"]
    assert payload["request_id"] == "req-1"
    assert ctx.history is not None


def test_long_answer_is_bounded_and_full_answer_can_be_paged(tmp_path):
    answer = "\n\n".join("段落" + str(n) + "内容" * 500 for n in range(80))
    ws, parent, _, _ = history_fixture(tmp_path, answer=answer)
    ctx, payload = child_context(ws, parent)
    assert len(payload["prompt"]) <= 32000
    assert payload["omitted"]
    result, ok = ctx.execute("read_research_history", {"view":"answer","turn":1,"range":"6000-14000"})
    assert ok
    assert json.loads(result)["text"] == answer[6000:14000]


def test_import_quotes_reuses_verification_only_within_one_request(tmp_path, monkeypatch):
    ws, parent, ps, raw = history_fixture(tmp_path)
    first = ps.cards()[0].evidence_quotes()[0]
    quote = "备份单独处理。"
    start = raw.index(quote)
    ps.state["cards"][0]["quotes"] = [first.model_dump(), first.model_copy(update={
        "quote": quote, "location": f"chars={start}-{start+len(quote)}"}).model_dump()]
    ps.commit()
    ctx, payload = child_context(ws, parent)
    tracked = {ws._dir(parent) / name for name in payload["ancestors"][0]["files"]}
    tracked.add(ps.sources_dir / "s1.txt")
    reads = Counter()
    read_bytes = Path.read_bytes
    def counted_read(path):
        if path in tracked:
            reads[path] += 1
        return read_bytes(path)
    monkeypatch.setattr(Path, "read_bytes", counted_read)
    ctx.history.import_card(1, "c1")
    assert reads == {path: 1 for path in tracked}
    card = ctx.store.cards()[0]
    assert card.quote == ps.cards()[0].quote
    assert card.claim == ps.cards()[0].claim
    assert card.origin["run_id"] == parent
    assert ctx.store.read_source_text(card.source_ref) == raw
    assert ctx.read_exposures[card.source_ref] != [(0,len(raw))]
    assert ctx.store.source(card.source_ref).origin["run_id"] == parent
    assert len(card.quotes) == 2
    assert ctx.history.import_card(1, "c1")["card_id"] == card.card_id
    assert reads == {path: 2 for path in tracked}
    ledger = ws._dir(parent) / "run.json"
    ledger.write_text(ledger.read_text() + "\n")
    with pytest.raises(HistoryError, match="已变化"):
        ctx.history.import_card(1, "c1")


def test_parallel_import_is_idempotent(tmp_path):
    ws, parent, _, _ = history_fixture(tmp_path)
    ctx, _ = child_context(ws, parent)
    contexts = [ToolContext(ctx.store, ctx.config), ToolContext(ctx.store, ctx.config)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda c:c.execute("read_research_history", {"view":"cards","turn":1,"card_ids":["c1"]}), contexts))
    assert all(ok for _,ok in rows)
    assert len(ctx.store.cards()) == 1
    assert len(ctx.store.state["sources"]) == 1


def test_new_history_import_rejudged_once_outside_store_lock(tmp_path, monkeypatch):
    from dra import evidence_judge as ej
    ws, parent, ps, _ = history_fixture(tmp_path)
    ps.state["cards"][0]["judgment"] = ej.Judgment(status="complete", relation="supported").model_dump()
    ps.commit()
    ctx, _ = child_context(ws, parent)
    ctx.config.evidence_judge.enabled = True
    seen = []
    def judge(cards, **kwargs):
        with ThreadPoolExecutor(1) as pool:
            # 网络调用期间其他 Agent 仍可读取账本，不能握着写锁等待远端。
            assert pool.submit(ctx.store.cards).result(timeout=1)
        seen.extend(cards)
        return {c["card_id"]: ej.Judgment(status="complete", relation="insufficient") for c in cards}
    monkeypatch.setattr(ej, "judge_cards", judge)
    for _ in range(2):
        out, ok = ctx.execute("read_research_history", {"view":"cards","turn":1,"card_ids":["c1"]})
        assert ok and '"insufficient"' in out
    assert len(seen) == 1 and seen[0]["judgment"]["status"] == "pending"
    assert ctx.store.cards()[0].judgment.relation == "insufficient"


def test_tampered_or_unrelated_history_rejected(tmp_path):
    ws, parent, ps, _ = history_fixture(tmp_path)
    ctx, _ = child_context(ws, parent)
    assert not ctx.execute("read_research_history", {"view":"cards","turn":999,"card_ids":["c1"]})[1]
    assert not ctx.read_source({"source_ref":"h1:../../secret"})[1]
    (ps.sources_dir / "s1.txt").write_text("伪造原文")
    assert not ctx.execute("read_research_history", {"view":"cards","turn":1,"card_ids":["c1"]})[1]
    assert not ctx.store.cards()


def test_historical_source_read_is_scoped_and_not_a_fresh_url_cache(tmp_path):
    ws, parent, _, _ = history_fixture(tmp_path)
    ctx, _ = child_context(ws, parent)
    result, ok = ctx.read_source({"source_ref":"h1:s1","range":"0-8"})
    assert ok, result
    ref = json.loads(result)["source_ref"]
    assert ctx.store.source(ref).origin
    assert ctx.store.persisted_full_ref("https://example.org/rules") is None


def test_refetch_same_version_revalidates_but_changed_page_is_a_new_snapshot(tmp_path):
    ws, parent, _, raw = history_fixture(tmp_path)
    ctx, _ = child_context(ws, parent)
    imported = ctx.history.import_source(1, "s1")
    doc = RetrievedDoc(title="规则", source_url="https://example.org/rules", raw_content=raw, snippet="")
    fresh = ctx._persist_reading(doc)
    assert fresh.snapshot.ref == imported
    assert ctx.store.source(imported).revalidated_at is not None
    changed = ctx._persist_reading(doc.model_copy(update={"raw_content": raw.replace("43", "9")}))
    assert changed.snapshot.ref != imported
    assert ctx.store.read_source_text(imported) == raw


def test_cancelled_draft_is_not_initial_answer(tmp_path):
    ws, parent, _, _ = history_fixture(tmp_path, answer="未交付草稿", status="cancelled")
    _, payload = child_context(ws, parent)
    assert "未交付草稿" not in payload["prompt"]
    assert "cancelled" in payload["prompt"]


def test_constraint_overflow_rejected_before_start(tmp_path):
    ws, parent, _, _ = history_fixture(tmp_path)
    result = ws.read_simple_result(parent)
    result["requirements"] = [{"id":f"r{i}","text":"硬要求"*3000,"kind":"constraint"} for i in range(5)]
    ws.save_simple_result(parent,result)
    with pytest.raises(HistoryError, match="约束"):
        prepare_context(ws,parent,"继续","req-overflow")


def test_followup_durable_idempotency_latest_guard_and_config_freeze(tmp_path, monkeypatch):
    ws, parent, _, _ = history_fixture(tmp_path)
    registry=web.RunRegistry(ws,max_active_runs=2)
    async def exercise():
        release=asyncio.Event()
        async def fake(run_id,*args,**kwargs):
            await release.wait()
            registry._finalize_simple(run_id,"done")
        monkeypatch.setattr(registry,"_run_simple",fake)
        config=SimpleConfig()
        one=registry.start_followup(parent,"req-id","继续",config=config)
        two=registry.start_followup(parent,"req-id","继续",config=config.model_copy(update={"max_llm_calls":2}))
        assert one == two
        meta=ws.get_meta(one)
        assert meta["conversation_id"] == parent and meta["turn_index"] == 2
        assert meta["resolved_config"]["max_llm_calls"] == config.max_llm_calls
        assert (ws._dir(one)/"followup_context.json").exists()
        with pytest.raises(HistoryError):
            registry.start_followup(parent,"req-id","不同内容",config=config)
        with pytest.raises(HistoryError):
            registry.start_followup(parent,"req-other","旧页提交",config=config)
        release.set()
        await registry.runs[one].task
        restarted=web.RunRegistry(ws)
        assert restarted.start_followup(parent,"req-id","继续",config=config) == one
    asyncio.run(exercise())


def test_preparing_orphan_keeps_request_and_marks_failed(tmp_path):
    ws, parent, _, _ = history_fixture(tmp_path)
    meta=ws.get_meta(parent)
    meta.update(status="preparing",request_id="never-reexecute")
    ws._write_meta(parent,meta)
    ws.mark_orphans()
    assert ws.get_meta(parent)["status"] == "failed"
    assert ws.get_meta(parent)["request_id"] == "never-reexecute"


def test_failed_turn_without_result_keeps_last_effective_constraints(tmp_path):
    ws, parent, _, _ = history_fixture(tmp_path)
    context = prepare_context(ws, parent, "继续核查", "failed-req")
    failed = ws.create_run("继续核查", None, {"mode": "simple"}, identity={
        "conversation_id": parent, "parent_run_id": parent, "turn_index": 2})
    atomic_json(ws._dir(failed) / "followup_context.json", context)
    ws.finalize(failed, "failed", None)
    next_context = prepare_context(ws, failed, "再试一次", "retry-req")
    assert next_context["constraints"] == context["constraints"]
    # 即使结果未保存，已原子提交的范围修改仍优先于上一轮要求。
    ledger = RunStore(ws._dir(failed), run_id=failed, query="继续核查")
    ledger.record_action("research_plan", {"scope": {"goal": "只查日志", "requirements": []}})
    ledger.commit()
    assert prepare_context(ws, failed, "继续", "new-req")["constraints"] == []


def test_start_failure_is_terminal_and_same_request_does_not_start_again(tmp_path, monkeypatch):
    ws, parent, _, _ = history_fixture(tmp_path)
    registry = web.RunRegistry(ws)
    class NoLoop:
        def create_task(self, coro):
            raise RuntimeError("cannot schedule")
    monkeypatch.setattr(web.asyncio, "get_running_loop", lambda: NoLoop())
    with pytest.raises(RuntimeError, match="cannot schedule"):
        registry.start_followup(parent, "fail-start", "继续", config=SimpleConfig())
    latest = ws.conversation_turns(parent)[-1]
    assert latest["status"] == "failed"
    assert registry.active_count() == 0
    assert registry.start_followup(parent, "fail-start", "继续", config=SimpleConfig()) == latest["run_id"]
