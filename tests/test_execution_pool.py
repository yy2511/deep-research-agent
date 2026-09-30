import time
from types import SimpleNamespace

import pytest

from dra.execution import ExecutionConfig, ExecutionError, ExecutionSession


def pool_type():
    from dra.execution_pool import ExecutionPool
    return ExecutionPool


def fake_session(root, run, conversation=None):
    s=ExecutionSession(root/run,run,ExecutionConfig(enabled=True),conversation_id=conversation)
    s.client=SimpleNamespace(close=lambda:None)
    s.container=SimpleNamespace(id=run,attrs={'State':{'Running':True}},reload=lambda:None,
                                remove=lambda **kwargs:None)
    s.image_id='image';s.container_name=run
    return s


def test_pool_exclusive_lease_and_idle_reuse(tmp_path):
    pool=pool_type()(tmp_path,ExecutionConfig(enabled=True),start_checker=False)
    one=fake_session(tmp_path,'r1')
    two=fake_session(tmp_path,'r2','r1')
    assert pool.acquire(one) is None
    with pytest.raises(ExecutionError,match='workspace_busy'): pool.acquire(two)
    assert pool.park(one)
    assert pool.acquire(two).container.id=='r1'
    assert not pool.park(one)  # 旧轮不能释放新轮的租用。
    pool.forget(two)


def test_idle_expiry_only_reaps_idle_entries_and_capacity_evicts_oldest(tmp_path):
    cfg=ExecutionConfig(enabled=True,idle_timeout_s=10,max_containers=2)
    pool=pool_type()(tmp_path,cfg,start_checker=False)
    active=fake_session(tmp_path,'active'); warm=fake_session(tmp_path,'warm')
    removed=[]
    warm.container.remove=lambda **kw:removed.append('warm')
    pool.acquire(active);pool.acquire(warm);pool.park(warm)
    third=fake_session(tmp_path,'third')
    pool.acquire(third)
    assert removed==['warm'] and pool.active[active.workspace.key] is active
    pool.park(third)
    pool.reap(now=time.monotonic()+11)
    assert not pool.warm and pool.active[active.workspace.key] is active
    pool.forget(active)


def test_cleanup_failure_quarantines_and_retry_clears(tmp_path,monkeypatch):
    from dra import execution
    import docker
    pending=set();monkeypatch.setattr(execution,'_PENDING',pending)
    pool=pool_type()(tmp_path,ExecutionConfig(enabled=True,idle_timeout_s=1),start_checker=False)
    one=fake_session(tmp_path,'r1')
    pool.acquire(one);pool.park(one)
    def fail(**kw): raise docker.errors.DockerException('offline')
    one.container.remove=fail
    with pytest.raises(ExecutionError,match='cleanup_pending'):
        pool.reap(now=time.monotonic()+2)
    assert pending=={'r1'} and pool.warm
    one.container.remove=lambda **kw:None
    pool.reap(now=time.monotonic()+2)
    assert not pending and not pool.warm


def test_control_helper_failure_is_not_a_healthy_idle_container(tmp_path):
    pool=pool_type()(tmp_path,ExecutionConfig(enabled=True),start_checker=False)
    one=fake_session(tmp_path,'r1');one.pool=pool;one.state='ready'
    pool.acquire(one)
    one._exec=lambda *args,**kwargs:{'ok':True,'stdout':'{"ok":false,"error_code":"file_error"}'}
    one.release()
    assert one.state=='closed' and not pool.warm and not pool.active


def test_lease_helper_failure_is_not_success(tmp_path):
    one=fake_session(tmp_path,'r1')
    one._exec=lambda *args,**kwargs:{'ok':True,'stdout':'{"ok":false,"error_code":"path_denied"}'}
    with pytest.raises(ExecutionError,match='lease_failed'): one._renew_lease(600)


@pytest.mark.docker
def test_periodic_idle_checker_reclaims_without_another_request(tmp_path):
    import docker
    cfg=ExecutionConfig(enabled=True,idle_timeout_s=.2,idle_check_s=.1)
    pool=pool_type()(tmp_path,cfg)
    one=ExecutionSession(tmp_path/'r1','r1',cfg,pool=pool)
    try:
        one.tool('write_file',{'path':'/workspace/work/persist','content':'saved'})
        container=one.container;one.release()
        until=time.monotonic()+3
        while pool.warm and time.monotonic()<until: time.sleep(.05)
        assert not pool.warm
        with pytest.raises(docker.errors.NotFound): container.reload()
        assert (one.workspace.path/'work'/'persist').read_text()=='saved'
    finally: pool.shutdown()


