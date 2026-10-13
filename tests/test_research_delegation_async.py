import threading
import json
import pytest
from dra.research_delegation import DelegationManager


def test_wait_returns_selected_status_without_repeating_delivered_findings(tmp_path):
    from dra import simple_research as sr

    findings = 'UNIQUE_CHILD_FINDINGS:' + '原文依据' * 1500 + ':END_OF_FINDINGS'
    manager = DelegationManager(
        lambda task, cancel: {'findings': findings if task['goal'] == 'first' else 'second result',
                              'gaps': ['待核实的条件'], 'card_ids': ['c1'], 'summary_available': True},
        lambda event: None)
    tasks = manager.submit([('first', ''), ('second', '')], 'dispatch')
    manager.close(cancel=False)

    ctx = sr.ToolContext(sr.RunStore(tmp_path, run_id='wait-delivery', query='q'), sr.SimpleConfig())
    ctx.delegation_manager = manager
    ctx.delegation_messages = []
    sr._receive_delegation_messages(ctx)
    history = []
    for i in range(3):
        rows = manager.wait([tasks[1]['id']], timeout=0)
        assert rows == [{'id': tasks[1]['id'], 'status': 'completed', 'accepts_messages': False}]
        history.append([
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': f'wait{i}'}]},
            {'role': 'tool', 'tool_call_id': f'wait{i}', 'content': json.dumps(rows)}])
        sr._receive_delegation_messages(ctx)
        messages = sr._build_messages([], ctx, [], history, seconds_left=None)
        content = '\n'.join(m.get('content') or '' for m in messages)
        assert content.count(findings) == 1
        assert content.count('second result') == 1

    assert manager.drain('parent') == []
    assert {r['id'] for r in manager.wait(timeout=0)} == {t['id'] for t in tasks}
    result = manager.inspect(tasks[0]['id'])[0]['result']
    assert result['findings'] == findings
    assert result['gaps'] == ['待核实的条件'] and result['card_ids'] == ['c1']


def test_background_result_and_messages_without_waiting_for_sibling():
    release = threading.Event()
    entered = threading.Event()
    writes = []
    def runner(task, cancel):
        if task['goal'] == 'slow':
            entered.set()
            assert release.wait(2)
        return {'findings': task['goal'], 'gaps': [], 'status': 'finished'}
    manager = DelegationManager(runner, writes.append)
    try:
        tasks = manager.submit([('slow', ''), ('fast', '')], 'call1')
        assert entered.wait(1)
        manager.wait(timeout=1)
        assert manager.inspect(tasks[0]['id'])[0]['status'] == 'running'
        assert any(m.get('result', {}).get('findings') == 'fast' for m in manager.drain('parent'))
        manager.send('parent', tasks[0]['id'], 'narrow scope')
        assert manager.drain(tasks[0]['id'])[0]['message'] == 'narrow scope'
        assert manager.drain(tasks[0]['id']) == []
    finally:
        release.set()
        manager.close()
    assert len([w for w in writes if w['kind'] == 'result']) == 2


def test_capacity_is_run_wide_and_dispatch_is_idempotent():
    release = threading.Event()
    manager = DelegationManager(lambda t,c: (release.wait(2) or {}) and {}, lambda e: None, workers=1, queue_limit=1)
    try:
        first = manager.submit([('a',''),('b','')], 'call1')
        assert manager.submit([('a',''),('b','')], 'call1') == first
        with pytest.raises(ValueError, match='capacity'):
            manager.submit([('c','')], 'call2')
        manager.cancel(first[1]['id'], 'unneeded')
        with pytest.raises(ValueError, match='task_closed'):
            manager.send('parent', first[1]['id'], 'late')
    finally:
        release.set()
        manager.close()


def test_failure_isolated_and_terminal_is_once():
    writes=[]
    def runner(t,c):
        raise ValueError('bad source')
    manager=DelegationManager(runner,writes.append)
    task=manager.submit([('a','')], 'call')[0]
    manager.close(cancel=False)
    assert manager.inspect(task['id'])[0]['status']=='failed'
    assert len([w for w in writes if w['kind']=='result'])==1
    with pytest.raises(ValueError, match='task_closed'):
        manager.send('parent',task['id'],'late')


def test_seal_cannot_lose_an_accepted_message():
    release=threading.Event()
    manager=DelegationManager(lambda t,c: (release.wait(2) and {}),lambda e:None)
    try:
        task=manager.submit([('task','')],'call')[0]['id']
        manager.send('parent',task,'new requirement')
        assert not manager.seal(task)
        assert manager.drain(task)[0]['message']=='new requirement'
        assert manager.seal(task)
        with pytest.raises(ValueError,match='task_closed'):
            manager.send('parent',task,'too late')
    finally:
        release.set();manager.close()


