"""交付、原生像素、协作文件和持久浏览器的离线回归。"""
import asyncio
import base64
import hashlib
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace as N

import pytest
from PIL import Image

from dra import artifacts, browser, provider_registry, simple_research as sr, vision
from dra.execution import ExecutionConfig, ExecutionError, ExecutionSession, MiB
from dra.mcp_client import MCPError, source_parts
from dra.mcp_session import MCPSession
from test_file_vision import original, native
from test_simple_research import Script, _turn


def context(tmp_path, **kwargs):
    kwargs.setdefault('vision', vision.VisionConfig())
    return sr.ToolContext(sr.RunStore(tmp_path, run_id='delivery', query='交付报告及原图'), sr.SimpleConfig(**kwargs))


def pixels(fmt):
    out = io.BytesIO()
    Image.new('RGB', (80, 40), 'red').save(out, fmt)
    return out.getvalue()


def test_many_large_generic_files_nested_paths_and_immutable_download(tmp_path):
    ctx = context(tmp_path, execution=ExecutionConfig(enabled=True))
    ctx.execution = ExecutionSession(tmp_path, 'delivery', ctx.config.execution)
    ctx.execution.workspace.prepare()
    folder = ctx.execution.workspace.path / 'output'
    (folder / 'photos').mkdir()
    data = b'original' + b'x' * (65 * MiB)  # 超过旧单份、总量和工作区三道门。
    (folder / 'photos/raw.bin').write_bytes(data)
    items = [{'path': 'photos/raw.bin', 'title': '原文件'}]
    for i in range(9):
        (folder / f'图({i}).jpg').write_bytes(pixels('JPEG'))
        items.append({'path': ('output/' if i % 2 else '/workspace/output/') + f'图({i}).jpg', 'title': '原图'})
    assert artifacts.prepare_finish(ctx, {'draft': '报告正文。', 'artifacts': items}) == '报告正文。'
    assert not ctx.pending_artifact_errors
    records = artifacts.publish(ctx, '报告正文。')
    assert len(records) == 10
    assert artifacts.read_committed_file(tmp_path, records[0]) == data
    assert not artifacts.previewable(records[0])
    assert all(artifacts.previewable(record) for record in records[1:])
    assert not ctx.execution.workspace.describe()['over_quota']
    for path in ('../../private', '/etc/passwd', '/workspace/work/a'):
        with pytest.raises(ExecutionError):artifacts.output_path(path)


def test_snapshot_keeps_workspace_boundary_and_hashes_bytes(tmp_path):
    session = ExecutionSession(tmp_path, 'r', ExecutionConfig(enabled=True))
    session.workspace.prepare()
    root = session.workspace.path
    target = root / 'output/a.txt'; target.write_bytes(b'hello')
    (root / 'output/link.txt').symlink_to(target)
    assert session.snapshot({'/workspace/output/a.txt': None})['/workspace/output/a.txt'] == b'hello'
    with pytest.raises(ExecutionError):session.snapshot({'/workspace/output/link.txt': None})
    with pytest.raises(ExecutionError):session.snapshot({'/workspace/output/../input/x': None})


def test_attachment_failure_reaches_model_and_repaired_finish_succeeds(tmp_path):
    ctx = context(tmp_path)
    ctx.config = ctx.config.model_copy(update={'execution': ExecutionConfig(enabled=True)})
    files = {'/workspace/output/good.csv': b'value\n2\n'}
    def snapshot(paths):
        result = {}
        for path in paths:
            if path not in files:raise ExecutionError('file_error', '文件不存在')
            result[path] = files[path]
        return result
    ctx.execution = N(snapshot=snapshot)
    first = {'draft': '已完成分析。', 'artifacts': [{'path': 'bad.csv', 'title': '表格'}]}
    final = {'draft': '已完成分析。', 'artifacts': [{'path': 'good.csv', 'title': '表格'}]}
    script = Script([_turn(('finish_research', first))], finish=('finish_research', final))
    # 工作区来自离线替身；不探测或启动 Docker。
    ctx.config = ctx.config.model_copy(update={'execution': ExecutionConfig(enabled=True)})
    ctx.execution_capability = {'available': True}
    ctx.execution_owner = N(execution_capability=ctx.execution_capability)
    budget = sr.Budget(ctx.config, None)
    result = sr.run_lead_loop(ctx.store, ctx.config, [sr.Requirement(id='goal', text='q')],
                             context=ctx, budget=budget, model_call=script)
    assert result['draft'] == '已完成分析。'
    assert len(script.seen) == 2
    assert '文件不存在' in str(script.seen[1]) and '正文和可用附件已保留' in str(script.seen[1])
    assert [p['filename'] for p in ctx.pending_artifacts] == ['good.csv']


