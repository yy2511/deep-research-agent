import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from dra import simple_research as sr
from dra.execution import ExecutionError,ExecutionConfig


def test_file_finish_and_no_format_default(tmp_path):
    from dra.artifacts import prepare_finish, publish
    class Files:
        image_id='sha256:image';inputs={'s1.txt':'source-sha'}
        def snapshot(self, paths): return {p:b'# Report\nData is 60.' if p.endswith('.md') else b'a,b\n1,2\n' for p in paths}
    ctx=sr.ToolContext(sr.open_run(tmp_path,run_id='r',query='q'),sr.SimpleConfig(execution=ExecutionConfig(enabled=True)))
    ctx.execution=Files()
    ctx.execution_materials=[{'source_ref':'s1','path':'/workspace/input/s1.txt','sha256':'source-sha'}]
    ctx.execution_files={'/workspace/output/table.csv':{'path':'/workspace/output/table.csv','size':8}}
    ctx.receipts=[{'tool':'write_file','payload':json.dumps({'execution_id':'exec_1'})}]
    ctx.active_tool_call_id='finish_1'
    args={'draft_file':'/workspace/work/report.md','artifacts':[
        {'path':f'/workspace/output/{name}.csv','title':name,'kind':'data','requirement_ids':['goal']}
        for name in ('table','copy')]}

    ctx.effective_requirements=[sr.Requirement(id='goal',text='q')]
    body=prepare_finish(ctx,args)
    assert body.startswith('# Report') and ctx.pending_artifacts
    records=publish(ctx,body)
    record=records[0]
    assert (tmp_path/record['storage_path']).read_bytes()==b'a,b\n1,2\n'
    manifest=json.loads((tmp_path/'artifacts/manifest.json').read_text())
    assert len(records)==2 and manifest['artifacts']==records
    provenance=manifest['provenance']
    assert provenance=={'backend':'docker',
        'draft_sha256':hashlib.sha256(body.encode()).hexdigest(),
        'image_id':'sha256:image','inputs':ctx.execution.inputs,
        'source_materials':ctx.execution_materials,'execution_files':list(ctx.execution_files.values()),
        'execution_ids':['exec_1'],'tool_call_id':'finish_1',
    }
    assert all(not set(provenance).intersection(item) for item in records)
    from dra.artifacts import read_committed_file
    assert all(read_committed_file(tmp_path,item)==b'a,b\n1,2\n' for item in records)
    assert record['artifact_id'] not in args['artifacts'][0]['path']
    with pytest.raises(ExecutionError):prepare_finish(ctx,{'draft':'x','draft_file':'/workspace/work/a.md'})


@pytest.mark.parametrize('name,data',[
    ('bad.svg',b'<svg>'),('bad.svg',b'<html/>'),
    ('bad.pdf',b'not a pdf'),('bad.png',b'no'),('bad.json',b'{'),
])
def test_invalid_file_formats_rejected(name,data):
    from dra.artifacts import validate_file
    with pytest.raises(ExecutionError):validate_file(name,data)


def test_missing_formats_and_mindmap_requires_editable_source():
    from dra.artifacts import missing_formats
    req=[sr.Requirement(id='goal',text='q',output_formats=['pdf','mindmap'])]
    assert len(missing_formats(req,[]))==2
    artifacts=[{'format':'pdf','kind':'report','requirement_ids':['goal']}, {'format':'svg','kind':'mindmap','requirement_ids':['goal']}]
    assert len(missing_formats(req,artifacts))==1
    artifacts.append({'format':'json','kind':'mindmap','requirement_ids':['goal']})
    assert not missing_formats(req,artifacts)


def test_rejected_finish_does_not_keep_old_attachments(tmp_path):
    from dra.artifacts import prepare_finish
    ctx=sr.ToolContext(sr.open_run(tmp_path,run_id='r',query='q'),sr.SimpleConfig())
    ctx.pending_artifacts=[{'fake':True}]
    ctx.pending_artifact_errors=[{'message':'old error'}]
    assert prepare_finish(ctx,{'draft':'Text only.'})=='Text only.'
    assert not ctx.pending_artifacts
    assert not ctx.pending_artifact_errors


