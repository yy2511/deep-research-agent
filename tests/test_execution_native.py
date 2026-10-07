import io
import json
import shlex
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dra.execution import ExecutionConfig, ExecutionError
from dra.execution_native import NativeSession
from dra.execution_workspace import ConversationWorkspace


def test_workspace_lease_is_shared_by_backends_and_real_paths_stay_scoped(tmp_path):
    docker=ConversationWorkspace(tmp_path/'a','a',ExecutionConfig(), 'conversation')
    native=ConversationWorkspace(tmp_path/'b','b',ExecutionConfig(backend='native'), 'conversation')
    docker.acquire()
    try:
        with pytest.raises(ExecutionError,match='workspace_busy'):native.acquire()
        assert native.logical_path(str(native.path/'work'/'data.csv'))=='/workspace/work/data.csv'
        assert native.logical_path(str(tmp_path/'other'))==str(tmp_path/'other')
    finally:docker.release()
    native.acquire();native.release()


def test_native_cleanup_error_still_releases_workspace(tmp_path):
    from types import SimpleNamespace
    session=NativeSession(tmp_path/'run','run',ExecutionConfig(enabled=True,backend='native'))
    session.workspace.acquire()
    def fail():raise OSError('fixture cleanup failure')
    session.temporary=SimpleNamespace(cleanup=fail)
    session.close()
    following=ConversationWorkspace(tmp_path/'run','run',session.config)
    following.acquire();following.release()
    assert session.state=='closed'
    assert session.summary()['cleanup_error']=='fixture cleanup failure'
    assert (session.workspace.path/'workspace.json').exists()


