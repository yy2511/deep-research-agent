"""真实运行中遇到过的边界情况：用量未知的失败请求、来源目录不重复推送原文、回读不能占用存证的保留额度。

只验证确定的规则，不评价研究策略好坏。"""
import json

from dra import simple_research as sr
from test_budget_wrapup import Script, _doc, _turn


def test_failed_attempt_without_usage_is_explicitly_unknown(tmp_path):
    store = sr.open_run(tmp_path / 'run', query='q', run_id='r')
    budget = sr.Budget(sr.SimpleConfig(), deadline=None)
    record = budget.take('lead', 'turn-1', store)
    budget.settle(record, store, status='failed', error='LLMRequestTimeout')
    saved = store.state['calls'][0]
    assert saved['usage_unknown'] is True
    assert saved['input_tokens'] is None and saved['output_tokens'] is None
def test_source_catalog_does_not_inject_read_previews_or_unread_text(tmp_path):
    cfg = sr.SimpleConfig()
    store = sr.open_run(tmp_path / 'run', query='q', run_id='r')
    context = sr.ToolContext(store, cfg, search_fn=lambda _: [_doc(raw='READABLE ' * 150 + 'HIDDEN_TAIL ' * 700)])
    context.search_sources({'query':'q'})
    result, ok = context.read_source({'source_ref':'s1', 'range':'0-1000'})
    assert ok
    context.receipts.append({'tool':'read_source','ok':True,'payload':result})
    messages = sr._build_messages([],context,[],[],seconds_left=20)
    content = json.dumps(messages, ensure_ascii=False)
    assert '【可引用来源】' in content and 's2' in content
    assert 'READABLE' not in content and 'HIDDEN_TAIL' not in content
    assert '【最近已读、可直接存证的片段】' not in content


def test_inspection_cannot_consume_reserved_saving_slots(tmp_path):
    script = Script([
        _turn(('search_sources',{'query':'q'})),
        _turn(('read_source',{'source_ref':'s1'})),
        _turn(('inspect_research',{'view':'sources'})),
        _turn(('save_evidence',{'cards':[{'claim':'Both local files work.','source_ref':'s2','excerpt_no':1}]})),
        _turn(('finish_research',{'draft':'Both local files work [c1].'})),
    ])
    result = sr.run_simple_research('q',run_dir=tmp_path/'run',
        config=sr.SimpleConfig(max_llm_calls=8,max_tool_calls=4),
        model_call=script,search_fn=lambda _: [_doc()])
    ledger = json.loads((tmp_path/'run/run.json').read_text())
    inspection = next(a['payload'] for a in ledger['actions']
        if a.get('action')=='tool_call' and a['payload'].get('tool')=='inspect_research')
    assert inspection['ok'] is False and '保留区' in inspection['reason']
    assert len(result.cards) == 1