@pytest.mark.parametrize('file_draft', [False, True])
def test_bad_optional_attachments_keep_body_and_publish_valid_file(tmp_path, file_draft):
    from dra.artifacts import prepare_finish, publish
    body='现有研究正文。'
    class Files:
        image_id='sha256:image';inputs={}
        def snapshot(self, paths):
            files={}
            for path in paths:
                if path.endswith('missing.csv'):raise ExecutionError('file_error','文件不存在')
                files[path]=body.encode() if path.endswith('.md') else b'{' if path.endswith('.json') else b'total\n60\n'
            return files
    ctx=sr.ToolContext(sr.open_run(tmp_path,run_id='r',query='q'),sr.SimpleConfig(execution=ExecutionConfig(enabled=True)))
    ctx.execution=Files()
    args={'draft_file':'/workspace/work/report.md'} if file_draft else {'draft':body}
    args['artifacts']=[{'path':f'/workspace/output/{name}','title':name}
                       for name in ('missing.csv','bad.json','valid.csv')]
    assert prepare_finish(ctx,args)==body
    errors=ctx.pending_artifact_errors
    assert [(e['path'],e['error_code']) for e in errors]==[
        ('/workspace/output/missing.csv','file_error'),('/workspace/output/bad.json','invalid_artifact')]
    assert all(e['message'] for e in errors)
    records=publish(ctx,body)
    assert len(records)==1 and records[0]['filename']=='valid.csv'
    assert (tmp_path/records[0]['storage_path']).read_bytes()==b'total\n60\n'


@pytest.mark.parametrize('items', [
    {'path':'/workspace/output/report.csv','title':'Report'},
])
def test_invalid_attachment_list_keeps_body_with_explicit_error(tmp_path, items):
    from dra.artifacts import prepare_finish
    ctx=sr.ToolContext(sr.open_run(tmp_path,run_id='r',query='q'),sr.SimpleConfig(execution=ExecutionConfig(enabled=True)))
    ctx.execution=SimpleNamespace(snapshot=lambda _: pytest.fail('invalid list must not be partly exported'))
    assert prepare_finish(ctx,{'draft':'正文。','artifacts':items})=='正文。'
    assert not ctx.pending_artifacts
    assert len(ctx.pending_artifact_errors)==1 and ctx.pending_artifact_errors[0]['message']


def test_optional_attachment_without_workspace_keeps_body(tmp_path):
    from dra.artifacts import prepare_finish
    ctx=sr.ToolContext(sr.open_run(tmp_path,run_id='r',query='q'),sr.SimpleConfig())
    assert prepare_finish(ctx,{'draft':'正文。','artifacts':[{'path':'/workspace/output/a.csv','title':'A'}]})=='正文。'
    assert not ctx.pending_artifacts
    assert ctx.pending_artifact_errors[0]['error_code']=='workspace_unavailable'


@pytest.mark.parametrize('file_draft,error', [
    (True,ExecutionError('file_error','正文文件不存在')),
    (False,ExecutionError('cancelled')),
    (False,ExecutionError('command_timeout')),
    (False,sr.LLMPersistenceError('写盘失败')),
])
def test_body_file_and_runtime_failures_still_block_finish(tmp_path, file_draft, error):
    from dra.artifacts import prepare_finish
    def snapshot(_):raise error
    ctx=sr.ToolContext(sr.open_run(tmp_path,run_id='r',query='q'),sr.SimpleConfig(execution=ExecutionConfig(enabled=True)))
    ctx.execution=SimpleNamespace(snapshot=snapshot)
    args={'draft_file':'/workspace/work/report.md'} if file_draft else {
        'draft':'正文。','artifacts':[{'path':'/workspace/output/a.csv','title':'A'}]}
    with pytest.raises(type(error)) as caught:prepare_finish(ctx,args)
    assert caught.value is error


def test_format_requirements_survive_brief_and_missing_files_downgrade(tmp_path):
    from dra.research_brief import ResearchBrief
    from test_simple_research import Script
    brief=ResearchBrief(original_query='q',objective='q',requirements=[{'id':'goal','text':'输出 PDF','kind':'constraint','output_formats':['pdf']}],route=[{'id':'r1','text':'检索'}],assumptions=[],needs_confirmation=False,confirmation_reason='明确')
    requirements=brief.to_run_requirements()
    assert requirements[0].output_formats==['pdf']
    result=sr.run_simple_research('q',requirements,run_dir=tmp_path,model_call=Script([],finish=('finish_research',{'draft':'现有资料不足。','coverage':[{'requirement_id':'goal','status':'answered'}]})))
    assert result.delivery_status=='limited'
    assert result.research_coverage['goal']=='partial'
    assert result.artifact_errors[0]['format']=='pdf'