def fixture_wheel():
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as wheel:
        wheel.writestr('dra_fixture.py','VALUE = 1729\n')
        wheel.writestr('dra_fixture-1.0.dist-info/METADATA','Metadata-Version: 2.1\nName: dra-fixture\nVersion: 1.0\n')
        wheel.writestr('dra_fixture-1.0.dist-info/WHEEL','Wheel-Version: 1.0\nGenerator: dra-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        wheel.writestr('dra_fixture-1.0.dist-info/RECORD','')
    return buf.getvalue()


@pytest.fixture
def native(tmp_path):
    if sys.platform!='darwin':pytest.skip('macOS native backend')
    config=ExecutionConfig(enabled=True,backend='native',network_mode='bridge',max_commands=40)
    session=NativeSession(tmp_path/'run','run',config,deadline=time.monotonic()+180,conversation_id='native-test')
    try:yield session
    finally:session.close()


def bash(session,command,**kwargs):
    return session.tool('bash',{'command':command,'timeout_s':60,**kwargs})


@pytest.mark.native
def test_real_native_files_dependencies_evidence_and_next_turn(native,tmp_path,monkeypatch):
    source=native.import_file('raw.txt',b'original')
    wheel=native.import_file('dra_fixture-1.0-py3-none-any.whl',fixture_wheel())
    assert native.tool('read_file',{'path':source})['content']=='original'
    receipt=bash(native,'set -e; python -m pip install --no-index '+shlex.quote(native.workspace.execution_path(wheel)))
    assert receipt['ok'],receipt
    receipt=bash(native,"python -c 'import dra_fixture,requests,pandas; print(dra_fixture.VALUE); print(requests.__version__)'")
    assert receipt['ok'] and '1729' in receipt['stdout'],receipt
    inventory=json.loads((native.run_dir/receipt['dependency_manifest']).read_text())
    assert inventory['packages']['dra-fixture']=='1.0'
    assert 'pandas' in inventory['packages']
    receipt=bash(native,'set -e; printf "# Test\n" > "$DRA_WORK/report.md"; dra-render report --input "$DRA_WORK/report.md" --html "$DRA_OUTPUT/report.html"')
    assert receipt['ok'],receipt
    assert b'<h1>Test</h1>' in native.snapshot({'/workspace/output/report.html':None})['/workspace/output/report.html']
    actual=str(native.workspace.path/'output/report.html')
    assert native.tool('read_file',{'path':actual})['content'].startswith('<!doctype html>')
    # A valid file-tool request may exceed macOS ARG_MAX once base64 encoded.
    large='A'*240_000
    seed=native.tool('write_file',{'path':'/workspace/work/large.txt','content':'seed'})
    assert native.tool('edit_file',{'path':'/workspace/work/large.txt','expected_sha256':seed['sha256'],
                                   'old_text':'seed','new_text':large})['ok']
    assert native.snapshot({'/workspace/work/large.txt':None})['/workspace/work/large.txt']==large.encode()
    native.close()
    monkeypatch.chdir(tmp_path)
    following=NativeSession(Path('next'),'next',native.config,deadline=time.monotonic()+90,conversation_id='native-test')
    try:
        receipt=bash(following,"python -c 'import dra_fixture;print(dra_fixture.VALUE)'")
        assert receipt['ok'] and receipt['stdout'].strip()=='1729',receipt
    finally:following.close()


@pytest.mark.native
def test_real_native_read_write_hardlink_and_environment_boundaries(native,tmp_path,monkeypatch):
    monkeypatch.setenv('DRA_TEST_SECRET','must-not-inherit')
    outside=tmp_path/'private-fixture';outside.write_text('dummy secret')
    native.import_file('raw.txt',b'original')
    receipt=bash(native,'python -c \'import os; print(os.environ.get("DRA_TEST_SECRET","absent"))\'')
    assert receipt['ok'] and receipt['stdout'].strip()=='absent',receipt
    lease_fd=native.workspace._lease.fileno()
    script=f'\nimport os\ntry:os.fstat({lease_fd})\nexcept OSError:print("lease closed")\nelse:raise AssertionError("inherited workspace lease")'
    receipt=bash(native,shlex.join(['python','-c',script]))
    assert receipt['ok'] and receipt['stdout'].strip()=='lease closed',receipt
    paths=[outside,native.workspace.root/'.locks'/native.workspace.key]
    for path in paths:
        receipt=bash(native,'cat '+shlex.quote(str(path)))
        assert not receipt['ok'],receipt
    commands=[
        'chmod u+w "$DRA_INPUT/raw.txt" && echo broken > "$DRA_INPUT/raw.txt"',
        'ln "$DRA_INPUT/raw.txt" "$DRA_WORK/linked" && chmod u+w "$DRA_WORK/linked" && echo broken > "$DRA_WORK/linked"',
        'ln '+shlex.quote(str(outside))+' "$DRA_WORK/secret-link" && cat "$DRA_WORK/secret-link"',
        'ln -s '+shlex.quote(str(outside))+' "$DRA_WORK/secret-symlink" && cat "$DRA_WORK/secret-symlink"',
        'echo broken > '+shlex.quote(str(outside)),
    ]
    for command in commands:
        receipt=bash(native,command)
        assert not receipt['ok'],receipt
    assert outside.read_text()=='dummy secret'
    assert (native.workspace.path/'input/raw.txt').read_bytes()==b'original'


@pytest.mark.native
def test_real_native_network_cannot_reach_host_or_bypass_proxy(native):
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path);self.send_response(200);self.end_headers();self.wfile.write(b'host secret')
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        for host in ('127.0.0.1','localhost','localhost.','0x7f000001','[::1]','169.254.169.254','10.0.0.1'):
            receipt=bash(native,shlex.join(['curl','--fail','--max-time','2',f'http://{host}:{server.server_port}/test']))
            assert not receipt['ok'],receipt
        receipt=bash(native,shlex.join(['curl','--noproxy','*','--fail','--max-time','2',f'http://127.0.0.1:{server.server_port}/direct']))
        assert not receipt['ok'],receipt
        receipt=bash(native,shlex.join(['curl','--noproxy','','--fail','--max-time','2',f'http://127.0.0.1:{server.server_port}/proxy']))
        assert not receipt['ok'],receipt
        python=f'import os,urllib.request; os.environ["NO_PROXY"]=""; os.environ["no_proxy"]=""; opener=urllib.request.build_opener(urllib.request.ProxyHandler({{"http":os.environ["HTTP_PROXY"]}})); print(opener.open("http://localhost:{server.server_port}/urllib",timeout=2).read())'
        receipt=bash(native,shlex.join(['python','-c',python]))
        assert not receipt['ok'],receipt
        assert requests==[]
    finally:server.shutdown();server.server_close();thread.join()