@pytest.mark.parametrize('fmt,mime', [('JPEG', 'image/jpeg'), ('WEBP', 'image/webp'), ('GIF', 'image/gif')])
def test_native_preserves_original_encoding_and_bytes(tmp_path, monkeypatch, fmt, mime):
    native(monkeypatch)
    ctx = context(tmp_path)
    data = pixels(fmt)
    source = original(ctx, data, 'image.' + fmt.lower())
    receipt, ok = ctx.execute('view_image', {'source_ref': source.ref})
    assert ok
    url = ctx.pending_image_messages[0]['content'][1]['image_url']['url']
    assert url.startswith('data:' + mime + ';base64,')
    assert base64.b64decode(url.split(',', 1)[1]) == data
    assert json.loads(receipt)['route'] == 'native'


def test_official_vision_capability_does_not_promote_relay_and_manual_false_wins(monkeypatch):
    data = {'connections': {'official': {'base_url': 'https://api.deepseek.com'},
                            'relay': {'base_url': 'https://relay.example/v1'}}, 'discovery': {}, 'overrides': {}}
    monkeypatch.setattr(provider_registry, '_load', lambda: data)
    assert provider_registry.capacity_snapshot('official', 'deepseek-flash')['image_transport'] is True
    assert provider_registry.capacity_snapshot('relay', 'deepseek-flash')['image_transport'] is None
    data['overrides']['official'] = {'deepseek-flash': {'image_input': False}}
    assert provider_registry.capacity_snapshot('official', 'deepseek-flash')['image_input'] is False


def test_workspace_and_mcp_pixels_have_distinct_evidence_contracts(tmp_path, monkeypatch):
    native(monkeypatch)
    ctx = context(tmp_path, execution=ExecutionConfig(enabled=True))
    ctx.execution = N(snapshot=lambda paths: {p: pixels('JPEG') for p in paths})
    receipt, ok = ctx.execute('view_image', {'path': '/workspace/output/own-report.jpg'})
    assert ok
    own = json.loads(receipt)
    vision.mark_delivered(ctx, ctx.pending_image_messages)
    _, saved = ctx.execute('save_evidence', {'cards': [{'claim': '外部事实', 'quotes': [
        {'image_ref': own['image_ref'], 'observation': '外部事实'}]}]})
    assert not saved
    again, ok = ctx.execute('view_image', {'image_ref': own['image_ref']})
    assert ok and json.loads(again)['image_sha256'] == own['image_sha256']
    payload = {'content': [{'type': 'image', 'mimeType': 'image/jpeg', 'data': base64.b64encode(pixels('JPEG')).decode()}]}
    results = vision.mcp_images(ctx, payload, {'tool': 'browser_take_screenshot', 'receipt_id': 'm1'}, page_url='https://example.org/chart')
    assert results[0]['original_source_ref'] and results[0]['route'] == 'native'
    assert 'base64' not in json.dumps(results)


def test_persistent_mcp_session_uses_one_client_and_closes_in_own_task(tmp_path):
    made, calls = [], []
    class Client:
        def __init__(self, *a, **kw):made.append(self)
        async def __aenter__(self):self.task = asyncio.current_task(); return self
        async def __aexit__(self, *a):assert asyncio.current_task() is self.task; calls.append('closed')
        async def list_tools(self, **kw):return N(tools=[], next_cursor=None)
        async def call_tool(self, name, args):
            calls.append((name, args))
            return N(model_dump=lambda **kw: {'content': [{'type': 'text', 'text': str(len(calls))}]})
    session = MCPSession('mock', root=tmp_path, client_factory=Client)
    try:
        assert session.request() == []
        assert session.request('first', {})['content'][0]['text'] == '1'
        assert session.request('second', {})['content'][0]['text'] == '2'
    finally:session.close()
    assert len(made) == 1 and calls[-1] == 'closed' and not session.thread.is_alive()


