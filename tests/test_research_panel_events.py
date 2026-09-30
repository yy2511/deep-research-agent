"""侧栏所需的真实事件身份与派发起点，无外部模型调用。"""
from types import SimpleNamespace
from dra import simple_research as sr


def test_delegation_start_exposes_goals_before_children_finish(tmp_path, monkeypatch):
    store = sr.RunStore(tmp_path, run_id='r', query='q')
    context = sr.ToolContext(store, sr.SimpleConfig())
    emitted = []
    monkeypatch.setattr(sr.events, 'emit', lambda kind, **data: emitted.append(data))
    goal = '完整目标' * 150
    call = SimpleNamespace(id='parent', name='delegate_research', arguments={'tasks':[{'goal':goal}]}, arguments_raw='{}')
    sr._emit_tool(context, 'delegate_research', False, '', call, turn=1, phase='running')
    assert emitted[0]['display']['goals'] == [goal[:sr._DISPLAY_GOAL_CHARS]]
    assert emitted[0]['display']['async_dispatch'] is True
    assert 'tasks' not in emitted[0]['display']  # manager owns task identity


def test_repeated_delegations_have_distinct_message_scopes(tmp_path, monkeypatch):
    store = sr.RunStore(tmp_path, run_id='r', query='q')
    config = sr.SimpleConfig()
    context = sr.ToolContext(store, config)
    scopes = []
    def loop(*args, **kwargs):
        scopes.append(kwargs['scope'])
        return {'stop_reason':'finished', 'draft':'结果', 'gaps':[]}
    monkeypatch.setattr(sr, 'run_lead_loop', loop)
    manager=sr._create_delegation_manager(store,context,config,sr.Budget(config,None),None)
    context.delegation_manager=manager
    for parent in ('a','b'):
        manager.submit([('目标','上下文')],parent)
    manager.close(cancel=False)
    assert set(scopes) == {'a:sub1', 'b:sub2'}
