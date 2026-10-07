"""Original/derived/visual contracts, real local bytes and mocked transports only."""
import base64
import hashlib
import io
import json
from copy import deepcopy

import pytest
from PIL import Image
from pypdf import PdfWriter

from dra import context_budget, context_compaction, provider_registry, simple_research as sr, vision
from dra.execution import ExecutionConfig, ExecutionError
from dra.models import RetrievedDoc
from test_document_library import TinyEncoder
from test_execution_evidence import LocalSession
from test_research_history import child_context, history_fixture
from test_source_download import Response, fake_http


def png():
    output = io.BytesIO()
    Image.new('RGB', (80, 40), 'red').save(output, 'PNG')
    return output.getvalue()


def context(tmp_path, **kwargs):
    kwargs.setdefault('vision', vision.VisionConfig())
    return sr.ToolContext(sr.RunStore(tmp_path, run_id='vision', query='读取图表'), sr.SimpleConfig(**kwargs))


def original(ctx, data=None, name='chart.png'):
    data = data or png()
    source = ctx.store.write_original(RetrievedDoc(title=name, source_url='https://example.org/'+name,
        raw_content='', snippet=''), data, download={'filename': name, 'content_type': 'application/octet-stream',
            'original_sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
            'requested_url': 'https://example.org/'+name, 'final_url': 'https://example.org/'+name})
    return source


def native(monkeypatch):
    monkeypatch.setattr(provider_registry, 'capacity_snapshot', lambda *a: {'image_input': True, 'image_transport': True})


def test_binary_download_keeps_original_and_does_not_grant_quotes(tmp_path, monkeypatch):
    data = png()
    fake_http(monkeypatch, [Response(data=data, headers={'Content-Type': 'image/png'})])
    ctx = context(tmp_path)
    ctx.tool_role = 'subagent'
    receipt, ok = ctx.execute('read_source', {'url': 'https://example.org/chart.png'})
    payload = json.loads(receipt)
    assert ok and payload['snapshot_kind'] == 'original' and not payload['quotable']
    ref = payload['source_ref']
    assert (ctx.store.sources_dir / (ref+'.original')).read_bytes() == data
    assert not (ctx.store.sources_dir / (ref+'.txt')).exists()
    _, saved = ctx.execute('save_evidence', {'cards': [{'claim': '图中数值', 'source_ref': ref, 'quote': '42'}]})
    assert not saved and not ctx.store.cards()


def test_unrecognized_bytes_are_preserved(tmp_path, monkeypatch):
    from dra import source_download
    fake_http(monkeypatch, [Response(data=b'\xff\x00\x80', headers={'Content-Type': 'application/octet-stream'})])
    data, text, metadata = source_download.fetch('https://example.org/unknown.blob')
    assert data == b'\xff\x00\x80' and text is None and metadata['encoding'] is None


@pytest.mark.parametrize('name,data', [('unknown.blob', b'\xff\x00'), ('chart.png', png()), ('binary.txt', b'\x00abc')])
def test_upload_binary_can_be_selected_without_text_index(tmp_path, name, data):
    from dra.library import LibraryStore
    library = LibraryStore(tmp_path/'library', encoder=TinyEncoder())
    try:
        version = library.import_bytes(name, data, wait=True)
        assert version['status'] == 'original'
        selected = library.pin([version['document_id']])
        library.validate_selection(selected)
        assert library.original(version['version_id']) == data
        assert library.search('图表', selected) == []
    finally:
        library.close()


def test_native_pixels_not_json_text_and_observation_binding(tmp_path, monkeypatch):
    native(monkeypatch)
    ctx = context(tmp_path)
    source = original(ctx)
    receipt, ok = ctx.execute('view_image', {'source_ref': source.ref, 'region': [10, 5, 60, 30]})
    info = json.loads(receipt)
    assert ok and info['route'] == 'native' and info['pixel_size'] == [50, 25]
    assert 'base64' not in receipt
    messages = ctx.pending_image_messages
    pixels = base64.b64decode(messages[0]['content'][1]['image_url']['url'].split(',', 1)[1])
    assert Image.open(io.BytesIO(pixels)).size == (50, 25)
    card = {'claim': '图像为红色', 'quotes': [{'image_ref': info['image_ref'], 'observation': '裁剪区域为红色。'}]}
    _, ok = ctx.execute('save_evidence', {'cards': [card]})
    assert not ok  # queued pixels have not yet reached a model request
    vision.mark_delivered(ctx, messages)
    _, ok = ctx.execute('save_evidence', {'cards': [card]})
    assert ok
    quote = ctx.store.cards()[0].evidence_quotes()[0]
    assert quote.source_kind == 'observation' and quote.location.startswith('image=')
    assert quote.observation['original_source_ref'] == source.ref
    again, ok = ctx.execute('view_image', {'source_ref': source.ref, 'region': [10, 5, 60, 30]})
    assert ok and json.loads(again)['image_ref'] == info['image_ref']