@pytest.mark.docker
def test_real_file_finish_passes_existing_gates_and_survives_cleanup(tmp_path):
    from test_simple_research import _run,_turn,_full_sequence
    turns=_full_sequence()+[
        _turn(('write_file',{'path':'/workspace/work/report.md','content':'sqlite3_rsync 通过 SSH 同步数据库 [c1]。'})),
        _turn(('write_file',{'path':'/workspace/output/report.html','content':'<!doctype html><html><body><h1>中文报告</h1><p>通过 SSH 同步 [c1]。</p></body></html>'})),
    ]
    finish=('finish_research',{'draft_file':'/workspace/work/report.md','artifacts':[{'path':'/workspace/output/report.html','title':'中文报告','kind':'report','requirement_ids':['goal']}],'coverage':[{'requirement_id':'goal','status':'answered'}]})
    result,script=_run(turns,finish=finish,run_dir=tmp_path,config=sr.SimpleConfig(execution=ExecutionConfig(enabled=True)))
    assert result.delivery_status=='delivered',result.warnings
    assert len(result.artifacts)==1 and result.execution_summary['state']=='released'
    assert (tmp_path/result.artifacts[0]['storage_path']).read_bytes().startswith(b'<!doctype')
    assert json.loads((tmp_path/'run.json').read_text())['final']['artifacts']==result.artifacts


@pytest.mark.docker
def test_invalid_citation_never_publishes_attachments(tmp_path):
    from test_simple_research import _run,_turn
    turns=[_turn(('write_file',{'path':'/workspace/output/table.csv','content':'a,b\n1,2'}))]
    result,_=_run(turns,finish=('finish_research',{'draft':'错误结论 [c999]。','artifacts':[{'path':'/workspace/output/table.csv','title':'表格','kind':'data','requirement_ids':[]}],'coverage':[]}),run_dir=tmp_path,config=sr.SimpleConfig(execution=ExecutionConfig(enabled=True)))
    assert result.draft_rejected and not result.artifacts
    assert not (tmp_path/'artifacts').exists()


@pytest.mark.docker
def test_wrapup_can_only_submit_already_written_files(tmp_path):
    from test_simple_research import _run,_turn
    turns=[_turn(('bash',{'command':"printf '计算结果为 60。' > /workspace/work/report.md; printf 'total\\n60\\n' > /workspace/output/data.csv"}))]
    result,script=_run(turns,finish=('finish_research',{'draft_file':'/workspace/work/report.md','artifacts':[{'path':'/workspace/output/data.csv','title':'计算结果','kind':'data','requirement_ids':[]}],'coverage':[]}),run_dir=tmp_path,config=sr.SimpleConfig(execution=ExecutionConfig(enabled=True),max_tool_calls=1))
    assert len(result.artifacts)==1,result.warnings
    assert 'wrapup' in result.stop_reason
    assert [s['function']['name'] for s in script.kwargs['tools']]==['finish_research']


def test_interactive_html_is_published_unchanged(tmp_path):
    from dra.artifacts import prepare_finish, publish, read_committed_file
    page=(Path(__file__).parent/'fixtures/interactive-report.html').read_bytes()
    class Files:
        image_id='sha256:image';inputs={}
        def snapshot(self, paths):return {path:page for path in paths}
    ctx=sr.ToolContext(sr.open_run(tmp_path,run_id='r',query='q'),sr.SimpleConfig(execution=ExecutionConfig(enabled=True)))
    ctx.execution=Files()
    body=prepare_finish(ctx,{'draft':'交互报告。','artifacts':[{'path':'/workspace/output/report.html','title':'交互报告'}]})
    record=publish(ctx,body)[0]
    assert record['format']=='html'
    assert read_committed_file(tmp_path,record)==page


@pytest.mark.parametrize('svg', [
    '<svg xmlns="http://www.w3.org/2000/svg" xml:base="https://example.com/" onload="console.log(1)"><script>console.log(2)</script><foreignObject/><image href="chart.png"/></svg>',
    '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd"><svg xmlns="http://www.w3.org/2000/svg"><metadata/><style>@import url("https://example.com/chart.css");</style></svg>',
])
def test_svg_accepts_browser_features(svg):
    from dra.artifacts import validate_file
    assert validate_file('chart.svg',svg.encode())==('svg','image/svg+xml')


def test_pdf_validation_does_not_expand_untrusted_objects_on_host(monkeypatch):
    import pypdf
    from dra.artifacts import validate_file
    monkeypatch.setattr(pypdf,'PdfReader',lambda *a,**kw:pytest.fail('untrusted PDF parser must not run in web process'))
    data=(Path(__file__).parents[1]/'fixtures/library_demo/orchid-data-policy.pdf').read_bytes()
    assert validate_file('report.pdf',data)[0]=='pdf'