@pytest.mark.browser
def test_real_local_browser_keeps_page_clicks_reports_broken_image_and_delivers_pixels(tmp_path, monkeypatch):
    native(monkeypatch)
    ctx = context(tmp_path, browser=browser.BrowserConfig(enabled=True))
    try:
        receipt, ok = ctx.execute('browser', {})
        assert ok, receipt
        assert 'browser_run_code_unsafe' not in [t['name'] for t in json.loads(receipt)['tools']]
        target = ctx.browser.workspace.path / 'output/report.html'
        target.write_text('<!doctype html><meta charset="utf-8"><h1>离线验收</h1><button onclick="this.textContent=\'已点击\'">点我</button><img src="missing.png">')
        receipt, ok = ctx.execute('browser', {'preview_path': '/workspace/output/report.html'})
        assert ok and json.loads(receipt)['inspection'], receipt
        receipt, ok = ctx.execute('browser', {'tool': 'browser_snapshot', 'arguments': {}})
        assert ok and '离线验收' in receipt and '点我' in receipt, receipt
        receipt, ok = ctx.execute('browser', {'tool': 'browser_click', 'arguments': {'target': 'button'}})
        assert ok, receipt
        receipt, ok = ctx.execute('browser', {'tool': 'browser_evaluate', 'arguments': {
            'function': '() => ({button:document.querySelector("button").textContent,broken:[...document.images].filter(i=>!i.naturalWidth).length})'}})
        assert ok and '已点击' in receipt and 'broken' in receipt, receipt
        receipt, ok = ctx.execute('browser', {'tool': 'browser_take_screenshot', 'arguments': {'scale': 'css'}, 'question': '检查离线页面'})
        assert ok and json.loads(receipt)['images'][0]['route'] == 'native', receipt
        assert ctx.pending_image_messages and not ctx.store.state['sources']
        image_ref = json.loads(receipt)['images'][0]['image_ref']
        receipt, ok = ctx.execute('view_image', {'image_ref': image_ref})
        assert ok, receipt
        session = ctx.browser.session
    finally:
        if ctx.browser:ctx.browser.close()
    assert not session.thread.is_alive()


def test_finish_can_snapshot_existing_files_during_reserved_wrapup(tmp_path):
    ctx = context(tmp_path, execution=ExecutionConfig(enabled=True), wrapup_reserve_s=120)
    ctx.budget = sr.Budget(ctx.config, time.monotonic() + 60)
    ctx.execution = ExecutionSession(tmp_path, 'delivery', ctx.config.execution, deadline=ctx.budget_deadline)
    ctx.execution.workspace.prepare()
    (ctx.execution.workspace.path / 'output/report.csv').write_bytes(b'total\n42\n')
    assert ctx.budget_expired() and not ctx.budget.expired()
    assert artifacts.prepare_finish(ctx, {'draft': '结果为42。', 'artifacts': [{'path': 'report.csv', 'title': '结果'}]}) == '结果为42。'
    assert not ctx.pending_artifact_errors and ctx.execution.state == 'new'


