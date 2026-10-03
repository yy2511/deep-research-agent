"""只替换模型/检索边界，验证真实主线和收尾按用户消息改向。"""
import json

import pytest

from dra import simple_research as sr
from dra.research_input import ResearchInput
from test_budget_wrapup import _turn, _doc


TEXT = '只查齿科，价格不用比较，重点看软件兼容。'
SCOPE = {'goal': '齿科面扫软件兼容',
         'requirements': [{'id': 'software', 'text': '核对软件兼容', 'kind': 'goal'}],
         'reason': '用户缩小范围，撤销价格比较'}
PLAN = {'items': [{'id': 'price', 'text': '查价格', 'status': 'dropped'},
                  {'id': 'software', 'text': '核对软件兼容', 'status': 'doing'}],
        'scope_update': SCOPE}


@pytest.mark.parametrize('wrapup', [False, True])
def test_pending_steer_blocks_stale_finish_and_changes_final_requirements(tmp_path, wrapup):
    inbox = ResearchInput('r', lambda _: None)
    seen = []
    def model(messages, **_):
        seen.append(messages)
        if len(seen) == 1:
            inbox.accept('u1', TEXT)  # 模型正在请求，回包仍试图按旧要求结束。
            return _turn(('finish_research', {'draft': '旧方向的答案', 'coverage': [], 'gaps': []}))
        assert any(m['role'] == 'user' and TEXT in m.get('content', '') for m in messages)
        return _turn(('finish_research', {'draft': '未取到齿科软件兼容的公开材料，暂不能下结论。',
            'research_plan': PLAN, 'coverage': [{'requirement_id': 'software', 'status': 'unanswered'}],
            'gaps': ['软件兼容材料不足']}))
    original = [sr.Requirement(id='price', text='比较价格')]
    result = sr.run_simple_research('比较面扫和价格', original, user_input=inbox,
        config=sr.SimpleConfig(max_llm_calls=2 if wrapup else 6, max_tool_calls=8),
        run_dir=tmp_path/'run', model_call=model)
    assert len(seen) == 2
    assert result.draft != '旧方向的答案'
    assert result.original_requirements == original
    assert [r.id for r in result.requirements] == ['software']
    assert result.research_coverage == {'software': 'unanswered'}
    assert result.effective_query == SCOPE['goal']
    assert result.scope_history[-1]['user_message_ids'] == ['u1']
    assert inbox.snapshot()['state'] == 'closed'
    ledger = json.loads((tmp_path/'run/run.json').read_text())
    assert ledger['final']['requirements'][0]['id'] == 'software'


def test_scope_validation_is_atomic_and_next_request_sees_effective_requirements(tmp_path):
    from copy import deepcopy
    inbox = ResearchInput('r', lambda _: None)
    inbox.accept('u1', TEXT)
    seen = []
    second = deepcopy(PLAN)
    second['scope_update']['goal'] = '只核对齿科软件兼容事实'
    second['scope_update']['requirements'][0]['text'] = '仅核对软件兼容事实'
    def model(messages, **_):
        seen.append(messages)
        if len(seen) == 1:
            return _turn(('update_research_plan', PLAN), ('update_research_plan', second))
        assert messages[:len(seen[0])] == seen[0]  # 改向保留原有前缀
        effective = next(m['content'] for m in messages if m.get('content', '').startswith('【当前有效任务】'))
        assert '【当前研究目标（已按用户中途补充调整）】只核对齿科软件兼容事实' in effective
        assert '【用户要求】\n- id=software；kind=goal' in effective
        return _turn(('finish_research', {'draft': '暂未找到可靠公开资料。'}))
    result = sr.run_simple_research('面扫价格', [sr.Requirement(id='price', text='比较价格')],
        user_input=inbox, config=sr.SimpleConfig(max_llm_calls=5),
        run_dir=tmp_path/'first', model_call=model)
    assert [scope['revision'] for scope in result.scope_history] == [1, 2]
    assert result.scope_history[0]['previous']['requirements'][0]['id'] == 'price'
    assert result.scope_history[1]['previous']['goal'] == SCOPE['goal']
    assert all(scope['user_message_ids'] == ['u1'] for scope in result.scope_history)
    assert result.stop_reason == 'finished'
    store = sr.open_run(tmp_path/'other', run_id='other', query='原目标')
    context = sr.ToolContext(store, sr.SimpleConfig())
    context.user_input = inbox
    for kind in ['missing_message', 'bad_goal', 'bad_requirement', 'subagent']:
        bad = deepcopy(PLAN)
        context.user_messages = [] if kind == 'missing_message' else inbox.snapshot()['messages']
        if kind == 'bad_goal': bad['scope_update']['goal'] = ''
        if kind == 'bad_requirement': bad['scope_update']['requirements'] = [{'id': 'x'}]
        context.tool_role = 'subagent' if kind == 'subagent' else 'lead'
        assert not context.update_research_plan(bad)[1]
        assert context.plan_items == [] and context.scope_history == []
        assert not store.state['actions']


