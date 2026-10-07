import json
import pytest
from dra import simple_research as sr
from dra.execution import ExecutionConfig
from test_simple_research import _run, _turn


@pytest.mark.parametrize('formats', [[], ['html'], ['pdf'], ['html', 'pdf']])
def test_mission_preserves_explicit_output_formats(formats):
    # 输入资料的 PDF 与交付格式是不同信息；不能只依赖要求文字猜格式。
    requirement=sr.Requirement(id='deliver',text='阅读提供的 PDF 资料并交付研究结果',output_formats=formats)
    prompt=sr._render_mission('阅读资料', [requirement])
    row=next(line for line in prompt.splitlines() if line.startswith('- id=deliver'))
    assert f'output_formats={json.dumps(formats)}' in row


def test_disabled_execution_is_not_advertised_or_executed(tmp_path):
    result, script = _run([_turn(('bash', {'command':'echo forbidden'}))],run_dir=tmp_path)
    assert 'bash' not in {s['function']['name'] for s in script.kwargs['tools']}
    assert result.execution_summary == {}
    assert not script.context.receipts[0]['ok']


def test_child_executes_in_parent_workspace(tmp_path):
    from test_execution_evidence import LocalSession
    store=sr.open_run(tmp_path,run_id='r',query='q')
    ctx=sr.ToolContext(store,sr.SimpleConfig(execution=ExecutionConfig(enabled=True)))
    ctx.execution=LocalSession(store)
    child=ctx.fork_for_subagent(ctx.config,budget=sr.Budget(ctx.config,None),delegation_id='sub1',parent_call_id='p')
    payload,ok=child.execute('bash',{'command':'echo shared'})
    assert ok and json.loads(payload)['stdout'].strip()=='shared'
    assert child.execution is ctx.execution


@pytest.mark.docker
def test_lead_computes_without_format_request_and_exports_without_read_permission(tmp_path):
    result,script=_run([
        _turn(('bash',{'command':"python -c 'print(sum([10,20,30]))'"})),
        _turn(('bash',{'command':'exit 7'})),
        _turn(('bash',{'command':'echo recovered'})),
    ],run_dir=tmp_path,config=sr.SimpleConfig(execution=ExecutionConfig(enabled=True)),
        finish=('finish_research',{'draft':'计算结果为 60。','coverage':[]}))
    assert 'bash' in {s['function']['name'] for s in script.kwargs['tools']}
    receipts=script.context.receipts
    assert json.loads(receipts[0]['payload'])['stdout'].strip()=='60'
    assert not receipts[1]['ok'] and receipts[2]['ok']
    assert result.execution_summary['state']=='released'
    assert result.execution_summary['commands']==3
    assert result.draft=='计算结果为 60。'


@pytest.mark.docker
def test_material_export_is_exact_and_does_not_grant_read_exposure(tmp_path):
    from test_simple_research import _doc
    store=sr.open_run(tmp_path,run_id='r',query='q')
    ctx=sr.ToolContext(store,sr.SimpleConfig(execution=ExecutionConfig(enabled=True)))
    source,_=store.write_source(_doc())
    try:
        payload,ok=ctx.execute('export_research_materials',{'source_refs':[source.ref]})
        assert ok,payload
        data=json.loads(payload)
        path=data['files'][0]['path']
        text,ok=ctx.execute('read_file',{'path':path})
        assert ok and json.loads(text)['content']==store.read_source_text(source.ref)
        assert ctx.read_exposures=={}
        (store.sources_dir/f'{source.ref}.txt').write_text('tampered')
        payload,ok=ctx.execute('export_research_materials',{'source_refs':[source.ref]})
        assert not ok and json.loads(payload)['error_code']=='source_changed'
    finally:
        if ctx.execution: ctx.execution.close()


def test_unavailable_environment_keeps_research_and_text_delivery(tmp_path,monkeypatch):
    monkeypatch.setattr('dra.execution_tools.availability',lambda config:{'enabled':True,'available':False,'reason':'image_missing'})
    result,script=_run([],run_dir=tmp_path,config=sr.SimpleConfig(execution=ExecutionConfig(enabled=True)),
        finish=('finish_research',{'draft':'没有取得外部材料。','coverage':[]}))
    assert 'bash' not in {s['function']['name'] for s in script.kwargs['tools']}
    assert result.draft and not result.execution_summary
    assert 'image_missing' in script.seen[0][0]['content']