def test_pdf_page_crop_and_bounds(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=120)
    writer.add_blank_page(width=200, height=80)
    buf = io.BytesIO(); writer.write(buf)
    ctx = context(tmp_path)
    source = original(ctx, buf.getvalue(), 'scan.pdf')
    pixels, info = vision.render(ctx.store, source, {'page': 2, 'dpi': 72, 'region': [10, 5, 100, 50]})
    assert Image.open(io.BytesIO(pixels)).size == (90, 45)
    assert info['page'] == 2 and info['pdf_size'] == [200, 80] and info['pdf_region_top_left'] == [10, 5, 100, 50]
    with pytest.raises(ExecutionError):
        vision.render(ctx.store, source, {'page': 3})


@pytest.mark.parametrize('summary', [False, True])
def test_original_by_known_ref_gets_pdf_without_rebinding_text(tmp_path, monkeypatch, summary):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=120)
    buf = io.BytesIO(); writer.write(buf)
    data = buf.getvalue()
    fake_http(monkeypatch, [Response(data=data, headers={'Content-Type': 'application/pdf'})])
    ctx = context(tmp_path)
    doc = RetrievedDoc(title='historic PDF', source_url='https://example.org/chart.pdf',
                       snippet='summary', raw_content='指定版本数据：5.00。')
    source = ctx.store.register_summary(doc) if summary else ctx.store.write_source(doc)[0]
    if not summary:
        ctx.read_source({'source_ref': source.ref})
        ctx.save_evidence({'cards': [{'claim': '数据5.00', 'source_ref': source.ref, 'quote': '指定版本数据：5.00。'}]})
    before_sessions, before_aliases = dict(ctx.sessions), dict(ctx.aliases)
    before_cards = [c.model_dump() for c in ctx.store.cards()]
    before_exposure = deepcopy(ctx.read_exposures)
    receipt, ok = ctx.read_source({'source_ref': source.ref, 'original': True})
    ref = json.loads(receipt)['source_ref']
    assert ok and ref != source.ref
    assert ctx.sessions == before_sessions and ctx.aliases == before_aliases
    assert ctx.read_exposures == before_exposure
    assert [c.model_dump() for c in ctx.store.cards()] == before_cards
    if not summary:
        assert ctx.store.read_source_text(source.ref) == doc.raw_content
    original = ctx.store.original_source(ref)
    pixels, details = vision.render(ctx.store, original, {'page': 1, 'dpi': 72})
    assert Image.open(io.BytesIO(pixels)).size == (100, 120) and details['page'] == 1
    # Same old text ref reuses the original byte snapshot on the second call.
    again, ok = ctx.read_source({'source_ref': source.ref, 'original': True})
    assert ok and json.loads(again)['source_ref'] == ref


