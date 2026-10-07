import json
from dra import simple_research as sr
from dra.models import RetrievedDoc


def make(tmp_path, texts):
    store = sr.RunStore(tmp_path, run_id='quote-test', query='quote')
    for text in texts:
        store.write_source(RetrievedDoc(title='测试', snippet='', raw_content=text, source_url='https://example.com/'+str(len(store.state['sources']))))
    return sr.ToolContext(store, sr.SimpleConfig())


def save(ctx, quote):
    body, ok = ctx.save_evidence({'cards': [{'claim': '测试', 'quote': quote}]})
    return json.loads(body), ok


def test_unique_and_archive(tmp_path):
    ctx = make(tmp_path, ['开头\n精度\r\n  0.02mm\n结尾'])
    ctx.read_source({'source_ref': 's1'})
    assert save(ctx, '精度 0.02mm')[1]
    card = ctx.store.cards()[0]
    assert card.quote == '精度\r\n  0.02mm'
    a,b = sr._chars_of(card.location)
    assert ctx.store.read_source_text('s1')[a:b] == card.quote
    assert card.deep_link
    assert 's1' in ctx.archived
    assert save(ctx, '结尾')[1]


def test_unread_then_child_read_does_not_grant_parent(tmp_path):
    ctx = make(tmp_path, ['B产品用时2秒'])
    child = ctx.fork_for_subagent(ctx.config, budget=None, delegation_id='sub1')
    assert not save(ctx, 'B产品')[1]
    child.read_source({'source_ref': 's1'})
    assert save(child, 'B产品')[1]
    assert not save(ctx, 'B产品')[1]
    assert not ctx.save_evidence({'cards': [{'claim': '旧接口', 'source_ref': 's1', 'excerpt_no': 1}]})[1]
    ctx.read_source({'source_ref': 's1'})
    assert save(ctx, 'B产品')[1]


def test_duplicate_candidates_then_expanded_quote(tmp_path):
    ctx = make(tmp_path, ['A产品。用时2秒。', 'B产品。用时2秒。'])
    for ref in ['s1','s2']:
        ctx.read_source({'source_ref': ref})
    body, ok = save(ctx, '用时2秒。')
    assert ok and body['pending_selection'] == 1 and not ctx.store.cards()
    assert body['results'][0]['binding_status'] == 'ambiguous'
    assert len(body['results'][0]['candidates']) == 2
    assert save(ctx, 'B产品。用时2秒。')[1]
    assert ctx.store.cards()[0].source_ref == 's2'


def test_cross_read_windows_but_not_unread_gap(tmp_path):
    ctx = make(tmp_path, ['abcdef'])
    ctx.read_source({'source_ref': 's1', 'range': '0-2'})
    ctx.read_source({'source_ref': 's1', 'range': '3-6'})
    assert not save(ctx, 'abcdef')[1]
    ctx.read_source({'source_ref': 's1', 'range': '2-3'})
    assert save(ctx, 'abcdef')[1]


def test_invalid_quote_does_not_fall_back_to_pointer(tmp_path):
    ctx = make(tmp_path, ['原文'])
    ctx.read_source({'source_ref': 's1'})
    assert not ctx.save_evidence({'cards': [{'claim': '测试', 'quote': '', 'source_ref': 's1', 'excerpt_no': 1}]})[1]


def test_condense_summary_does_not_grant_unshown_source(tmp_path):
    ctx = make(tmp_path, ['已展示原文。' + '填充内容' * 200 + '隐藏参数0.05mm'])
    ctx.config.condense_over_chars = 10
    ctx.condense_fn = lambda *args, **kwargs: {'summary': '隐藏参数0.05mm', 'excerpts': ['已展示原文。']}
    result, ok = ctx.read_source({'source_ref': 's1'})
    assert ok
    body = json.loads(result)
    assert body['view'] == 'condensed'
    assert not save(ctx, '隐藏参数0.05mm')[1]
    assert save(ctx, '已展示原文。')[1]


def test_candidate_context_cannot_leak_unread_text(tmp_path):
    ctx = make(tmp_path, ['未读机密用时2秒。', '另一机密用时2秒。'])
    for ref in ['s1', 's2']:
        ctx.read_source({'source_ref': ref, 'range': '4-9'})
    body, ok = save(ctx, '用时2秒。')
    assert ok and body['pending_selection'] == 1 and not ctx.store.cards()
    assert '机密' not in json.dumps(body, ensure_ascii=False)


def test_parent_read_does_not_grant_child_or_inject_source_text(tmp_path):
    ctx = make(tmp_path, ['父任务秘密原文'])
    result, ok = ctx.read_source({'source_ref': 's1'})
    ctx.receipts.append({'tool': 'read_source', 'ok': ok, 'payload': result})
    child = ctx.fork_for_subagent(ctx.config, budget=None, delegation_id='sub1')
    assert not save(child, '父任务秘密原文')[1]
    messages = sr._build_messages([], child, [], [], seconds_left=None)
    assert '父任务秘密原文' not in json.dumps(messages, ensure_ascii=False)


def test_candidates_are_bounded_and_count_is_honest(tmp_path):
    ctx = make(tmp_path, ['用时2秒。' * 25])
    ctx.read_source({'source_ref': 's1'})
    body, ok = save(ctx, '用时2秒。')
    assert ok and body['pending_selection'] == 1 and not ctx.store.cards()
    row = body['results'][0]
    assert len(row['candidates']) == 10
    assert row['candidates_total'] == 25


def test_unresolved_rejection_preserves_batch_read_context(tmp_path):
    ctx = make(tmp_path, ['唯一原文'])
    ctx.read_source({'source_ref': 's1'})
    body, ok = ctx.save_evidence({'cards': [{'claim': '成功', 'quote': '唯一原文'},
                                          {'claim': '', 'quote': '唯一原文'}]})
    assert ok
    assert 's1' not in ctx.archived
