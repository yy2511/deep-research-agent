import hashlib,json
from pathlib import Path
from fastapi.testclient import TestClient
import pytest
from dra import web
from dra.runstore import RunStore


@pytest.fixture
def artifacts_web(tmp_path,monkeypatch):
    store=RunStore(tmp_path/'runs');monkeypatch.setattr(web,'STORE',store)
    monkeypatch.setattr(web,'REGISTRY',web.RunRegistry(store))
    rid=store.create_run('q',None,{'mode':'simple'})
    aid='a_'+'1'*32;data=(Path(__file__).parent/'fixtures/interactive-report.html').read_bytes()
    folder=store._dir(rid)/'artifacts'/aid;folder.mkdir(parents=True)
    (folder/'report.html').write_bytes(data)
    record={'artifact_id':aid,'run_id':rid,'filename':'report.html','title':'Report','format':'html','mime':'text/html','size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'storage_path':f'artifacts/{aid}/report.html','requirement_ids':[],'kind':'report'}
    return store,rid,aid,record


def test_artifacts_require_committed_result_and_download_is_immutable(artifacts_web):
    store,rid,aid,record=artifacts_web
    with TestClient(web.app) as client:
        url=f'/api/runs/{rid}/artifacts/{aid}'
        assert client.get(url+'/download').status_code==404
        store.save_simple_result(rid,{'artifacts':[record],'draft_rejected':False,'stop_reason':'finished'})
        listing=client.get(f'/api/runs/{rid}/artifacts').json()
        assert listing['artifacts'][0]['artifact_id']==aid
        response=client.get(url+'/preview')
        assert response.status_code==200 and '<button onclick=' in response.text
        assert 'content-security-policy' not in response.headers
        assert hashlib.sha256(response.content).hexdigest()==record['sha256']
        assert response.headers['x-content-type-options']=='nosniff'
        response=client.get(url+'/download')
        assert response.headers['content-disposition'].startswith('attachment;')
        assert 'content-security-policy' not in response.headers
        assert hashlib.sha256(response.content).hexdigest()==record['sha256']
        (store._dir(rid)/record['storage_path']).write_text('tampered')
        assert client.get(url+'/download').status_code==409


def test_rejected_or_cancelled_results_never_expose_files(artifacts_web):
    store,rid,aid,record=artifacts_web
    with TestClient(web.app) as client:
        for flags in ({'draft_rejected':True},{'stop_reason':'cancelled'}):
            store.save_simple_result(rid,{'artifacts':[record],**flags})
            assert client.get(f'/api/runs/{rid}/artifacts/{aid}/download').status_code==404


def test_execution_logs_are_bounded_and_path_checked(artifacts_web):
    store,rid,_,_=artifacts_web;eid='exec_'+'2'*32
    logs=store._dir(rid)/'execution/logs';logs.mkdir(parents=True)
    (logs/f'{eid}.stdout').write_bytes(b'x'*70000)
    with TestClient(web.app) as client:
        response=client.get(f'/api/runs/{rid}/execution/logs/{eid}?stream=stdout')
        assert response.status_code==200
        assert len(response.json()['text'])==65536 and response.json()['next_cursor']==65536
        assert client.get(f'/api/runs/{rid}/execution/logs/{eid}?stream=../../meta.json').status_code==400
        assert client.get('/api/execution/status').json()['enabled'] is False


@pytest.mark.docker
def test_http_research_generates_files_and_records_commands(tmp_path,monkeypatch):
    from dra import research,runtime_config,simple_research as sr
    from dra.execution import ExecutionConfig
    from dra.evidence_judge import JudgeConfig
    from test_simple_research import Script,_turn,_full_sequence,_doc
    from test_web_research_e2e import _wait_done
    store=RunStore(tmp_path/'runs');registry=web.RunRegistry(store)
    monkeypatch.setattr(web,'STORE',store);monkeypatch.setattr(web,'REGISTRY',registry)
    monkeypatch.setattr(runtime_config,'current',runtime_config._code_defaults)
    monkeypatch.setattr(runtime_config,'to_simple_config',lambda settings,**kw:sr.SimpleConfig(execution=ExecutionConfig(enabled=True),evidence_judge=JudgeConfig(enabled=False),**kw))
    monkeypatch.setenv('DRA_EXECUTION_ENABLED','true')
    script=Script(_full_sequence()+[
        _turn(('write_file',{'path':'/workspace/work/report.md','content':'# 调研结果\n\nsqlite3_rsync 通过 SSH 同步数据库 [c1]。'})),
        _turn(('bash',{'command':'dra-render report --input /workspace/work/report.md --html /workspace/output/report.html --pdf /workspace/output/report.pdf'}))],
        finish=('finish_research',{'draft_file':'/workspace/work/report.md','coverage':[{'requirement_id':'goal','status':'answered'}],
            'artifacts':[{'path':f'/workspace/output/report.{fmt}','title':f'研究报告 {fmt}','kind':'report','requirement_ids':['goal']} for fmt in ('html','pdf')]}))
    original=research.run_minimal_research
    def run(*args,**kwargs):return original(*args,**kwargs,model_call=script,search_fn=lambda q:[_doc()],ref_resolver=script)
    monkeypatch.setattr(research,'run_minimal_research',run)
    with TestClient(web.app) as client:
        response=client.post('/api/research',json={'mode':'simple','query':'测试文件交付'})
        assert response.status_code==200,response.text
        rid=response.json()['run_id'];_wait_done(client,rid,timeout=20)
        payload=client.get(f'/api/runs/{rid}/simple').json()
        assert len(payload['artifacts'])==2,payload
        assert payload['execution_summary']['state']=='released'
        for item in payload['artifacts']:
            response=client.get(f'/api/runs/{rid}/artifacts/{item["artifact_id"]}/download')
            assert hashlib.sha256(response.content).hexdigest()==item['sha256']
        events=[json.loads(line) for line in (store._dir(rid)/'events.jsonl').read_text().splitlines()]
        call=next(e for e in events if e.get('tool')=='bash' and e.get('phase')=='done')
        assert call['display']['exit_code']==0 and call['display']['command'].startswith('dra-render')


@pytest.mark.docker
def test_http_native_html_only_keeps_layout_and_source_links(tmp_path,monkeypatch):
    from dra import research,runtime_config,simple_research as sr
    from dra.execution import ExecutionConfig
    from dra.evidence_judge import JudgeConfig
    from test_simple_research import Script,_turn,_full_sequence,_doc
    from test_web_research_e2e import _wait_done
    page=(Path(__file__).parent/'fixtures/interactive-report.html').read_text()
    store=RunStore(tmp_path/'runs');registry=web.RunRegistry(store)
    monkeypatch.setattr(web,'STORE',store);monkeypatch.setattr(web,'REGISTRY',registry)
    monkeypatch.setattr(runtime_config,'current',runtime_config._code_defaults)
    monkeypatch.setattr(runtime_config,'to_simple_config',lambda settings,**kw:sr.SimpleConfig(
        execution=ExecutionConfig(enabled=True),evidence_judge=JudgeConfig(enabled=False),**kw))
    monkeypatch.setenv('DRA_EXECUTION_ENABLED','true')
    script=Script(_full_sequence()+[
        _turn(('write_file',{'path':'/workspace/output/report.html','content':page})),
    ],finish=('finish_research',{'draft':'sqlite3_rsync 通过 SSH 在主机之间同步数据库 [c1]。',
        'coverage':[{'requirement_id':'goal','status':'answered'}],
        'artifacts':[{'path':'/workspace/output/report.html','title':'SQLite 同步指南','kind':'report','requirement_ids':['goal']}]}))
    original=research.run_minimal_research
    def run(*args,**kwargs):return original(*args,**kwargs,model_call=script,search_fn=lambda q:[_doc()],ref_resolver=script)
    monkeypatch.setattr(research,'run_minimal_research',run)
    query='解释同步方式，交付 HTML 网页'
    brief={'original_query':query,'objective':query,
           'requirements':[{'id':'goal','text':'解释同步方式并交付网页','output_formats':['html']}],
           'route':[{'id':'verify','text':'核对同步关系'}],'needs_confirmation':False,'confirmation_reason':'验收范围明确'}
    with TestClient(web.app) as client:
        response=client.post('/api/research',json={'mode':'simple','query':query,'brief':brief})
        assert response.status_code==200,response.text
        rid=response.json()['run_id'];_wait_done(client,rid,timeout=20)
        result=client.get(f'/api/runs/{rid}/simple').json()
        assert result['delivery_status']=='delivered',result
        assert [a['format'] for a in result['artifacts']]==['html']
        assert result['artifact_errors']==[]
        assert 'output_formats=["html"]' in str(script.seen[0])
        artifact=result['artifacts'][0]
        for action in ('preview','download'):
            response=client.get(f'/api/runs/{rid}/artifacts/{artifact["artifact_id"]}/{action}')
            assert response.status_code==200,response.text
            assert response.content==page.encode()
            assert 'content-security-policy' not in response.headers
        assert not list(store._dir(rid).rglob('*.pdf'))
        assert not any(row['tool']=='bash' for row in script.context.receipts)


@pytest.mark.docker
def test_http_followups_edit_same_workspace_and_old_deliveries_survive_restart(tmp_path,monkeypatch):
    import time
    from dra import research,runtime_config,simple_research as sr
    from dra.execution import ExecutionConfig
    from dra.evidence_judge import JudgeConfig
    from test_simple_research import Script,_turn
    from test_web_research_e2e import _wait_done
    store=RunStore(tmp_path/'runs');registry=web.RunRegistry(store)
    monkeypatch.setattr(web,'STORE',store);monkeypatch.setattr(web,'REGISTRY',registry)
    monkeypatch.setattr(runtime_config,'current',runtime_config._code_defaults)
    monkeypatch.setattr(runtime_config,'to_simple_config',lambda settings,**kw:sr.SimpleConfig(
        execution=ExecutionConfig(enabled=True),evidence_judge=JudgeConfig(enabled=False),**kw))
    monkeypatch.setenv('DRA_EXECUTION_ENABLED','true')
    calls=[];original=research.run_minimal_research
    def body(n): return f'# 测试报告\n\n版本 {n}。'
    def html(n): return f'<!doctype html><html><body><h1>测试报告</h1><p>版本 {n}。</p></body></html>'
    def run(query,requirements=None,**kwargs):
        n=len(calls)+1
        if n==1:
            steps=[_turn(('write_file',{'path':'/workspace/work/report.md','content':body(n)})),
                   _turn(('write_file',{'path':'/workspace/output/report.html','content':html(n)}))]
        else:
            steps=[_turn(('read_file',{'path':'/workspace/work/report.md'}))]
            for path,content in [('/workspace/work/report.md',body),('/workspace/output/report.html',html)]:
                steps.append(_turn(('edit_file',{'path':path,'old_text':f'版本 {n-1}','new_text':f'版本 {n}',
                    'expected_sha256':hashlib.sha256(content(n-1).encode()).hexdigest()})))
        script=Script(steps,finish=('finish_research',{'draft_file':'/workspace/work/report.md',
            'coverage':[{'requirement_id':r.id,'status':'answered'} for r in requirements or sr.requirements_from_query(query)],
            'artifacts':[{'path':'/workspace/output/report.html','title':'测试报告','kind':'report','requirement_ids':[]}]}))
        calls.append(script)
        return original(query,requirements,**kwargs,model_call=script,ref_resolver=script)
    monkeypatch.setattr(research,'run_minimal_research',run)
    delivered=[]
    def collect(client,rid):
        _wait_done(client,rid,timeout=20)
        until=time.monotonic()+3
        while registry.runs[rid].busy and time.monotonic()<until: time.sleep(.01)
        result=client.get(f'/api/runs/{rid}/simple').json()
        assert result['execution_summary']['state']=='released',result
        assert len(result['artifacts'])==1,result
        artifact=result['artifacts'][0]
        url=f'/api/runs/{rid}/artifacts/{artifact["artifact_id"]}/download'
        content=client.get(url).content
        assert content==html(len(calls)).encode()
        delivered.append((url,content))
        for old_url,old_bytes in delivered:
            assert client.get(old_url).content==old_bytes
        return result
    with TestClient(web.app) as client:
        first=client.post('/api/research',json={'mode':'simple','query':'创建一个测试报告文件'}).json()['run_id']
        a=collect(client,first)
        response=client.post(f'/api/research/{first}/followup',json={'request_id':'edit-2','text':'把报告改成版本 2'})
        assert response.status_code==202,response.text
        second=response.json()['run_id'];b=collect(client,second)
        assert b['execution_summary']['reused_container']
        assert a['execution_summary']['container_id']==b['execution_summary']['container_id']
        assert json.loads(calls[1].context.receipts[0]['payload'])['content']==body(1)
        assert 'report.md' in str(calls[1].seen[0]) and first in str(calls[1].seen[0])
    # lifespan 已关闭旧池；模拟新进程的运行注册表，保持磁盘数据。
    registry=web.RunRegistry(store);monkeypatch.setattr(web,'REGISTRY',registry)
    with TestClient(web.app) as client:
        response=client.post(f'/api/research/{second}/followup',json={'request_id':'edit-3','text':'把报告改成版本 3'})
        assert response.status_code==202,response.text
        third=response.json()['run_id'];c=collect(client,third)
        assert not c['execution_summary']['reused_container']
        assert c['execution_summary']['container_id']!=b['execution_summary']['container_id']
        assert json.loads(calls[2].context.receipts[0]['payload'])['content']==body(2)
        assert client.get(f'/api/conversations/{first}').json()['total']==3