def test_original_ref_checks_bytes_and_reports_recovery_url(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    source, _ = ctx.store.write_source(RetrievedDoc(title='chart', source_url='https://example.org/chart.pdf',
        raw_content='Text without pixels', snippet=''))
    receipt, ok = ctx.execute('view_image', {'source_ref': source.ref})
    assert not ok and 'original_required' in receipt and source.url in receipt and source.ref in receipt
    stored = original(ctx)
    (ctx.store.sources_dir / (stored.ref + '.original')).write_bytes(b'changed')
    receipt, ok = ctx.read_source({'source_ref': stored.ref, 'original': True})
    assert not ok and '校验失败' in receipt


def test_selected_library_summary_gets_local_original_without_network(tmp_path, monkeypatch):
    from dra import library as library_module, file_sources
    library = library_module.LibraryStore(tmp_path / 'library', encoder=TinyEncoder())
    monkeypatch.setattr(library_module, 'get_library', lambda: library)
    monkeypatch.setattr(file_sources, 'fetch_original', lambda *a, **kw: pytest.fail('library contacted network'))
    data = b'county,value\r\nAlpha,5\r\n'
    try:
        version = library.import_bytes('data.csv', data, wait=True)
        selection = library.pin([version['document_id']])
        ctx = context(tmp_path / 'run', source_mode='library', library_selection=selection)
        summary = ctx.store.register_summary(library.search('Alpha', selection)[0])
        receipt, ok = ctx.read_source({'source_ref': summary.ref, 'original': True})
        ref = json.loads(receipt)['source_ref']
        assert ok and file_sources.original_bytes(ctx.store, ctx.store.original_source(ref)) == data
        # Reuse the now-persisted full source without fetching or granting quote exposure.
        receipt, ok = ctx.read_source({'source_ref': ref, 'original': True})
        assert ok and json.loads(receipt)['source_ref'] == ref
        assert not ctx.read_exposures
    finally:
        library.close()


@pytest.mark.parametrize('derived', ['computation', 'observation', None])
def test_derived_or_url_less_sources_do_not_download_a_web_original(tmp_path, monkeypatch, derived):
    from dra import file_sources
    ctx = context(tmp_path)
    source, _ = ctx.store.write_source(RetrievedDoc(title='generated', raw_content='derived text', snippet='',
        source_url='https://example.org/derived' if derived else ''))
    entry = next(s for s in ctx.store.state['sources'] if s['ref'] == source.ref)
    if derived == 'computation':
        entry[derived] = {'declared_source_refs': []}
    elif derived == 'observation':
        entry[derived] = {'original_source_ref': 'missing'}
    monkeypatch.setattr(file_sources, 'fetch_original', lambda *a, **kw: pytest.fail('derived source downloaded'))
    receipt, ok = ctx.read_source({'source_ref': source.ref, 'original': True})
    assert not ok and ('不是文件原件' in receipt if derived else '没有原件字节' in receipt)



def test_auxiliary_routing_and_exact_observation_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_registry, 'capacity_snapshot', lambda *a: {'image_input': True})
    ctx = context(tmp_path, vision=vision.VisionConfig(provider='fake', model='gemini-2.5-flash'))
    source = original(ctx)
    requests = []
    def chat(messages, **kwargs):
        requests.append((deepcopy(messages), kwargs))
        return '图例标注红色；未见数值。'
    monkeypatch.setattr(vision.llm, 'chat', chat)
    receipt, ok = ctx.execute('view_image', {'source_ref': source.ref, 'question': '有没有数字？'})
    info = json.loads(receipt)
    assert ok and info['route'] == 'auxiliary' and not ctx.pending_image_messages
    assert requests[0][1]['model'] == 'gemini-2.5-flash'
    assert requests[0][0][1]['content'][1]['type'] == 'image_url'
    _, ok = ctx.execute('save_evidence', {'cards': [{'claim': '图例有红色', 'source_ref': info['source_ref'], 'quote': '图例标注红色'}]})
    assert ok and ctx.store.cards()[0].source_kind == 'observation'
    assert ctx.store.cards()[0].observation['original_source_ref'] == source.ref


def test_force_auxiliary_and_missing_config(tmp_path, monkeypatch):
    native(monkeypatch)
    ctx = context(tmp_path, vision=vision.VisionConfig(mode='force_auxiliary', provider='fake'))
    assert vision.route(ctx) == 'auxiliary'
    ctx.config.vision.provider = ''
    with pytest.raises(ExecutionError): vision.route(ctx)


def test_image_budget_compaction_and_prefix_immutability(tmp_path):
    ctx = context(tmp_path)
    source = original(ctx)
    pixels, info = vision.render(ctx.store, source, {})
    image_message = {'role': 'user', 'content': vision.image_content(pixels, info, '图例')}
    messages = [{'role':'system','content':'stable'}, image_message]
    before = deepcopy(messages)
    estimated = context_budget.assess(ctx, messages)
    assert estimated['estimated_image_tokens'] == 15
    assert estimated['request_chars'] < len(json.dumps(messages, ensure_ascii=False))
    assert messages == before
    view = type('View', (), {'chunks': [(True, messages)]})()
    blocks = context_compaction._process_blocks(view, 1)
    assert 'base64' not in json.dumps(blocks) and info['image_ref'] in json.dumps(blocks)
    assert messages == before