def test_wait_wakes_for_user_and_lead_can_message_one_child_without_stopping_sibling(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from dra import events, llm
    from dra.research_delegation import DelegationManager
    inbox = ResearchInput('r', lambda _: None)
    waiting, child_entered, updated, finish_children = [threading.Event() for _ in range(4)]
    main_turn = 0
    requests = {}
    log = []
    original_wait = DelegationManager.wait
    def wait(self, *args, **kwargs):
        if kwargs.get('wakeup') is inbox.pending:
            waiting.set()
        return original_wait(self, *args, **kwargs)
    def observe(e):
        log.append(e)
        if e.get('type') == 'delegation_updated' and e.get('kind') == 'message' and e.get('target') == 'sub1':
            updated.set()
    def model(messages, **_):
        nonlocal main_turn
        if '你是研究子 Agent' in messages[0]['content']:
            name = 'A' if 'child-A' in messages[1]['content'] else 'B'
            requests[name] = requests.get(name, 0) + 1
            if name == 'A' and requests[name] == 1:
                child_entered.set(); assert updated.wait(5)
                assert not llm.cancellation_requested()
                return _turn(('send_research_message', {'target': 'parent', 'message': '准备按新范围补查'}))
            if name == 'A':
                assert TEXT in json.dumps(messages, ensure_ascii=False)
                finish_children.set()
            else:
                assert finish_children.wait(5)
                assert not llm.cancellation_requested()
            return _turn(('finish_research', {'draft': f'{name} 已核对', 'gaps': []}))
        main_turn += 1
        if main_turn == 1:
            return _turn(('delegate_research', {'tasks': [{'goal': 'child-A'}, {'goal': 'child-B'}]}))
        if main_turn == 2:
            assert child_entered.wait(2)
            return _turn(('wait_research', {'timeout_s': 30}))
        if not updated.is_set():
            assert TEXT in json.dumps(messages, ensure_ascii=False)
            return _turn(('send_research_message', {'target': 'sub1', 'message': TEXT}))
        if all(s in json.dumps(messages, ensure_ascii=False) for s in ['A 已核对', 'B 已核对']):
            return _turn(('finish_research', {'draft': '依据调整后的范围整理，材料不足处保留缺口。'}))
        return _turn(('wait_research', {'timeout_s': 1}))
    handle = events.subscribe(observe)
    from unittest.mock import patch
    try:
        with patch.object(DelegationManager, 'wait', wait), ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(sr.run_simple_research, '异步改向', user_input=inbox,
                config=sr.SimpleConfig(max_llm_calls=12, max_invalid_turns=6, wall_timeout_s=15),
                run_dir=tmp_path/'run', model_call=model)
            assert waiting.wait(3)
            inbox.accept('u1', TEXT)
            assert updated.wait(2), '不应等满 30 秒，也不应等子任务完成才处理用户消息'
            result = job.result(timeout=8)
    finally:
        updated.set(); finish_children.set(); events.unsubscribe(handle)
    assert result.stop_reason == 'finished'
    terminals = [e for e in log if e.get('type') == 'delegation_updated' and e.get('kind') == 'result']
    assert len(terminals) == 2 and all(e['task']['status'] == 'completed' for e in terminals)


def test_user_input_waits_for_complete_tool_batch(tmp_path):
    inbox = ResearchInput('r', lambda _: None)
    queries, seen = [], []
    def search(q):
        queries.append(q)
        if len(queries) == 1:
            inbox.accept('u1', TEXT)
        return [_doc()]
    def model(messages, **_):
        seen.append(messages)
        if len(seen) == 1:
            return _turn(('search_sources', {'query': 'first'}), ('search_sources', {'query': 'second'}))
        assert queries == ['first', 'second']
        assistant = next(m for m in messages if m.get('tool_calls'))
        ids = {c['id'] for c in assistant['tool_calls']}
        assert ids == {m['tool_call_id'] for m in messages if m['role'] == 'tool'}
        assert TEXT in messages[-1]['content'] and messages[-1]['role'] == 'user'
        return _turn(('finish_research', {'draft': '暂缺可引用材料', 'gaps': [], 'coverage': []}))
    result = sr.run_simple_research('研究', user_input=inbox,
        config=sr.SimpleConfig(max_llm_calls=5, max_tool_calls=8), run_dir=tmp_path/'run',
        model_call=model, search_fn=search)
    assert result.stop_reason == 'finished'
    assert len(seen) == 2
