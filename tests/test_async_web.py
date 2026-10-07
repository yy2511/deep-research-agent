"""HTTP lifecycle tests replace only model/search boundaries; no paid requests."""
import json
import threading
import pytest
from fastapi.testclient import TestClient
from dra import research, web
from test_web_research_e2e import isolated_web, _wait_done
from test_simple_research import _turn, _doc


@pytest.mark.parametrize('cancel', [False, True])
def test_background_tree_keeps_web_slot_and_replays_terminal(isolated_web,monkeypatch,cancel):
    entered=threading.Event();parent_work=threading.Event();release=threading.Event()
    original=research.run_minimal_research
    main=0
    def model(messages,**kwargs):
        nonlocal main
        if '你是研究子 Agent' in messages[0]['content']:
            entered.set();assert release.wait(5)
            return _turn(('finish_research',{'draft':'子任务结果','gaps':[]}))
        main+=1
        if main==1:return _turn(('delegate_research',{'tasks':[{'goal':'后台核对','context':''}]}))
        if main==2:return _turn(('search_sources',{'query':'主线独立搜索'}))
        return _turn(('finish_research',{'draft':'主线整合结果','gaps':[]}))
    def search(q):parent_work.set();return [_doc()]
    def boundary(*args,**kwargs):
        kwargs['config']=kwargs['config'].model_copy(update={'max_llm_calls':10,'wall_timeout_s':15, "wrapup_reserve_s": 0})
        return original(*args,**kwargs,model_call=model,search_fn=search)
    monkeypatch.setattr(research,'run_minimal_research',boundary)
    with TestClient(web.app) as client:
        run=client.post('/api/research',json={'query':'离线异步验收','mode':'simple'}).json()['run_id']
        try:
            assert entered.wait(2) and parent_work.wait(2)
            if cancel:
                assert client.post(f'/api/research/{run}/cancel').json()['status']=='cancelling'
            assert client.post('/api/research',json={'query':'不应抢占运行名额','mode':'simple'}).status_code==429
        finally:
            release.set()
        detail=_wait_done(client,run)
        assert detail['meta']['status']==('cancelled' if cancel else 'partial')
        rows=[json.loads(line) for line in client.get(f'/api/runs/{run}/events').text.splitlines() if line]
        terminal=[e for e in rows if e['type']=='delegation_updated' and e['kind']=='result']
        assert len(terminal)==1
        assert terminal[0]['task']['status']==('cancelled' if cancel else 'completed')
        assert all(e['run_id']==run for e in rows if e['type']=='delegation_updated')
        assert rows.index(terminal[0])<next(i for i,e in enumerate(rows) if e['type']=='research_status' and e['phase']=='finished')
        assert isolated_web[1].active_count()==0
        # Ended runs expose stable replay; no worker writes after release of the slot.
        assert rows==[json.loads(line) for line in client.get(f'/api/runs/{run}/events').text.splitlines() if line]


def test_failed_child_artifacts_and_error_replay_through_http(isolated_web, monkeypatch):
    from test_simple_delegation import GoalScript, _sub_turns
    class Broken(GoalScript):
        def __call__(self,messages,**kwargs):
            if '你是研究子 Agent' in messages[0]['content'] and self._sub_index['broken']>=3:
                raise RuntimeError('Connection error.')
            return super().__call__(messages,**kwargs)
    script=Broken([_turn(('delegate_research',{'tasks':[{'goal':'broken'}]})),
                   *[_turn(('wait_research',{'timeout_s':1})) for _ in range(3)]],{'broken':_sub_turns('broken')})
    original=research.run_minimal_research
    def boundary(*args,**kwargs):
        return original(*args,**kwargs,model_call=script,search_fn=lambda q:[_doc()],ref_resolver=script)
    monkeypatch.setattr(research,'run_minimal_research',boundary)
    with TestClient(web.app) as client:
        run=client.post('/api/research',json={'query':'离线成果保留验收','mode':'simple'}).json()['run_id']
        _wait_done(client,run)
        rows=[json.loads(line) for line in client.get(f'/api/runs/{run}/events').text.splitlines() if line]
        result=next(e for e in rows if e['type']=='delegation_updated' and e['kind']=='result')
        assert result['task']['status']=='failed'
        assert result['result']['card_ids']==['c1']
        assert result['result']['summary_available'] is False
        assert result['result']['error']['message']=='Connection error.'
        replay=[json.loads(line) for line in client.get(f'/api/runs/{run}/events').text.splitlines() if line]
        assert rows==replay