def test_batch_fetch_maps_success_and_keeps_failures_separate(tmp_path, monkeypatch):
    from dra import execution_tools, file_sources
    ctx = context(tmp_path, execution=ExecutionConfig(enabled=True, network_mode='bridge'))
    ctx.execution = LocalSession(ctx.store)
    source = original(ctx)
    def acquire(ctx, url):
        if 'bad' in url: raise ExecutionError('download_http_error', '404')
        return source, png(), False
    monkeypatch.setattr(file_sources, 'acquire', acquire)
    command = "python3 -c 'import os,json; print(json.loads(os.environ[\"DRA_INPUTS\"])[0][\"source_ref\"])'"
    receipt, ok = execution_tools.execute(ctx, 'bash', {'command': command, 'fetch': ['https://example.org/chart.png','https://example.org/bad']})
    info = json.loads(receipt)
    assert ok and info['downloads'][0]['ok'] and not info['downloads'][1]['ok']
    assert ctx.store.source(info['source_ref']).computation['declared_source_refs'] == [source.ref]
    assert 'DRA_INPUTS' in ctx.store.source(info['source_ref']).computation['command']


def test_visual_card_history_retains_pixels_and_original(tmp_path, monkeypatch):
    native(monkeypatch)
    ws, parent, store, _ = history_fixture(tmp_path)
    ctx = sr.ToolContext(store, sr.SimpleConfig(vision=vision.VisionConfig()))
    source = original(ctx)
    receipt, ok = ctx.execute('view_image', {'source_ref': source.ref})
    image = json.loads(receipt)
    vision.mark_delivered(ctx, ctx.pending_image_messages)
    receipt, ok = ctx.execute('save_evidence', {'cards': [{'claim':'图为红色', 'quotes':[{'image_ref':image['image_ref'],'observation':'红色图块'}]}]})
    assert ok
    child, _ = child_context(ws, parent)
    imported = child.history.import_card(1, store.cards()[-1].card_id)
    visual = imported['quotes'][0]
    assert visual['source_kind'] == 'observation'
    local = child.store.original_source(visual['source_ref'])
    assert (child.store.sources_dir/(local.ref+'.original')).read_bytes() == png()
    assert (child.store.dir/visual['observation']['image_file']).exists()


def test_auxiliary_card_history_preserves_text_pixels_and_parent(tmp_path, monkeypatch):
    ws, parent, store, _ = history_fixture(tmp_path)
    monkeypatch.setattr(provider_registry, 'capacity_snapshot', lambda *a: {})
    monkeypatch.setattr(vision.llm, 'chat', lambda *a, **k: '图例标注红色；未见数值。')
    ctx = sr.ToolContext(store, sr.SimpleConfig(vision=vision.VisionConfig(provider='fake')))
    source = original(ctx)
    receipt, ok = ctx.execute('view_image', {'source_ref': source.ref})
    assert ok
    observation = json.loads(receipt)
    _, ok = ctx.execute('save_evidence', {'cards': [{'claim': '图例有红色',
        'source_ref': observation['source_ref'], 'quote': '图例标注红色'}]})
    assert ok
    child, _ = child_context(ws, parent)
    card = child.history.import_card(1, store.cards()[-1].card_id)
    span = card['quotes'][0]
    assert span['source_kind'] == 'observation'
    assert not child.store.cards()[-1].evidence_quotes()[0].deep_link
    imported = child.store.source(span['source_ref'])
    assert imported.observation == span['observation']
    parent_source = child.store.original_source(span['observation']['original_source_ref'])
    assert (child.store.sources_dir / (parent_source.ref + '.original')).read_bytes() == png()
    assert (child.store.dir / span['observation']['image_file']).exists()


