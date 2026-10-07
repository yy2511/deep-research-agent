import pytest


def test_network_mode_is_explicit_and_reversible(tmp_path, monkeypatch):
    from dra import execution
    from pydantic import ValidationError
    monkeypatch.setattr(execution, '_ENV_PATH', tmp_path / '.env')
    monkeypatch.delenv('DRA_EXECUTION_NETWORK_MODE', raising=False)
    assert execution.ExecutionConfig.from_env().network_mode == 'none'
    (tmp_path / '.env').write_text('DRA_EXECUTION_NETWORK_MODE=bridge\n')
    assert execution.ExecutionConfig.from_env().network_mode == 'bridge'
    monkeypatch.setenv('DRA_EXECUTION_NETWORK_MODE', 'none')
    assert execution.ExecutionConfig.from_env().network_mode == 'none'
    monkeypatch.setenv('DRA_EXECUTION_NETWORK_MODE', 'host')
    with pytest.raises(ValidationError):
        execution.ExecutionConfig.from_env()


def test_execution_disabled_never_connects(tmp_path, monkeypatch):
    from dra.execution import ExecutionConfig, ExecutionSession, ExecutionError
    monkeypatch.setattr('docker.DockerClient', lambda **kw: pytest.fail('must not connect'))
    session = ExecutionSession(tmp_path, 'r1', ExecutionConfig())
    with pytest.raises(ExecutionError, match='disabled'):
        session.start()


def test_pre_file_parser_image_requires_rebuild(monkeypatch):
    from types import SimpleNamespace as N
    from dra import execution as ex
    monkeypatch.setattr(ex, '_PENDING', set())
    image = N(id='sha256:old', labels={'dra.sandbox.protocol': '2'})
    client = N(ping=lambda: None, images=N(get=lambda *a: image), close=lambda: None)
    monkeypatch.setattr(ex, 'docker_client', lambda config: client)
    assert ex.availability(ex.ExecutionConfig(enabled=True))['reason'] == 'image_incompatible'
    image.labels['dra.sandbox.protocol'] = '3'
    assert ex.availability(ex.ExecutionConfig(enabled=True))['image_id'] == 'sha256:old'


@pytest.mark.parametrize('path', ['/etc/passwd', '/workspace/work/../input/a', '/workspace/work/a\x00', '/workspace/work//a'])
def test_paths_reject_escape(path):
    from dra.execution import workspace_path, ExecutionError
    with pytest.raises(ExecutionError):
        workspace_path(path, write=True)


def test_export_receipt_is_bounded_and_binds_path_and_hash():
    import hashlib,base64
    from dra.execution import decode_snapshot,ExecutionError
    path='/workspace/output/report.md'
    item={'path':path,'base64':base64.b64encode(b'abc').decode(),'size':3,'sha256':hashlib.sha256(b'abc').hexdigest()}
    assert decode_snapshot({'ok':True,'files':[item]},{path:10})=={path:b'abc'}
    for payload in [{'ok':True,'files':[item,item]}, {'ok':True,'files':[{**item,'sha256':'wrong'}]},
                    {'ok':True,'files':[{**item,'path':'/etc/passwd'}]}, {'ok':False}]:
        with pytest.raises(ExecutionError): decode_snapshot(payload,{path:10})
    with pytest.raises(ExecutionError): decode_snapshot({'ok':True,'files':[item]},{path:2})