@pytest.mark.native
def test_native_proxy_guard_normalizes_literals_and_checks_resolved_private_addresses():
    from dra.execution_native import ROOT
    script='''
import assert from 'node:assert/strict';
import { outboundFilter } from './sandbox_image/native_runner.mjs';
import { createResolvedAddressGuard } from './web/node_modules/@anthropic-ai/sandbox-runtime/dist/sandbox/resolved-address-guard.js';
const network={deniedResolvedAddresses:['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','100.64.0.0/10','fc00::/7']};
const allow=outboundFilter({allowExternalNetwork:true,network});
for (const host of ['127.1','0x7f000001','localhost.','child.localhost','::1','::ffff:127.0.0.1','2002:7f00:1::','64:ff9b::a00:1','10.1.2.3','172.16.0.1','192.168.1.1','100.64.1.2','fd00::1']) {
  assert.equal(await allow({host,port:80}),false,host);
}
assert.equal(await allow({host:'example.com',port:443}),true);
assert.equal(await allow({host:'8.8.8.8',port:443}),true);
assert.equal(await outboundFilter({allowExternalNetwork:false,network})({host:'example.com',port:443}),false);
// Fake DNS only: verify the actual dial lookup rejects an otherwise permitted name.
const guard=createResolvedAddressGuard({...network,resolve:(_host,_options,callback)=>callback(null,[{address:'10.2.3.4',family:4}])});
await new Promise(resolve=>guard.lookupFor(443)('public.fixture',{},(error,addresses)=>{
  assert.equal(error.code,'ERR_SRT_RESOLVED_ADDRESS_DENIED');assert.deepEqual(addresses,[]);resolve();
}));
'''
    result=subprocess.run([shutil.which('node'),'--input-type=module','-e',script],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode==0,result.stderr


@pytest.mark.native
def test_native_service_crash_stops_ordinary_children_and_releases_lease(tmp_path):
    from dra.execution_native import ROOT
    config=ExecutionConfig(enabled=True,backend='native',network_mode='none')
    workspace=ConversationWorkspace(tmp_path/'crash','crash',config,'crash-conversation')
    script='''
import sys,time
from pathlib import Path
from dra.execution import ExecutionConfig
from dra.execution_native import NativeSession
s=NativeSession(Path(sys.argv[1])/'crash','crash',ExecutionConfig(enabled=True,backend='native',network_mode='none'),deadline=time.monotonic()+60,conversation_id='crash-conversation')
s.tool('bash',{'command':'echo $$ > "$DRA_WORK/child.pid"; sleep 3; echo escaped > "$DRA_OUTPUT/escaped"','timeout_s':30})
'''
    parent=subprocess.Popen([sys.executable,'-c',script,str(tmp_path)],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        until=time.monotonic()+15
        while not (workspace.path/'work/child.pid').exists() and time.monotonic()<until:
            assert parent.poll() is None,parent.communicate()
            time.sleep(.05)
        assert (workspace.path/'work/child.pid').exists()
        with pytest.raises(ExecutionError,match='workspace_busy'):workspace.acquire()
        parent.kill();parent.wait(timeout=3)
        until=time.monotonic()+3
        while True:
            try:workspace.acquire();break
            except ExecutionError:
                if time.monotonic()>=until:raise
                time.sleep(.05)
        time.sleep(3)
        assert not (workspace.path/'output/escaped').exists()
        # Pipes close too: no surviving Bash/Node child holds them after the crash.
        parent.communicate(timeout=2)
    finally:
        if parent.poll() is None:parent.kill();parent.wait()
        parent.stdout.close();parent.stderr.close();workspace.release()


@pytest.mark.native
def test_real_native_timeout_stops_children_keeps_files(native):
    assert bash(native,'echo retained > "$DRA_OUTPUT/before.txt"')['ok']
    child='import os,time,pathlib; time.sleep(2); pathlib.Path(os.environ["DRA_OUTPUT"],"escaped.txt").write_text("bad")'
    parent=f'import subprocess,sys,time; subprocess.Popen([sys.executable,"-c",{child!r}]);time.sleep(30)'
    with pytest.raises(ExecutionError,match='command_timeout'):
        bash(native,shlex.join(['python','-c',parent]),timeout_s=.8)
    time.sleep(2)
    assert not (native.workspace.path/'output/escaped.txt').exists()
    assert bash(native,'cat "$DRA_OUTPUT/before.txt"')['stdout'].strip()=='retained'
    assert native.state=='ready'


@pytest.mark.native
def test_native_child_computation_enters_evidence_contract(tmp_path):
    from dra import simple_research as sr
    from dra.models import RetrievedDoc
    from dra.execution_tools import prepare
    from test_execution_evidence import cite
    config=sr.SimpleConfig(execution=ExecutionConfig(enabled=True,backend='native',network_mode='none'))
    ctx=sr.ToolContext(sr.open_run(tmp_path,run_id='native-evidence',query='fixture'),config)
    source,_=ctx.store.write_source(RetrievedDoc(title='fixture',source_url='https://example.org/data',snippet='',raw_content='Value=1729'))
    prepare(ctx)
    child=ctx.fork_for_subagent(config,budget=sr.Budget(config,None),delegation_id='sub1',parent_call_id='p')
    try:
        text,ok=child.execute('bash',{'source_refs':[source.ref], 'command':'python -c \'print("Value=1729")\'', 'timeout_s':60})
        assert ok,text
        receipt=json.loads(text)
        assert child.execution is ctx.execution
        assert cite(child,receipt['source_ref'],'Value=1729')[1]
        computation=ctx.store.source(receipt['source_ref']).computation
        assert computation['backend']=='native' and computation['image'] is None
        assert computation['dependency_manifest'].startswith('execution/dependencies-')
        material=computation['inputs'][0]
        assert material['path'].startswith('/workspace/input/')
        assert Path(material['exec_path']).read_text()=='Value=1729'
    finally:
        if ctx.execution:ctx.execution.close()
