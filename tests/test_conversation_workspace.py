import json
import os

import pytest

from dra.execution import ExecutionConfig, ExecutionError, ExecutionSession


def test_workspace_is_shared_only_by_explicit_conversation(tmp_path):
    first = ExecutionSession(tmp_path/'r1', 'r1', ExecutionConfig(enabled=True))
    first.workspace.prepare()
    (first.workspace.path/'work'/'draft.md').write_text('first draft')
    second = ExecutionSession(tmp_path/'r2', 'r2', first.config, conversation_id='r1')
    second.workspace.prepare()
    assert (second.workspace.path/'work'/'draft.md').read_text() == 'first draft'
    other = ExecutionSession(tmp_path/'other', 'other', first.config)
    other.workspace.prepare()
    assert not (other.workspace.path/'work'/'draft.md').exists()


def test_workspace_refuses_symlink_mount_root_and_host_path_traversal(tmp_path):
    session = ExecutionSession(tmp_path/'run', 'run', ExecutionConfig())
    outside = tmp_path/'outside'; outside.mkdir()
    session.workspace.path.parent.mkdir(parents=True)
    session.workspace.path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExecutionError, match='workspace_path_invalid'):
        session.workspace.prepare()
    with pytest.raises(ExecutionError):
        ExecutionSession(tmp_path/'run', 'run', ExecutionConfig(), conversation_id='../outside')


def test_workspace_listing_is_bounded_and_does_not_follow_links(tmp_path):
    session = ExecutionSession(tmp_path/'r', 'r', ExecutionConfig())
    session.workspace.prepare()
    (session.workspace.path/'work'/'draft.md').write_text('draft')
    outside = tmp_path/'private'; outside.mkdir()
    (outside/'secret').write_text('secret')
    (session.workspace.path/'work'/'linked').symlink_to(outside, target_is_directory=True)
    listing = session.workspace.describe()
    assert any(row['path']=='/workspace/work/draft.md' for row in listing['files'])
    assert 'secret' not in json.dumps(listing)
    assert len(listing['files']) <= 30


def test_workspace_identity_survives_moving_the_data_directory(tmp_path):
    import shutil
    first=ExecutionSession(tmp_path/'old'/'r1','r1',ExecutionConfig())
    first.workspace.prepare()
    (first.workspace.path/'work'/'draft').write_text('saved')
    shutil.copytree(tmp_path/'old',tmp_path/'new')
    restored=ExecutionSession(tmp_path/'new'/'r2','r2',ExecutionConfig(),conversation_id='r1')
    assert (restored.workspace.path/'work'/'draft').read_text()=='saved'


def test_daemon_mount_path_is_explicit_for_containerized_backend(tmp_path):
    config = ExecutionConfig(host_workspace_root='/srv/dra/runs/.workspaces')
    session = ExecutionSession(tmp_path/'r', 'r', config)
    session.workspace.prepare()
    mounts = session.workspace.mounts()
    assert len(mounts) == 2
    assert {m['Target'] for m in mounts} == {'/workspace/work','/workspace/output'}
    assert all(m['Source'].startswith('/srv/dra/runs/.workspaces/') for m in mounts)
    assert all(m['Type']=='bind' and m['ReadOnly'] is False for m in mounts)


@pytest.mark.docker
def test_real_workspace_survives_container_and_preserves_other_conversations(tmp_path):
    config = ExecutionConfig(enabled=True)
    first = ExecutionSession(tmp_path/'r1','r1',config)
    second = ExecutionSession(tmp_path/'r2','r2',config,conversation_id='r1')
    other = ExecutionSession(tmp_path/'r3','r3',config)
    try:
        receipt=first.tool('write_file',{'path':'/workspace/work/draft.md','content':'version 1'})
        assert receipt['ok'], receipt
        original_id=first.container.id
        first.close()
        receipt=second.tool('read_file',{'path':'/workspace/work/draft.md'})
        assert receipt['content']=='version 1',receipt
        assert second.container.id != original_id
        changed=second.tool('edit_file',{'path':'/workspace/work/draft.md','old_text':'1','new_text':'2','expected_sha256':receipt['sha256']})
        assert changed['ok'],changed
        assert (first.workspace.path/'work'/'draft.md').read_text()=='version 2'
        assert not other.tool('read_file',{'path':'/workspace/work/draft.md'})['ok']
        assert not second.tool('bash',{'command':f'cat {tmp_path}/r1/meta.json'})['ok']
    finally:
        first.close(); second.close(); other.close()


@pytest.mark.docker
def test_cancelled_workspace_keeps_written_files(tmp_path):
    import threading
    flag=threading.Event()
    first=ExecutionSession(tmp_path/'first','first',ExecutionConfig(enabled=True),cancelled=flag.is_set)
    second=ExecutionSession(tmp_path/'next','next',first.config,conversation_id='first')
    try:
        assert first.tool('write_file',{'path':'/workspace/work/data.json','content':'[1,2,3]'})['ok']
        flag.set()
        first.close()
        assert second.tool('read_file',{'path':'/workspace/work/data.json'})['content']=='[1,2,3]'
    finally:
        first.close(); second.close()
