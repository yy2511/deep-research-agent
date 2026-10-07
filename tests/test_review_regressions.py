"""Claude 复核问题：只用本机可控进程和协议替身，不调用 Docker、模型或外部网站。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace as N

import pytest

from dra import artifacts, browser, execution_tools, llm, simple_research as sr
from dra.execution import ExecutionConfig, ExecutionError, ExecutionSession
from dra.execution_process import RUN, STOP, CLEANUP
from test_delivery_browser import context


@pytest.mark.parametrize('execution_enabled', [False, True])
def test_browser_only_files_publish_without_starting_execution(tmp_path, monkeypatch, execution_enabled):
    ctx = context(tmp_path, browser={'enabled': True}, execution=ExecutionConfig(enabled=execution_enabled))
    monkeypatch.setattr('dra.execution.docker_client', lambda *a: pytest.fail('must not start Docker'))
    ctx.browser = browser.BrowserSession(ctx)
    ctx.browser.workspace.prepare()
    root = ctx.browser.workspace.path
    (root / 'work/report.md').write_text('报告正文。')
    (root / 'output/report.html').write_text('<h1>报告</h1>')
    draft = artifacts.prepare_finish(ctx, {'draft_file': '/workspace/work/report.md',
        'artifacts': [{'path': 'report.html', 'title': '报告'}, {'path': 'missing.pdf', 'title': '缺失附件'}]})
    records = artifacts.publish(ctx, draft)
    assert draft == '报告正文。' and ctx.execution is None
    assert len(ctx.pending_artifact_errors) == 1 and ctx.pending_artifact_errors[0]['error_code'] == 'file_error'
    assert artifacts.read_committed_file(tmp_path, records[0]) == '<h1>报告</h1>'.encode()
    provenance = json.loads((tmp_path / 'artifacts/manifest.json').read_text())['provenance']
    assert provenance['image_id'] is None and provenance['inputs'] == {}


def test_stdio_passes_browser_path_without_provider_credentials(tmp_path, monkeypatch):
    ctx = context(tmp_path, browser={'enabled': True})
    captured = []
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', '/opt/playwright-browsers')
    monkeypatch.setenv('FAKE_PROVIDER_API_KEY', 'fixture-only')
    monkeypatch.setattr(browser, 'availability', lambda: {'available': True})
    monkeypatch.setattr(browser, 'ThreadingHTTPServer', lambda *a: N(server_port=8000,
        serve_forever=lambda: None, shutdown=lambda: None, server_close=lambda: None))
    monkeypatch.setattr(browser, 'stdio_client', lambda params, **kw: captured.append(params))
    monkeypatch.setattr(browser, 'MCPSession', lambda *a, **kw: N(close=lambda: None))
    session = browser.BrowserSession(ctx)
    try:session.start()
    finally:session.close()
    assert captured[0].env == {'PLAYWRIGHT_BROWSERS_PATH': '/opt/playwright-browsers'}


def controlled_browser(ctx, monkeypatch):
    session = browser.BrowserSession(ctx)
    session.workspace.prepare()
    session.preview_origin = 'http://127.0.0.1:8000'
    names = ['browser_snapshot', 'browser_evaluate', 'browser_type', 'browser_take_screenshot', 'browser_navigate']
    session.tools = {n: {'name': n, 'inputSchema': {'type': 'object'}} for n in names}
    state = {'id': 'page1', 'url': 'https://fixture.invalid/article', 'modified': False}
    calls = []
    def request(name, args, **kwargs):
        calls.append((name, args))
        if name == 'browser_evaluate' and args['function'] == browser._PAGE_STATE:
            return {'content': [{'type': 'text', 'text': '### Result\n' + json.dumps(state)}]}
        if name == 'browser_evaluate' and args['function'] == browser._BEFORE_EVALUATE:
            return {'content': [{'type': 'text', 'text': '### Result\nundefined'}]}
        if name == 'browser_navigate':
            state.update(id='page2', url=args['url'], modified=False)
        if name == 'browser_type' or (name == 'browser_evaluate' and args['function'] == 'edit DOM'):
            state['modified'] = True
        raw = '### Result\n"MODEL_AUTHORED_TEXT"\n### Ran Playwright code\n```js\nMODEL_CODE\n```\n'
        if name == 'browser_snapshot':
            raw += '### Page\n- Page URL: https://fixture.invalid/article\n### Snapshot\n```yaml\n- paragraph: Published rainfall was 42 mm.\n```\n### Events\nDEBUG_LINE'
        return {'content': [{'type': 'text', 'text': raw}]}
    session.session = N(request=request)
    monkeypatch.setattr(session, 'start', lambda: None)
    ctx.browser = session
    return session, state, calls


def accepted(ctx, quote):
    raw, _ = ctx.save_evidence({'cards': [{'claim': 'Test fact', 'quote': quote}]})
    return json.loads(raw)['results'][0]['accepted']


def test_browser_separates_script_results_and_page_snapshots(tmp_path, monkeypatch):
    ctx = context(tmp_path, browser={'enabled': True}, vision={'enabled': False})
    session, state, calls = controlled_browser(ctx, monkeypatch)
    result = session.invoke({'tool': 'browser_evaluate', 'arguments': {'function': 'read only'}})
    assert 'MODEL_AUTHORED_TEXT' in str(result) and not ctx.store.state['sources']
    assert not accepted(ctx, 'MODEL_AUTHORED_TEXT')
    result = session.invoke({'tool': 'browser_snapshot'})
    assert result['sources'] and result['evidence_eligible']
    ref = result['sources'][0]['source_ref']
    assert result['sources'][0]['source_kind'] == 'browser_snapshot'
    assert ctx.store.read_source_text(ref) == '- paragraph: Published rainfall was 42 mm.'
    assert accepted(ctx, 'Published rainfall was 42 mm.')
    assert ctx.store.cards()[-1].evidence_quotes()[0].source_kind == 'browser_snapshot'
    assert not accepted(ctx, 'MODEL_CODE') and not accepted(ctx, 'DEBUG_LINE')
    assert ('browser_evaluate', {'function': browser._BEFORE_EVALUATE}) in calls


@pytest.mark.parametrize('tool,args', [('browser_evaluate', {'function': 'edit DOM'}), ('browser_type', {'text': 'made up'})])
def test_edited_page_and_its_images_remain_inspection_until_fresh_document(tmp_path, monkeypatch, tool, args):
    from dra import vision
    ctx = context(tmp_path, browser={'enabled': True}, vision={'enabled': False})
    session, state, calls = controlled_browser(ctx, monkeypatch)
    image_calls = []
    monkeypatch.setattr(vision, 'mcp_images', lambda *a, **kw: image_calls.append(kw) or [])
    session.invoke({'tool': tool, 'arguments': args})
    result = session.invoke({'tool': 'browser_snapshot'})
    assert not result['evidence_eligible'] and 'sources' not in result
    assert not ctx.store.state['sources'] and not accepted(ctx, 'Published rainfall was 42 mm.')
    session.invoke({'tool': 'browser_take_screenshot'})
    assert image_calls[-1]['inspection']
    session.invoke({'tool': 'browser_navigate', 'arguments': {'url': 'https://fixture.invalid/article'}})
    assert session.invoke({'tool': 'browser_snapshot'})['sources']


@pytest.mark.browser
def test_real_local_page_distinguishes_script_output_edits_and_delayed_changes(tmp_path):
    """仅访问本测试启动的 localhost；无模型、视觉、搜索或外部网络。"""
    class Page(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(b'<p id="fact">Published rainfall was 42 mm.</p><input aria-label="note">')
        def log_message(self, *a):pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ctx = context(tmp_path, browser={'enabled': True}, vision={'enabled': False})
    session = browser.BrowserSession(ctx)
    url = f'http://127.0.0.1:{server.server_port}/'
    try:
        session.invoke({'tool': 'browser_navigate', 'arguments': {'url': url}})
        result = session.invoke({'tool': 'browser_evaluate', 'arguments': {'function': '() => "model-created string"'}})
        assert result['evidence_eligible'] and not accepted(ctx, 'model-created string')
        snapshot = session.invoke({'tool': 'browser_snapshot'})
        assert snapshot['sources'] and accepted(ctx, 'Published rainfall was 42 mm.')
        assert ctx.store.cards()[-1].evidence_quotes()[0].source_kind == 'browser_snapshot'
        # 文件快照也使用实际内容，而不是可点击的文件链接。
        assert session.invoke({'tool': 'browser_snapshot', 'arguments': {'filename': 'work/page.yml'}})['sources']
        for script in [
            '() => { document.querySelector("#fact").textContent = "invented fact"; }',
            '() => { document.querySelector("input").value = "invented value"; }',
            '() => { setTimeout(() => document.querySelector("#fact").textContent = "invented later", 100); }',
        ]:
            session.invoke({'tool': 'browser_navigate', 'arguments': {'url': url}})
            session.invoke({'tool': 'browser_evaluate', 'arguments': {'function': script}})
            session.invoke({'tool': 'browser_wait_for', 'arguments': {'time': .2}})
            changed = session.invoke({'tool': 'browser_snapshot'})
            assert not changed['evidence_eligible'] and 'sources' not in changed
            assert not accepted(ctx, 'invented fact') and not accepted(ctx, 'invented value') and not accepted(ctx, 'invented later')
        session.invoke({'tool': 'browser_navigate', 'arguments': {'url': url}})
        assert session.invoke({'tool': 'browser_snapshot'})['sources']
    finally:
        session.close(); server.shutdown(); server.server_close(); thread.join()


@pytest.mark.parametrize('failure', ['cancelled', 'command_timeout', 'process_killed'])
def test_cancelled_child_keeps_shared_execution_inputs_files_and_budgets(tmp_path, monkeypatch, failure):
    parent = context(tmp_path, execution=ExecutionConfig(enabled=True))
    child = parent.fork_for_subagent(parent.config, budget=sr.Budget(parent.config, None), delegation_id='child')
    session = ExecutionSession(tmp_path, 'delivery', parent.config.execution, deadline=time.monotonic() + 600,
                               cancelled=llm.cancellation_requested)
    parent.execution = session
    session.state = 'ready'
    session.workspace.prepare()
    session.import_file('data.txt', b'original input')
    (session.workspace.path / 'output/saved.csv').write_text('saved\n')
    session.commands, session.log_bytes = 3, 123
    cancel = threading.Event()
    started, stopped = [], []
    class API:
        def exec_create(self, container, argv, **kw):
            started.append(argv)
            return {'Id': 'exec'}
        def exec_start(self, *a, **kw):
            read, write = socket.socketpair()
            write.sendall(struct.pack('>BxxxI', 1, 7) + b'resumed')
            write.close()
            if len(started) == 1 and failure == 'cancelled':cancel.set()
            return read
        def exec_inspect(self, *a):return {'Running': False, 'ExitCode': 137 if failure == 'process_killed' and len(started) == 1 else 0}
    session.client = N(api=API(), close=lambda: pytest.fail('must keep shared client'))
    session.container = N(id='shared', remove=lambda **kw: pytest.fail('must keep shared container'),
        exec_run=lambda argv, **kw: stopped.append(argv) or N(exit_code=0))
    with llm.execution_scope(cancel_event=cancel):
        raw, ok = execution_tools.execute(child, 'bash', {'command': 'slow command',
            'timeout_s': 1e-9 if failure == 'command_timeout' else 30})
    assert not ok and json.loads(raw)['error_code'] == failure
    assert json.loads(raw)['workspace_available'] and session.state == 'ready'
    assert STOP in stopped[0] and RUN in started[0]
    with llm.execution_scope():
        raw, ok = execution_tools.execute(parent, 'bash', {'command': 'continue'})
    assert ok and json.loads(raw)['stdout'] == 'resumed'
    assert session.commands == 5 and session.log_bytes == (137 if failure == 'process_killed' else 130)
    assert session.inputs['data.txt'] and session.input_bytes == len(b'original input')
    assert (session.workspace.path / 'input/data.txt').read_bytes() == b'original input'
    assert (session.workspace.path / 'output/saved.csv').read_text() == 'saved\n'


def test_cancellation_while_acquiring_container_allows_next_agent_to_start(tmp_path, monkeypatch):
    acquisitions = []
    client = N(images=N(get=lambda _: N(id='image', labels={'dra.sandbox.protocol': '3'})), close=lambda: None)
    container = N(id='shared', remove=lambda **kw: None)
    def acquire(session):
        acquisitions.append(session)
        if len(acquisitions) == 1:raise ExecutionError('cancelled')
        return N(client=client, container=container, name='shared', image_id='image')
    pool = N(acquire=acquire, forget=lambda _: None)
    session = ExecutionSession(tmp_path, 'delivery', ExecutionConfig(enabled=True), pool=pool)
    session.workspace.prepare()
    (session.workspace.path / 'work/saved').write_text('existing file')
    monkeypatch.setattr(session, '_renew_lease', lambda _: None)
    with pytest.raises(ExecutionError, match='cancelled'):session.start()
    assert session.state == 'new'
    session.start()
    assert session.state == 'ready' and session.container is container and len(acquisitions) == 2
    assert (session.workspace.path / 'work/saved').read_text() == 'existing file'
    session.close()


def test_real_command_group_stop_preserves_unrelated_process(tmp_path):
    control = tmp_path / 'command'
    ready = tmp_path / 'ready'
    script = 'import pathlib,subprocess,sys,time; c=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); pathlib.Path(sys.argv[1]).write_text(str(c.pid)); time.sleep(60)'
    other = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'])
    target = subprocess.Popen([sys.executable, '-I', '-c', RUN, str(control), sys.executable, '-c', script, str(ready)])
    try:
        until = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < until:time.sleep(.01)
        assert ready.exists()
        subprocess.run([sys.executable, '-I', '-c', STOP, str(control)], check=True, timeout=5)
        assert target.wait(timeout=5) == 137
        assert other.poll() is None and not control.exists()
        status = subprocess.run(['ps', '-p', ready.read_text(), '-o', 'stat='], capture_output=True, text=True)
        assert not status.stdout.strip() or status.stdout.lstrip().startswith('Z')
        # 同一个控制脚本也覆盖取消先于实际命令启动的路径。
        marker = tmp_path / 'should-not-exist'
        subprocess.run([sys.executable, '-I', '-c', STOP, str(control)], check=True, timeout=5)
        result = subprocess.run([sys.executable, '-I', '-c', RUN, str(control), sys.executable,
            '-c', 'import pathlib,sys;pathlib.Path(sys.argv[1]).touch()', str(marker)], timeout=5)
        assert result.returncode == 143 and not marker.exists()
    finally:
        if target.poll() is None:
            subprocess.run([sys.executable, '-I', '-c', STOP, str(control)], timeout=5)
            target.wait(timeout=5)
        other.kill(); other.wait(timeout=5)


def test_group_control_survives_parent_exit_and_preserves_stdio(tmp_path):
    control = tmp_path / 'command'
    command = subprocess.Popen([sys.executable, '-I', '-c', RUN, str(control), sys.executable,
        '-c', 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"])'], stdout=subprocess.PIPE)
    try:
        assert command.wait(timeout=5) == 0
        assert control.exists() and not select.select([command.stdout], [], [], .05)[0]
        subprocess.run([sys.executable, '-I', '-c', STOP, str(control)], check=True, timeout=5)
        assert select.select([command.stdout], [], [], 5)[0] and command.stdout.read() == b''
    finally:
        if control.exists():subprocess.run([sys.executable, '-I', '-c', STOP, str(control)], timeout=5)
        command.wait(timeout=5); command.stdout.close()
    result = subprocess.run([sys.executable, '-I', '-c', RUN, str(control), sys.executable,
        '-c', 'import sys;sys.stdout.buffer.write(sys.stdin.buffer.read());sys.stderr.write("err");sys.exit(7)'],
        input=b'input\x00bytes', capture_output=True, timeout=5)
    assert result.returncode == 7 and result.stdout == b'input\x00bytes' and result.stderr == b'err'
    subprocess.run([sys.executable, '-I', '-c', CLEANUP, str(control)], check=True, timeout=5)
    assert not control.exists()