def test_real_loop_main_continues_fast_result_arrives_and_slow_child_gets_message(tmp_path):
    import json
    from dra import events, simple_research as sr
    from test_simple_research import _turn, _doc
    parent_work=threading.Event()
    release_slow=threading.Event()
    child_requests=[]
    captured=[]
    lead_turn=0
    sent=False
    fast_findings = 'FAST_RESULT_READY' + ' supporting detail' * 500 + 'FINAL_QUALIFICATION'
    def observe(event):
        captured.append(event)
        if event.get('type')=='delegation_updated' and event.get('kind')=='message' and event.get('target')=='sub2':
            release_slow.set()
    handle=events.subscribe(observe)
    def model(messages,**kw):
        nonlocal lead_turn,sent
        is_child='你是研究子 Agent' in messages[0]['content']
        if is_child:
            if 'slow-child' in messages[1]['content']:
                child_requests.append(messages)
                if len(child_requests)==1:
                    assert release_slow.wait(3), 'parent must run while child is blocked'
                    return _turn(('send_research_message',{'target':'parent','message':'阶段核对完成'}))
                assert '新增核对参数' in json.dumps(messages,ensure_ascii=False)
                return _turn(('finish_research',{'draft':'slow child done','gaps':[]}))
            assert parent_work.wait(3), 'dispatch blocked parent research'
            return _turn(('finish_research',{'draft':fast_findings,'gaps':[]}))
        lead_turn+=1
        if lead_turn==1:
            return _turn(('delegate_research',{'tasks':[{'goal':'fast-child','context':''},{'goal':'slow-child','context':''}]}))
        if lead_turn==2:
            return _turn(('search_sources',{'query':'independent parent work'}))
        text=json.dumps(messages,ensure_ascii=False)
        if 'FAST_RESULT_READY' in text and not sent:
            assert fast_findings in text
            assert not release_slow.is_set()
            sent=True
            return _turn(('send_research_message',{'target':'sub2','message':'新增核对参数'}))
        if 'slow child done' in text:
            return _turn(('finish_research',{'draft':'已整合两个子任务结果。','gaps':[]}))
        return _turn(('wait_research',{'timeout_s':2}))
    def search(query):
        parent_work.set()
        return [_doc()]
    try:
        result=sr.run_simple_research('异步验收',sr.requirements_from_query('异步验收'),
            config=sr.SimpleConfig(max_llm_calls=20,max_tool_calls=12,max_invalid_turns=10,wall_timeout_s=15, wrapup_reserve_s=0),
            run_dir=tmp_path/'run',model_call=model,search_fn=search)
    finally:
        release_slow.set();events.unsubscribe(handle)
    assert sent and len(child_requests)==2
    assert '已整合' in result.draft
    records=json.loads((tmp_path/'run'/'run.json').read_text())['actions']
    terminal=[a['payload'] for a in records if a['action']=='delegation_event' and a['payload']['kind']=='result']
    assert [t['task']['id'] for t in terminal]==['sub1','sub2']
    assert all(t['task']['status']=='completed' for t in terminal)
    delivered=[m for a in records if a['action']=='delegation_event' and a['payload']['kind']=='delivered' for m in a['payload']['message_ids']]
    assert len(delivered)==len(set(delivered))
    calls=[a['action_id'] for a in records if a['action']=='model_call']
    assert len(calls)==len(set(calls))
    assert not any(t.name.startswith('research-child') and t.is_alive() for t in threading.enumerate())


def test_persistence_failure_propagates_and_joins_workers():
    from dra.llm import LLMPersistenceError
    workers=[]
    def runner(t,c):
        workers.append(threading.current_thread())
        return {'findings':'x'}
    def persist(event):
        if event['kind']=='result':raise LLMPersistenceError('disk full')
    manager=DelegationManager(runner,persist)
    manager.submit([('one','')],'call')
    with pytest.raises(LLMPersistenceError,match='disk full'):
        manager.close(cancel=False)
    assert workers and all(not worker.is_alive() for worker in workers)


def test_cancel_running_child_waits_for_actual_exit():
    entered=threading.Event();release=threading.Event()
    def runner(t,c):
        entered.set();assert release.wait(2)
        assert c.is_set()
        return {'findings':'late response'}
    manager=DelegationManager(runner,lambda e:None)
    task=manager.submit([('one','')],'call')[0]['id']
    try:
        assert entered.wait(1)
        assert manager.cancel(task,'not needed')['status']=='cancel_requested'
        assert manager.active()
    finally:
        release.set();manager.close()
    assert manager.inspect(task)[0]['status']=='cancelled'