@pytest.mark.parametrize('end', ['budget', 'provider_failure', 'cancelled'])
def test_interrupted_attachment_repair_preserves_valid_work_unless_cancelled(tmp_path, monkeypatch, end):
    from dra import execution_tools, llm
    config = sr.SimpleConfig(execution=ExecutionConfig(enabled=True), max_llm_calls=1, reserve_wrapup_calls=0)
    if end == 'provider_failure':config = config.model_copy(update={'max_llm_calls': 2})
    ctx = context(tmp_path, **config.model_dump())
    ctx.execution = ExecutionSession(tmp_path, 'delivery', ctx.config.execution)
    ctx.execution.workspace.prepare()
    (ctx.execution.workspace.path / 'output/good.csv').write_bytes(b'total\n42\n')
    def prepare(ctx):
        ctx.execution_capability = {'available': True}
        return ctx.execution_capability
    monkeypatch.setattr(execution_tools, 'prepare', prepare)
    call_count = 0
    def model(messages, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > 1:raise RuntimeError('offline provider failure')
        return _turn(('finish_research', {'draft': '此前已完成的正文。', 'artifacts': [
            {'path': 'good.csv', 'title': '有效结果'}, {'path': 'missing.jpg', 'title': '缺少图片'}]}))
    budget = sr.Budget(ctx.config, None)
    if end == 'cancelled':
        monkeypatch.setattr(llm, 'cancellation_requested', lambda: ctx.artifact_repair_draft is not None)
    outcome = sr.run_lead_loop(ctx.store, ctx.config, [sr.Requirement(id='goal', text='q')],
        context=ctx, budget=budget, model_call=model)
    if end == 'cancelled':
        assert not outcome['draft'] and not ctx.accepted_artifacts
    else:
        assert outcome['draft'] == '此前已完成的正文。' and ctx.accepted_artifacts
        assert len(ctx.pending_artifacts) == len(ctx.pending_artifact_errors) == 1
        if end == 'provider_failure':assert outcome['stop_reason'] == 'failed+partial_delivery'


def test_two_children_create_one_shared_execution_session(tmp_path, monkeypatch):
    from dra import execution_tools as et, execution_pool
    ctx = context(tmp_path, execution=ExecutionConfig(enabled=True))
    children = [ctx.fork_for_subagent(ctx.config, budget=sr.Budget(ctx.config, None), delegation_id=f'sub{i}') for i in range(2)]
    created = []
    def make(*a, **kw):
        session = N()
        created.append(session)
        return session
    monkeypatch.setattr(et, 'ExecutionSession', make)
    monkeypatch.setattr(execution_pool, 'get_pool', lambda *a: None)
    with ThreadPoolExecutor(2) as pool:
        sessions = list(pool.map(et._session, children))
    assert len(created) == 1 and sessions[0] is sessions[1] is ctx.execution


def test_mcp_large_image_is_not_text_and_vision_failure_retains_snapshot(tmp_path, monkeypatch):
    from dra import llm, file_sources
    from test_source_download import fake_http, Response, URL
    native(monkeypatch)
    ctx = context(tmp_path)
    # 图片内容不参与文字回执的200K限制；文本仍走原有规则。
    payload = {'content': [{'type': 'image', 'mimeType': 'image/jpeg', 'data': 'A' * 250_000}]}
    assert source_parts(payload) == ([], [])
    monkeypatch.setattr(vision, 'deliver', lambda *a, **kw: (_ for _ in ()).throw(llm.LLMProtocolError('offline error')))
    payload['content'][0]['data'] = base64.b64encode(pixels('JPEG')).decode()
    results = vision.mcp_images(ctx, payload, {'tool': 'screen', 'receipt_id': 'm1'}, page_url='https://example.org')
    assert results[0]['error_code'] == 'vision_provider_error'
    assert len(ctx.image_assets) == 1 and ctx.store.state['sources'][0]['download']['transport'] == 'mcp'
    # MCP图片原件不会被误认成下载缓存，也不会破坏后续下载。
    fake_http(monkeypatch, [Response()])
    source, _, reused = file_sources.acquire(ctx, URL)
    assert not reused and source.download['requested_url'] == URL


def test_mcp_cancelled_operation_does_not_destroy_session(tmp_path):
    class Client:
        def __init__(self, *a, **kw):pass
        async def __aenter__(self):return self
        async def __aexit__(self, *a):pass
        async def call_tool(self, name, args):
            if name == 'slow':await asyncio.Event().wait()
            return N(model_dump=lambda **kw: {'content': [{'type': 'text', 'text': 'still open'}]})
    session = MCPSession('mock', root=tmp_path, client_factory=Client)
    try:
        with pytest.raises(MCPError):session.request('slow', {}, deadline=time.monotonic() + .1)
        assert session.request('next', {}, deadline=time.monotonic() + 1)['content'][0]['text'] == 'still open'
    finally:session.close()
    assert not session.thread.is_alive()


@pytest.mark.parametrize('name,data,preview_status,mime', [
    ('photo.jpg', pixels('JPEG'), 200, 'image/jpeg'),
    ('photo.webp', pixels('WEBP'), 200, 'image/webp'),
    ('photo.gif', pixels('GIF'), 200, 'image/gif'),
    ('report.pdf', (Path(__file__).parents[1] / 'fixtures/library_demo/orchid-data-policy.pdf').read_bytes(), 200, 'application/pdf'),
    ('source.bin', b'\x00\xfforiginal', 415, 'application/octet-stream'),
])
def test_artifact_http_download_and_preview_preserve_actual_bytes(tmp_path, monkeypatch, name, data, preview_status, mime):
    from fastapi.testclient import TestClient
    from dra import web
    ctx = context(tmp_path, execution=ExecutionConfig(enabled=True))
    ctx.execution = ExecutionSession(tmp_path, 'delivery', ctx.config.execution)
    ctx.execution.workspace.prepare()
    (ctx.execution.workspace.path / 'output' / name).write_bytes(data)
    artifacts.prepare_finish(ctx, {'draft': '完整正文。', 'artifacts': [{'path': name, 'title': '文件'}]})
    records = artifacts.publish(ctx, '完整正文。')
    monkeypatch.setattr(web, 'STORE', N(get_meta=lambda run: {}, _dir=lambda run: tmp_path,
                                     read_simple_result=lambda run: {'artifacts': records, 'stop_reason': 'finished'}))
    client = TestClient(web.app)
    route = '/api/runs/delivery/artifacts/' + records[0]['artifact_id']
    downloaded = client.get(route + '/download')
    assert downloaded.status_code == 200 and downloaded.content == data
    assert downloaded.headers['content-type'].startswith(mime)
    preview = client.get(route + '/preview')
    assert preview.status_code == preview_status
    if preview_status == 200:assert preview.content == data and preview.headers['content-type'].startswith(mime)
    (tmp_path / records[0]['storage_path']).write_bytes(b'tampered')
    assert client.get(route + '/download').status_code == 409
