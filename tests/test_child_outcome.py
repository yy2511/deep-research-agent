import json
import pytest
from dra import simple_research as sr
from test_simple_delegation import GoalScript, _run, _sub_turns
from test_simple_research import _turn


@pytest.mark.parametrize('saved', [False, True])
def test_child_failure_retains_only_its_cards_and_notifies_parent(tmp_path, saved):
    class Failing(GoalScript):
        def __call__(self, messages, **kwargs):
            if '你是研究子 Agent' in messages[0]['content']:
                goal = self._goal_of(messages)
                if goal == 'broken' and self._sub_index[goal] >= (3 if saved else 0):
                    raise RuntimeError('Connection error.')
            return super().__call__(messages, **kwargs)
    script = Failing(
        [*_sub_turns('parent')[:3], _turn(('delegate_research', {'tasks': [{'goal':'broken'}, {'goal':'healthy'}]})),
         *[_turn(('wait_research', {'timeout_s':1})) for _ in range(4)]],
        {'broken': _sub_turns('broken'), 'healthy': _sub_turns('healthy')})
    _, path = _run(tmp_path, script, max_llm_calls=20, max_tool_calls=20)
    ledger = json.loads((path/'run.json').read_text())
    results = [a['payload'] for a in ledger['actions'] if a['action']=='delegation_event' and a['payload']['kind']=='result']
    bad = next(e for e in results if e['task']['goal']=='broken')
    good = next(e for e in results if e['task']['goal']=='healthy')
    assert bad['task']['status']=='failed'
    assert bad['result']['summary_available'] is False
    assert bad['result']['error']['message']=='Connection error.'
    assert bad['result']['error']['call_id'].startswith('sub1-')
    owned = [c['card_id'] for c in ledger['cards'] if c['claim']=='broken 的结论']
    assert bad['result']['card_ids']==owned
    assert len(owned)==int(saved)
    assert good['result']['summary_available'] is True
    assert good['result']['error'] is None
    assert not set(owned) & set(good['result']['card_ids'])
    notifications = [m['content'] for call in script.lead_calls for m in call if '【Agent 消息】' in str(m.get('content',''))]
    assert any('Connection error.' in m and 'summary_available' in m and 'card_ids' in m for m in notifications)

@pytest.mark.parametrize('reason,expected', [('finished','completed'), ('cancelled','cancelled'), ('request_timeout','timed_out'), ('failed','failed')])
def test_lifecycle_does_not_depend_on_retained_cards(reason, expected):
    from dra.research_delegation import DelegationManager
    events=[]
    manager=DelegationManager(lambda t,c: {'status':reason,'card_ids':['c7'],'summary_available':reason=='finished','findings':'summary' if reason=='finished' else ''}, events.append)
    manager.submit([('task','')],'call')
    manager.close(cancel=False)
    result=events[-1]
    assert result['task']['status']==expected
    assert result['result']['card_ids']==['c7']
    assert result['result']['summary_available']==(reason=='finished')


def test_non_model_exception_after_saving_also_retains_cards(tmp_path, monkeypatch):
    original=sr._build_messages
    def fail_after_save(*args, **kwargs):
        context=next((a for a in args if isinstance(a,sr.ToolContext)),kwargs.get('context'))
        if context and context.tool_role=='subagent' and context.saved_card_ids:
            raise ValueError('context assembly failed')
        return original(*args,**kwargs)
    monkeypatch.setattr(sr,'_build_messages',fail_after_save)
    script=GoalScript([_turn(('delegate_research',{'tasks':[{'goal':'broken'}]})),
                       *[_turn(('wait_research',{'timeout_s':1})) for _ in range(3)]],{'broken':_sub_turns('broken')})
    _,path=_run(tmp_path,script)
    ledger=json.loads((path/'run.json').read_text())
    event=next(a['payload'] for a in ledger['actions'] if a['action']=='delegation_event' and a['payload']['kind']=='result')
    assert event['result']['card_ids']==['c1']
    assert event['result']['error']['stage']=='execution'
    assert event['result']['error']['message']=='context assembly failed'