def test_selected_image_can_be_seen_and_cited_in_library_research(tmp_path, monkeypatch):
    from dra import library as library_module
    from dra.library import LibraryStore
    from test_budget_wrapup import _turn
    library = LibraryStore(tmp_path / 'library', encoder=TinyEncoder())
    monkeypatch.setattr(library_module, 'get_library', lambda: library)
    monkeypatch.setattr(provider_registry, 'capacity_snapshot', lambda *a: {})
    monkeypatch.setattr(vision.llm, 'chat', lambda *a, **k: '图像是红色图块。')
    try:
        version = library.import_bytes('chart.png', png(), wait=True)
        ctx = context(tmp_path / 'run', source_mode='library',
            library_selection=library.pin([version['document_id']]),
            delegation_enabled=False, vision=vision.VisionConfig(provider='fake'))
        calls = []
        def model(messages, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                source = next(s for s in ctx.store.state['sources'] if s.get('document'))
                return _turn(('view_image', {'source_ref': source['ref']}))
            if len(calls) == 2:
                info = next(json.loads(m['content']) for m in messages
                    if m['role'] == 'tool' and 'image_ref' in m['content'])
                return _turn(('save_evidence', {'cards': [{'claim': '图像是红色',
                    'source_ref': info['source_ref'], 'quote': '图像是红色图块。'}]}))
            return _turn(('finish_research', {'draft': '图像是红色。[c1]',
                'coverage': [{'requirement_id': 'goal', 'status': 'answered'}], 'gaps': []}))
        result = sr.run_lead_loop(ctx.store, ctx.config, sr.requirements_from_query('读取图表'),
            context=ctx, budget=sr.Budget(ctx.config, None), model_call=model)
        assert result['stop_reason'] == 'finished' and ctx.store.cards()[0].source_kind == 'observation'
        receipt, ok = ctx.execute('read_source', {'source_ref': ctx.store.cards()[0].source_ref})
        assert ok and '图像是红色图块。' in receipt
    finally:
        library.close()


def test_native_image_reaches_research_request_before_save(tmp_path, monkeypatch):
    from test_budget_wrapup import _turn
    native(monkeypatch)
    ctx = context(tmp_path, delegation_enabled=False)
    source = original(ctx)
    seen = []
    def model(messages, **kwargs):
        seen.append(deepcopy(messages))
        if len(seen) == 1:
            return _turn(('view_image', {'source_ref':source.ref}))
        if len(seen) == 2:
            assert any(isinstance(m.get('content'), list) for m in messages)
            info = next(json.loads(m['content']) for m in messages if m['role']=='tool' and 'image_ref' in m['content'])
            return _turn(('save_evidence', {'cards':[{'claim':'红色图像','quotes':[{'image_ref':info['image_ref'],'observation':'红色区域'}]}]}))
        return _turn(('finish_research', {'draft':'图像呈红色。[c1]', 'coverage':[{'requirement_id':'goal','status':'answered'}], 'gaps':[]}))
    budget = sr.Budget(ctx.config, None)
    result = sr.run_lead_loop(ctx.store, ctx.config, sr.requirements_from_query('读取图表'), context=ctx,
                             budget=budget, model_call=model)
    assert result['stop_reason'] == 'finished' and ctx.store.cards()[0].source_kind == 'observation'
    image = next(m for m in seen[1] if isinstance(m.get('content'), list))
    assert image in seen[2]  # earlier pixel payload stays unchanged for prefix caching


def test_auxiliary_preserves_cancellation_and_reports_provider_failure(tmp_path, monkeypatch):
    import threading
    import httpx
    from openai import APIStatusError
    ctx = context(tmp_path, vision=vision.VisionConfig(provider='fake'))
    source = original(ctx)
    stop = threading.Event()
    def chat(*args, **kwargs):
        assert vision.llm.current_cancel_event() is stop
        raise APIStatusError('upstream error', response=httpx.Response(503,request=httpx.Request('POST','https://example.org')),body=None)
    monkeypatch.setattr(vision.llm,'chat',chat)
    with vision.llm.execution_scope(cancel_event=stop):
        result, ok = ctx.execute('view_image', {'source_ref':source.ref})
    assert not ok and json.loads(result)['error_code']=='vision_provider_error'
    assert len(ctx.store.state['sources']) == 1


def test_web_image_checks_saved_hash_and_serves_binary_original(tmp_path, monkeypatch):
    from dra import web
    from fastapi.testclient import TestClient
    native(monkeypatch)
    ws, parent, store, _ = history_fixture(tmp_path)
    ctx = sr.ToolContext(store, sr.SimpleConfig(vision=vision.VisionConfig()))
    source = original(ctx)
    pixels, info = vision.render(store,source,{})
    monkeypatch.setattr(web,'STORE',ws)
    with TestClient(web.app) as client:
        response = client.get(f'/api/runs/{parent}/images/{info["image_sha256"]}')
        assert response.status_code==200 and response.content==pixels
        response = client.get(f'/api/runs/{parent}/sources/{source.ref}/original')
        assert response.status_code==200 and response.content==png()
        (store.dir/info['image_file']).write_bytes(b'changed')
        assert client.get(f'/api/runs/{parent}/images/{info["image_sha256"]}').status_code==404


def test_pdf_text_reading_does_not_use_original_download_or_visual_description(tmp_path, monkeypatch):
    from dra import file_sources
    ctx = context(tmp_path)
    source = original(ctx, b'%PDF-fixture', 'report.pdf')
    ctx.store.write_source(RetrievedDoc(title='视觉观察', source_url=source.url,
        raw_content='视觉模型猜测了图表。', snippet=''), observation={'original_source_ref': source.ref})
    assert ctx.store.persisted_full_ref(source.url) is None
    monkeypatch.setattr(file_sources, 'fetch_original', lambda *a, **k: pytest.fail('PDF正文不能自动直取原件'))
    monkeypatch.setattr(sr, 'fetch_url_content', lambda *a, **k: {'url': source.url, 'content': 'PDF原文字层：备份保留43天。', 'title': '正文'})
    receipt, ok = ctx.execute('read_source', {'url': source.url})
    assert ok and 'PDF原文字层' in receipt and '猜测' not in receipt
    text_ref = ctx.store.persisted_full_ref(source.url)
    assert text_ref and not ctx.store.source(text_ref).observation
    _, ok = ctx.execute('save_evidence', {'cards': [{'claim': '备份保留43天',
        'source_ref': text_ref, 'quote': '备份保留43天'}]})
    assert ok and ctx.store.cards()[0].source_kind == 'original'


def test_failed_view_of_web_page_does_not_download_or_replace_text(tmp_path, monkeypatch):
    from dra import file_sources
    ctx = context(tmp_path)
    source, _ = ctx.store.write_source(RetrievedDoc(title='网页', source_url='https://example.org/page',
        raw_content='可引用的网页正文', snippet=''))
    monkeypatch.setattr(file_sources, 'fetch_original', lambda *a, **k: pytest.fail('看图不能自动下载HTML'))
    receipt, ok = ctx.execute('view_image', {'source_ref': source.ref})
    assert not ok and json.loads(receipt)['error_code'] == 'original_required'
    receipt, ok = ctx.execute('read_source', {'source_ref': source.ref})
    assert ok and '可引用的网页正文' in receipt
    assert len(ctx.store.state['sources']) == 1


def test_image_usage_does_not_corrupt_text_anchor(tmp_path):
    ctx = context(tmp_path)
    source = original(ctx)
    pixels, info = vision.render(ctx.store, source, {})
    messages = [{'role': 'user', 'content': vision.image_content(pixels, info, '图例')}]
    assert sr._request_chars(messages) == context_budget.request_chars(messages)
    assert sr._request_chars(messages) < len(json.dumps(messages))
    ctx.note_usage_anchor(120, 360)
    anchor = ctx.usage_anchor
    chars = context_budget.request_chars(messages)
    actual = chars / 3 + 5
    ctx.note_usage_anchor(actual, chars, image_parts=vision.image_token_parts(messages))
    assert ctx.usage_anchor == anchor
    assert ctx.estimate_request_tokens(900)[0] == 300
    budget = context_budget.assess(ctx, messages)
    assert budget['estimated_input_tokens'] == pytest.approx(actual)
    assert budget['image_estimation_basis'] == 'aggregate_usage_fixed_images'


def test_crop_usage_error_does_not_multiply_new_full_page_estimate(tmp_path):
    ctx = context(tmp_path)
    ctx.note_usage_anchor(120, 360)
    ctx.token_calibration['research']['error_per_char'] = 0.1
    small, info = vision.render(ctx.store, original(ctx), {})
    previous = [{'role': 'user', 'content': vision.image_content(small, info, '裁剪图')}]
    chars = context_budget.request_chars(previous)
    actual = chars / 3 + chars * 0.1 + 500
    ctx.note_usage_anchor(actual, chars, image_parts=vision.image_token_parts(previous))
    full = io.BytesIO()
    Image.new('RGB', (800, 800), 'white').save(full, 'PNG')
    next_message = {'role': 'user', 'content': vision.image_content(full.getvalue(), info, '整页')}
    estimate, basis = context_budget.estimated_image_tokens(ctx, previous + [next_message])
    assert estimate == pytest.approx(500 + 2500)
    assert basis == 'aggregate_usage_fixed_images'
    total = context_budget.assess(ctx, previous + [next_message])
    assert total['checked_input_tokens'] == pytest.approx(total['request_chars'] / 3 * 1.3 + 3000)
    # Compaction can remove the old crop; its calibration then does not apply.
    assert context_budget.estimated_image_tokens(ctx, [next_message]) == (2500, 'pixel_estimated')


@pytest.mark.parametrize('name', ['table.xlsx', 'paper.docx', 'slides.pptx', 'files.zip'])
def test_office_link_without_execution_keeps_text_extraction(tmp_path, monkeypatch, name):
    from dra import file_sources
    ctx = context(tmp_path, execution=ExecutionConfig(enabled=True))
    ctx.execution_capability = {'available': False, 'reason': 'Docker未运行'}
    url = 'https://example.org/' + name
    monkeypatch.setattr(file_sources, 'fetch_original', lambda *a, **k: pytest.fail('不可用执行工具不应抢走文字提取'))
    monkeypatch.setattr(sr, 'fetch_url_content', lambda *a, **k: {'url': url, 'content': '提取到的表格正文', 'title': name})
    receipt, ok = ctx.execute('read_source', {'url': url})
    assert ok and '提取到的表格正文' in receipt


def test_child_file_handoff_can_use_shared_execution(tmp_path, monkeypatch):
    ctx = context(tmp_path, execution=ExecutionConfig(enabled=True), vision=vision.VisionConfig(enabled=False))
    ctx.execution_capability = {'available': True}
    child = ctx.fork_for_subagent(ctx.config, budget=sr.Budget(ctx.config, None), delegation_id='child')
    fake_http(monkeypatch, [Response(data=b'PK\x03\x04office', headers={'Content-Type': 'application/zip'})])
    receipt, ok = child.execute('read_source', {'url': 'https://example.org/table.xlsx'})
    payload = json.loads(receipt)
    assert ok and payload['available_tools'] == ['bash']
    assert child.execution is None
    ctx.execution = LocalSession(ctx.store)
    result, ok = child.execute('bash', {'command': 'true'})
    assert ok and child.execution is ctx.execution


def test_execution_only_image_receipt_does_not_offer_unavailable_vision(tmp_path, monkeypatch):
    ctx = context(tmp_path, execution=ExecutionConfig(enabled=True), vision=vision.VisionConfig(enabled=False))
    ctx.execution = LocalSession(ctx.store)
    fake_http(monkeypatch, [Response(data=png(), headers={'Content-Type': 'image/png'})])
    receipt, ok = ctx.execute('read_source', {'url': 'https://example.org/chart.png'})
    payload = json.loads(receipt)
    assert ok and payload['available_tools'] == ['bash']
    assert 'view_image' not in payload['note']


def test_original_html_does_not_override_default_clean_web_reading(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    url = 'https://example.org/page.html'
    fake_http(monkeypatch, [Response(data=b'<html><script>noise()</script><p>Fact</p></html>', headers={'Content-Type': 'text/html'})])
    receipt, ok = ctx.execute('read_source', {'url': url, 'original': True})
    assert ok and '<script>' in receipt
    original_ref = json.loads(receipt)['source_ref']
    assert ctx.store.persisted_full_ref(url) is None
    monkeypatch.setattr(sr, 'fetch_url_content', lambda *a, **k: {'url': url, 'content': '干净的正文Fact', 'title': '网页'})
    receipt, ok = ctx.execute('read_source', {'url': url})
    assert ok and '干净的正文Fact' in receipt and '<script>' not in receipt
    receipt, ok = ctx.execute('read_source', {'source_ref': original_ref})
    assert ok and '<script>' in receipt


def test_legacy_schema_stays_text_only_until_features_enabled():
    plain = vision.tool_schemas(sr.LEAD_TOOL_SCHEMAS, enabled=False, originals=False)
    assert plain == sr.LEAD_TOOL_SCHEMAS
    params = {s['function']['name']: s['function']['parameters'] for s in plain}
    assert 'original' not in params['read_source']['properties']
    item = params['save_evidence']['properties']['cards']['items']['properties']['quotes']['items']
    assert item['anyOf'] == [{'required': ['quote']}, {'required': ['candidate_id']}]
    assert 'image_ref' not in item['properties']
    updated = vision.tool_schemas(plain, enabled=True, originals=True)
    params = {s['function']['name']: s['function']['parameters'] for s in updated}
    assert 'original' in params['read_source']['properties']
    item = params['save_evidence']['properties']['cards']['items']['properties']['quotes']['items']
    assert {'required': ['image_ref', 'observation']} in item['anyOf']
    assert plain == sr.LEAD_TOOL_SCHEMAS


def test_bad_fetch_url_is_reported_without_stopping_other_inputs(tmp_path, monkeypatch):
    from dra import execution_tools
    ctx = context(tmp_path, execution=ExecutionConfig(enabled=True, network_mode='bridge'))
    ctx.execution = LocalSession(ctx.store)
    fake_http(monkeypatch, [Response(data=png(), headers={'Content-Type': 'image/png'})])
    receipt, ok = execution_tools.execute(ctx, 'bash', {'command': 'printf worked',
        'fetch': ['https://127.0.0.1/private', 'https://example.org/chart.png']})
    info = json.loads(receipt)
    assert ok and info['downloads'][0]['error_code'] == 'url_denied'
    assert info['downloads'][1]['ok'] and 'worked' in info['stdout']
    assert ctx.store.source(info['source_ref']).computation['declared_source_refs'] == [info['downloads'][1]['source_ref']]
    _, ok = execution_tools.execute(ctx, 'bash', {'command': 'printf bad', 'fetch': 'https://example.org/chart.png'})
    assert not ok


def test_pdf_index_failure_and_original_retry_are_distinct(tmp_path, monkeypatch):
    from dra.library import LibraryStore, LibraryError
    library = LibraryStore(tmp_path / 'library', encoder=TinyEncoder())
    def parse(directory, version):
        (directory / 'parsed.json').write_text(json.dumps({'text': '备份保留43天', 'pages': [],
            'warnings': [], 'parser': 'pypdf-text-v1'}))
    monkeypatch.setattr(library, '_parse_document', parse)
    index = library._index_document
    monkeypatch.setattr(library, '_index_document', lambda *a: (_ for _ in ()).throw(RuntimeError('encoder offline')))
    try:
        failed = library.import_bytes('electronic.pdf', b'%PDF-fixture', wait=True)
        assert failed['status'] == 'failed'
        monkeypatch.setattr(library, '_index_document', index)
        ready = library.import_bytes('electronic.pdf', b'%PDF-fixture', replace=failed['document_id'], wait=True)
        assert ready['status'] == 'ready'
        monkeypatch.setattr(library, '_parse_document', lambda *a: (_ for _ in ()).throw(LibraryError('temporarily unparseable')))
        saved = library.import_bytes('scan.pdf', b'%PDF-second-fixture', wait=True)
        assert saved['status'] == 'original'
        monkeypatch.setattr(library, '_parse_document', parse)
        retried = library.import_bytes('scan.pdf', b'%PDF-second-fixture', replace=saved['document_id'], wait=True)
        assert retried['status'] == 'ready' and retried['version_id'] != saved['version_id']
    finally:
        library.close()


def test_auxiliary_stream_cancellation_does_not_publish_partial_observation(tmp_path, monkeypatch):
    import threading
    from types import SimpleNamespace as N
    ctx = context(tmp_path, vision=vision.VisionConfig(provider='openai'))
    source = original(ctx)
    stop = threading.Event()
    closed = []
    class Stream:
        def __iter__(self):
            stop.set()
            yield N(choices=[N(finish_reason=None, delta=N(content='未完成观察'))], usage=None)
        def close(self):
            closed.append(True)
    def create(**kwargs):
        assert kwargs['stream'] is True
        return Stream()
    monkeypatch.setattr(provider_registry, 'capacity_snapshot', lambda *a: {})
    monkeypatch.setattr(vision.llm, '_get_client', lambda *a: N(chat=N(completions=N(create=create))))
    monkeypatch.setattr(vision.llm, '_provider_headers', lambda *a: {})
    with vision.llm.execution_scope(cancel_event=stop):
        receipt, ok = ctx.execute('view_image', {'source_ref': source.ref})
    assert not ok and len(ctx.store.state['sources']) == 1 and closed