@pytest.mark.docker
def test_real_warm_reuse_resets_inputs_budgets_and_logs(tmp_path):
    pool=pool_type()(tmp_path,ExecutionConfig(enabled=True),start_checker=False)
    one=ExecutionSession(tmp_path/'r1','r1',pool.config,pool=pool)
    two=ExecutionSession(tmp_path/'r2','r2',pool.config,pool=pool,conversation_id='r1')
    try:
        one.import_file('first.json',b'old input')
        assert one.tool('bash',{'command':'echo first > /workspace/work/data.txt'})['ok']
        cid=one.container.id
        one.release()
        assert one.state=='released' and pool.warm
        assert two.tool('read_file',{'path':'/workspace/work/data.txt'})['content']=='first\n'
        assert two.container.id==cid and two.reused
        assert two.commands==0 and not two.inputs and two.input_bytes==0
        assert not two.tool('read_file',{'path':'/workspace/input/first.json'})['ok']
        one.close()  # 旧对象不得销毁已由新轮接管的容器。
        assert two.tool('bash',{'command':'echo second'})['ok']
        assert two.commands==1
        assert one.directory!=two.directory
    finally:
        pool.shutdown()


@pytest.mark.docker
def test_idle_destroy_and_new_controller_recover_files(tmp_path):
    cfg=ExecutionConfig(enabled=True,idle_timeout_s=1)
    pool=pool_type()(tmp_path,cfg,start_checker=False)
    one=ExecutionSession(tmp_path/'r1','r1',cfg,pool=pool)
    try:
        one.tool('write_file',{'path':'/workspace/work/data','content':'saved'})
        cid=one.container.id;one.release()
        pool.reap(now=time.monotonic()+2)
        assert not pool.warm
        pool.shutdown()
        replacement=pool_type()(tmp_path,cfg,start_checker=False)
        try:
            two=ExecutionSession(tmp_path/'r2','r2',cfg,pool=replacement,conversation_id='r1')
            assert two.tool('read_file',{'path':'/workspace/work/data'})['content']=='saved'
            assert two.container.id!=cid
        finally: replacement.shutdown()
    finally: pool.shutdown()


@pytest.mark.docker
def test_background_process_is_not_parked_and_cancel_keeps_files(tmp_path):
    pool=pool_type()(tmp_path,ExecutionConfig(enabled=True),start_checker=False)
    one=ExecutionSession(tmp_path/'r1','r1',pool.config,pool=pool)
    try:
        assert one.tool('bash',{'command':'echo saved > /workspace/work/a; sleep 100 >/dev/null 2>&1 &'})['ok']
        one.release()
        assert one.state=='closed' and not pool.warm and not pool.active
        assert (one.workspace.path/'work'/'a').read_text()=='saved\n'
    finally: pool.shutdown()


@pytest.mark.docker
def test_warm_container_gets_a_new_lease_beyond_first_run_deadline(tmp_path):
    pool=pool_type()(tmp_path,ExecutionConfig(enabled=True),start_checker=False)
    one=ExecutionSession(tmp_path/'r1','r1',pool.config,pool=pool,deadline=time.monotonic()+3)
    try:
        one.tool('bash',{'command':'echo ok'})
        original=one.container;one.release()
        time.sleep(3.1)
        original.reload()
        assert original.attrs['State']['Running']
    finally: pool.shutdown()


@pytest.mark.docker
def test_cancelled_pooled_command_releases_exclusivity_and_keeps_data(tmp_path):
    import threading
    flag=threading.Event()
    pool=pool_type()(tmp_path,ExecutionConfig(enabled=True),start_checker=False)
    one=ExecutionSession(tmp_path/'r1','r1',pool.config,pool=pool,cancelled=flag.is_set)
    two=ExecutionSession(tmp_path/'r2','r2',pool.config,pool=pool,conversation_id='r1')
    try:
        one.tool('write_file',{'path':'/workspace/work/data','content':'saved'})
        timer=threading.Timer(.2,flag.set);timer.start()
        try:
            with pytest.raises(ExecutionError,match='cancelled'):
                one.tool('bash',{'command':'sleep 100','timeout_s':10})
        finally: timer.cancel()
        assert not pool.active and not pool.warm
        assert two.tool('read_file',{'path':'/workspace/work/data'})['content']=='saved'
    finally: pool.shutdown()