@pytest.fixture
def files(tmp_path):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('sandbox_helper',Path(__file__).parents[1]/'sandbox_image/helper.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    for folder in ('input','work','output'):(tmp_path/folder).mkdir()
    return module,tmp_path


def test_edit_conflict_and_exact_replacement(files):
    helper,root=files
    first=helper.operate({'op':'write_file','path':'/workspace/work/a','content':'hello world'},root)
    with pytest.raises(helper.FileError):
        helper.operate({'op':'write_file','path':'/workspace/work/a','content':'lost'},root)
    result=helper.operate({'op':'edit_file','path':'/workspace/work/a','old_text':'world','new_text':'世界','expected_sha256':first['sha256']},root)
    assert helper.operate({'op':'read_file','path':'/workspace/work/a'},root)['content']=='hello 世界'
    assert result['sha256']!=first['sha256']


def test_dependency_inventory_transfers_full_file_and_reports_save_failure(files,monkeypatch):
    import base64,json
    from dra.execution import ExecutionConfig,ExecutionSession
    helper,root=files
    inventory={'packages':{'requests':'fixture'},'node_lock':{'packages':{
        f'node_modules/fixture-{i}':{'version':'1.0','integrity':'x'*100} for i in range(3000)}}}
    monkeypatch.setattr(helper,'dependency_inventory',lambda root:inventory)
    session=ExecutionSession(root/'run','run',ExecutionConfig(enabled=True))
    session.workspace.prepare()
    def execute(argv,timeout,**kwargs):
        assert kwargs['managed'] and not kwargs['logs']
        request=json.loads(base64.b64decode(''.join(argv[3:])))
        result=helper.operate(request,root=session.workspace.path)
        return {'ok':True,'stdout':json.dumps({'ok':True,**result}),'truncated':False}
    session._exec=execute
    receipt={'ok':True,'execution_id':'large'}
    session._record_dependencies(receipt)
    saved=session.run_dir/receipt['dependency_manifest']
    assert saved.stat().st_size>128*1024
    assert json.loads(saved.read_text())==inventory
    def fail(*args,**kwargs):raise OSError('fixture disk full')
    monkeypatch.setattr('dra.durable.atomic_json',fail)
    receipt={'ok':True,'execution_id':'save-failed'}
    session._record_dependencies(receipt)
    assert receipt['ok'] and 'dependency_manifest' not in receipt
    assert receipt['dependency_inventory_error']=='fixture disk full'


def test_inventory_timeout_only_stops_its_process_group(tmp_path,monkeypatch):
    from types import SimpleNamespace as N
    from dra.execution import ExecutionConfig,ExecutionSession,ExecutionError
    from dra.execution_process import RUN,STOP
    session=ExecutionSession(tmp_path,'run',ExecutionConfig(enabled=True))
    session.state='ready'; calls=[]; starts=[]
    session.container=N(id='fixture',exec_run=lambda argv,**kw:(calls.append(argv) or N(exit_code=0)))
    stream=N(setblocking=lambda value:None,close=lambda:None)
    session.client=N(api=N(exec_create=lambda _,argv,**kw:(starts.append(argv) or {'Id':'fixture'}),
                          exec_start=lambda *a,**kw:stream))
    checks=0
    def check(*args):
        nonlocal checks
        checks+=1
        if checks>1:raise ExecutionError('command_timeout')
    monkeypatch.setattr(session,'_check',check)
    monkeypatch.setattr(session,'_persist',lambda:None)
    monkeypatch.setattr(session,'close',lambda:pytest.fail('must retain shared container'))
    with pytest.raises(ExecutionError,match='command_timeout'):
        session._exec(['inventory'],10,logs=False,managed=True)
    assert RUN in starts[0] and STOP in calls[0]
    assert session.state=='ready'


def test_files_refuse_symlink_parent_hardlink_and_fifo(files):
    import os
    helper,root=files
    private=root/'private'; private.write_text('secret')
    (root/'work/link').symlink_to(private)
    (root/'work/parent').symlink_to(root)
    os.link(private,root/'work/hard')
    os.mkfifo(root/'work/fifo')
    for path in ('link','parent/private','hard','fifo'):
        with pytest.raises((helper.FileError,OSError)):
            helper.operate({'op':'read_file','path':'/workspace/work/'+path},root)


def test_ambiguous_container_creation_quarantines_execution(tmp_path,monkeypatch):
    import docker
    from types import SimpleNamespace
    from dra import execution as ex
    def ambiguous(*a,**kw): raise docker.errors.DockerException('connection lost after create')
    client=SimpleNamespace(images=SimpleNamespace(get=lambda image:SimpleNamespace(id='sha256:test',labels={'dra.sandbox.protocol':'3'})),
        info=lambda:{'OSType':'linux','MemoryLimit':True,'CpuCfsQuota':True,'PidsLimit':True},
        containers=SimpleNamespace(create=ambiguous),close=lambda:None)
    monkeypatch.setattr(ex,'docker_client',lambda config:client)
    pending=set();monkeypatch.setattr(ex,'_PENDING',pending)
    session=ex.ExecutionSession(tmp_path/'run','run',ex.ExecutionConfig(enabled=True))
    with pytest.raises(ex.ExecutionError): session.start()
    assert session.state=='cleanup_pending'
    assert pending
    assert ex.availability(session.config)['reason']=='cleanup_pending'


def test_log_storage_failure_is_not_a_recoverable_script_error(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from dra.execution import ExecutionConfig,ExecutionSession
    from dra.llm import LLMPersistenceError
    session=ExecutionSession(tmp_path,'r',ExecutionConfig(enabled=True))
    session.client=SimpleNamespace(api=SimpleNamespace(exec_create=lambda *a,**kw:{'Id':'exec'}),close=lambda:None)
    session.container=SimpleNamespace(id='container',remove=lambda **kw:None)
    original=Path.open
    def fail(self,*args,**kwargs):
        if self.suffix=='.stdout':raise OSError('disk full')
        return original(self,*args,**kwargs)
    monkeypatch.setattr(Path,'open',fail)
    with pytest.raises(LLMPersistenceError):session._exec(['true'],10)


def test_failed_orphan_scan_quarantines_and_successful_scan_clears(tmp_path,monkeypatch):
    import docker
    from types import SimpleNamespace
    from dra import execution as ex
    pending=set();monkeypatch.setattr(ex,'_PENDING',pending)
    def fail(**kw):raise docker.errors.DockerException('offline')
    client=SimpleNamespace(containers=SimpleNamespace(list=fail),close=lambda:None)
    monkeypatch.setattr(ex,'docker_client',lambda cfg:client)
    config=ex.ExecutionConfig(enabled=True)
    with pytest.raises(docker.errors.DockerException):ex.reap_orphans(tmp_path,config)
    assert ex.availability(config)['reason']=='cleanup_pending'
    client.containers.list=lambda **kw:[]
    ex.reap_orphans(tmp_path,config)
    assert not pending


def test_execution_config_reads_project_env_before_provider_calls(tmp_path,monkeypatch):
    from dra import execution as ex
    config=tmp_path/'.env';config.write_text('DRA_EXECUTION_ENABLED=true\nDRA_EXECUTION_IMAGE=fixture-image\n')
    monkeypatch.setattr(ex,'_ENV_PATH',config,raising=False)
    monkeypatch.delenv('DRA_EXECUTION_ENABLED',raising=False)
    monkeypatch.delenv('DRA_EXECUTION_IMAGE',raising=False)
    value=ex.ExecutionConfig.from_env()
    assert value.enabled and value.image=='fixture-image'
    monkeypatch.setenv('DRA_EXECUTION_ENABLED','false')
    assert not ex.ExecutionConfig.from_env().enabled
