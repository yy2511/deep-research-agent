"""真实 Docker 边界验收；显式 --run-docker 时环境缺失必须失败。"""
import threading
import time
import hashlib
import json
import os

import pytest

from dra.execution import ExecutionSession,ExecutionConfig,ExecutionError,MiB

pytestmark=pytest.mark.docker
TEST_IMAGE=os.environ.get('DRA_EXECUTION_TEST_IMAGE','dra-sandbox:local')


@pytest.fixture
def session(tmp_path):
    s=ExecutionSession(tmp_path/'run','run',ExecutionConfig(enabled=True,image=TEST_IMAGE))
    try: s.start(); yield s
    finally: s.close()


def test_real_files_inputs_freeze_export_and_isolation(session,tmp_path):
    s=session
    path=s.import_file('data.txt',b'1,2,3')
    r=s.tool('write_file',{'path':'/workspace/output/report.md','content':'# 中文\n计算 6'})
    assert r['ok'],r
    r=s.tool('bash',{'command':f'cat {path}; python -c "print(sum([1,2,3]))"'})
    assert r['ok'] and '6' in r['stdout'],r
    assert s.snapshot({'/workspace/output/report.md':1000})['/workspace/output/report.md']=='# 中文\n计算 6'.encode()
    sentinel=tmp_path/'host-secret'; sentinel.write_text('not in image')
    r=s.tool('bash',{'command':f'test ! -e {sentinel} && test ! -e /data && test ! -e /var/run/docker.sock && test -z "$OPENAI_API_KEY"'})
    assert r['ok'],r
    for command in ['echo bad >/workspace/input/data.txt','touch /usr/forbidden','kill -TERM 1']:
        assert not s.tool('bash',{'command':command})['ok']
    r=s.tool('bash',{'command':'python -c "import socket; socket.create_connection((\'1.1.1.1\',443),1)"'})
    assert not r['ok']
    s.tool('bash',{'command':'ln -s /etc/passwd /workspace/output/link'})
    with pytest.raises(ExecutionError):s.snapshot({'/workspace/output/link':10000})


def test_large_input_is_complete_readable_and_readonly(session):
    raw = b'county,value\n' + b'Alpha,1\n' * (65 * MiB // 8)
    path = session.import_file('large.csv', raw)
    receipt = session.tool('bash', {'command': f'''python - <<'PY'
import hashlib, json
from pathlib import Path
p = Path({path!r})
with p.open('rb') as f:
    digest = hashlib.file_digest(f, 'sha256').hexdigest()
try:
    p.write_bytes(b'changed')
except OSError:
    readonly = True
else:
    readonly = False
print(json.dumps(dict(size=p.stat().st_size, sha256=digest, readonly=readonly)))
PY'''})
    assert receipt['ok'], receipt
    actual = json.loads(receipt['stdout'])
    assert actual == {'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(), 'readonly': True}


def test_no_output_timeout_stops_command_group_and_keeps_session(session):
    s=session; container=s.container
    # First Bash call initializes the conversation's dependency environment.
    assert s.tool('bash',{'command':'true'})['ok']
    started=time.monotonic()
    with pytest.raises(ExecutionError,match='command_timeout'):
        s.tool('bash',{'command':'sleep 100 & echo $! >/workspace/work/child.pid; wait','timeout_s':.4})
    assert time.monotonic()-started<5
    assert s.state=='ready' and s.container is container
    r=s.tool('bash',{'command':'test ! -e /proc/$(cat /workspace/work/child.pid)/stat && echo resumed'})
    assert r['ok'] and 'resumed' in r['stdout']


def test_cancel_stops_silent_command(session):
    flag=threading.Event(); session.cancelled=flag.is_set
    timer=threading.Timer(.3,flag.set); timer.start()
    try:
        with pytest.raises(ExecutionError,match='cancelled'):
            session.tool('bash',{'command':'sleep 100','timeout_s':10})
    finally:timer.cancel()
    assert session.state=='ready'
    flag.clear()
    assert session.tool('bash',{'command':'echo resumed'})['ok']


def test_log_is_bounded_but_workspace_has_no_removed_64mib_cap(session):
    r=session.tool('bash',{'command':"python -c 'import sys; sys.stdout.write(\"x\"*2000000)'"})
    assert r['ok'] and r['truncated'] and r['log_truncated']
    assert len(r['stdout'].encode())<=4096 and session.log_bytes<=MiB
    r=session.tool('bash',{'command':'dd if=/dev/zero of=/workspace/output/full bs=1M count=65 2>/dev/null'})
    assert r['ok'],r
    assert (session.workspace.path/'output/full').stat().st_size==65*MiB
    assert session.tool('bash',{'command':'rm /workspace/output/full'})['ok']


def test_process_limit_stops_spawning_without_killing_controller(session):
    r=session.tool('bash',{'command':"python -c 'import subprocess; ps=[]\ntry:\n for _ in range(150): ps.append(subprocess.Popen([\"sleep\",\"10\"]))\nexcept OSError: print(\"pids_limited\")\nfinally:\n for p in ps: p.kill()\n for p in ps: p.wait()'"})
    assert r['ok'] and 'pids_limited' in r['stdout'],r


def test_oom_command_keeps_healthy_controller(tmp_path):
    s=ExecutionSession(tmp_path/'oom','oom',ExecutionConfig(enabled=True,image=TEST_IMAGE,memory_mb=128))
    try:
        s.start()
        with pytest.raises(ExecutionError):
            s.tool('bash',{'command':'python -c "x=bytearray(256*1024*1024)"','timeout_s':10})
        assert s.state=='ready'
        assert s.tool('bash',{'command':'echo resumed'})['ok']
    finally:s.close()


def test_reaper_respects_instance_and_active_run(tmp_path):
    from dra.execution import reap_orphans
    cfg=ExecutionConfig(enabled=True,image=TEST_IMAGE)
    a=ExecutionSession(tmp_path/'one'/'a','a',cfg)
    b=ExecutionSession(tmp_path/'two'/'b','b',cfg)
    try:
        a.start(); b.start()
        assert reap_orphans(tmp_path/'one',cfg,{'a'})==[]
        assert reap_orphans(tmp_path/'one',cfg)==[a.container.id]
        assert b.tool('bash',{'command':'echo alive'})['ok']
    finally:a.close(); b.close()


def test_container_self_expires_without_controller_cleanup(tmp_path):
    s=ExecutionSession(tmp_path/'ttl','ttl',ExecutionConfig(enabled=True,image=TEST_IMAGE),deadline=time.monotonic()+2)
    try:
        s.start()
        assert s.container.wait(timeout=5)['StatusCode']==0
        s.container.reload()
        assert not s.container.attrs['State']['Running']
    finally:s.close()