def test_parent_budget_reserves_wrapup_until_child_is_settled(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from dra import simple_research as sr, llm
    from test_simple_research import _turn
    entered=threading.Event();release=threading.Event();wrapped=threading.Event()
    child_findings = 'CHILD_FINAL' + ' supporting detail' * 500 + 'FINAL_QUALIFICATION'
    def model(messages,**kwargs):
        if '你是研究子 Agent' in messages[0]['content']:
            entered.set();assert release.wait(3)
            return _turn(('finish_research',{'draft':child_findings,'gaps':[]}))
        if [s['function']['name'] for s in kwargs['tools']] == ['finish_research']:
            assert child_findings in str(messages)
            assert str(messages).count('CHILD_FINAL') == 1
            wrapped.set()
            return llm.ToolTurn(content='汇总已落盘的子任务发现。')
        return _turn(('delegate_research',{'tasks':[{'goal':'budget-child','context':''}]}))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(sr.run_simple_research,'test',config=sr.SimpleConfig(max_llm_calls=2,reserve_wrapup_calls=1,wall_timeout_s=10, wrapup_reserve_s=0),run_dir=tmp_path/'run',model_call=model)
        try:
            assert entered.wait(1)
            assert not wrapped.is_set() and not future.done()
        finally:release.set()
        result=future.result(timeout=3)
    assert wrapped.is_set()
    assert len([c for c in result.calls if c.role=='lead'])==2
    assert len([c for c in result.calls if c.step=='wrapup'])==1


def test_recursive_dispatch_is_rejected_even_if_model_invents_tool(tmp_path):
    from dra import simple_research as sr
    from test_simple_delegation import GoalScript
    from test_simple_research import _turn
    import json
    script=GoalScript([_turn(('delegate_research',{'tasks':[{'goal':'child','context':''}]}))],
        {'child':[_turn(('delegate_research',{'tasks':[{'goal':'grandchild','context':''}]})),
                  _turn(('finish_research',{'draft':'done','gaps':[]}))]},finish=('finish_research',{'draft':'done','gaps':[]}))
    sr.run_simple_research('test',config=sr.SimpleConfig(wall_timeout_s=10, wrapup_reserve_s=0),run_dir=tmp_path/'run',model_call=script)
    data=json.loads((tmp_path/'run'/'run.json').read_text())
    tasks={a['payload']['task']['id'] for a in data['actions'] if a['action']=='delegation_event' and 'task' in a['payload']}
    assert tasks=={'sub1'}
    assert any('不允许递归派发' in str(m) for m in script.seen)


def test_deadline_cleanup_is_timeout_not_user_cancellation():
    import time
    entered=threading.Event();release=threading.Event()
    def runner(t,c):
        entered.set();assert release.wait(2)
        return {'status':'deadline','findings':''}
    manager=DelegationManager(runner,lambda e:None)
    task=manager.submit([('one','')],'call')[0]['id']
    assert entered.wait(1)
    manager.deadline=time.monotonic()-1
    release.set();manager.close()
    assert manager.inspect(task)[0]['status']=='timed_out'


def test_repeated_provider_call_ids_do_not_reuse_task_or_budget_identity(tmp_path):
    from dra import simple_research as sr
    from test_simple_delegation import GoalScript
    from test_simple_research import _turn
    import json
    script=GoalScript([_turn(('delegate_research',{'tasks':[{'goal':g,'context':''}]})) for g in ('one','two')],
        {g:[_turn(('finish_research',{'draft':g,'gaps':[]}))] for g in ('one','two')},
        finish=('finish_research',{'draft':'done','gaps':[]}))
    result=sr.run_simple_research('test',config=sr.SimpleConfig(wall_timeout_s=10, wrapup_reserve_s=0),run_dir=tmp_path/'run',model_call=script)
    data=json.loads((tmp_path/'run'/'run.json').read_text())
    tasks=[a['payload']['task'] for a in data['actions'] if a['action']=='delegation_event' and a['payload']['kind']=='result']
    assert {t['id'] for t in tasks}=={'sub1','sub2'}
    assert {t['parent_call_id'] for t in tasks}=={'1:c0','2:c0'}
    children=[c for c in result.calls if c.role=='subagent']
    assert len(children)==2 and len({c.call_id for c in children})==2


def test_research_cutoff_cancels_active_child_and_still_delivers(tmp_path):
    """研究阶段到点时子任务还在跑：不再因「有活跃子任务」挡住交稿，收掉子任务后用交稿回合交卷。"""
    import time
    from dra import simple_research as sr, llm
    from test_simple_research import _turn
    started = time.monotonic()
    def model(messages, **kwargs):
        if '你是研究子 Agent' in messages[0]['content']:
            while not llm.cancellation_requested() and time.monotonic() - started < 5:
                time.sleep(0.02)
            raise llm.LLMDeadlineExceeded('child stopped')
        names = [s['function']['name'] for s in kwargs['tools']]
        if names == ['finish_research']:
            return _turn(('finish_research', {'draft': '按已有材料交稿。', 'gaps': ['子任务未完成']}))
        if 'delegate_research' in str(messages[-1]) or len(messages) > 3:
            return _turn(('finish_research', {'draft': '想提前交稿。', 'gaps': []}))
        return _turn(('delegate_research', {'tasks': [{'goal': 'slow-child', 'context': ''}]}))
    result = sr.run_simple_research('test', config=sr.SimpleConfig(wall_timeout_s=4, wrapup_reserve_s=1.5),
                                    run_dir=tmp_path/'run', model_call=model)
    assert result.stop_reason == 'research_time+wrapup' and result.draft == '按已有材料交稿。'
    assert time.monotonic() - started < 4
    assert not any(t.name.startswith('research-child') and t.is_alive() for t in threading.enumerate())
