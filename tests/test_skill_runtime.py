import json
from dra import skills, simple_research as sr
from test_skills import TEXT
from test_simple_research import _turn


def bundle(tmp_path, selected=()):
    repo = skills.SkillRepository(tmp_path / 'data', builtin_dir=tmp_path / 'none')
    repo.save('compare', TEXT, {'references/check.md': 'Check units.'})
    repo.set_enabled(['compare'])
    return repo.prepare(list(selected))


def context(tmp_path, selected=()):
    cfg = sr.SimpleConfig(skill_bundle=bundle(tmp_path, selected), max_llm_calls=8, max_tool_calls=8)
    return sr.ToolContext(sr.RunStore(tmp_path / 'runs', run_id='skills', query='Compare'), cfg)


def test_activation_independent_per_agent_and_snapshot(tmp_path):
    ctx = context(tmp_path)
    payload, ok = ctx.execute('load_skill', {'name': 'compare'})
    assert ok and json.loads(payload)['changed']
    assert not json.loads(ctx.execute('load_skill', {'name': 'compare'})[0])['changed']
    assert ctx.execute('read_skill_reference', {'name': 'compare', 'path': 'references/check.md'})[1]
    assert not ctx.execute('read_skill_reference', {'name': 'compare', 'path': '../SKILL.md'})[1]
    child = ctx.fork_for_subagent(ctx.config, budget=sr.Budget(ctx.config, None), delegation_id='sub1')
    assert child.skills.loaded == {}
    assert child.skills.bundle == ctx.skills.bundle
    assert child.execute('load_skill', {'name': 'compare'})[1]
    assert {e['agent_id'] for e in ctx.store.state['skills']['loaded']} == {'lead', 'sub1'}
    assert (ctx.store.dir / 'skills.json').exists()
    assert not ctx.store.state['sources']


def test_loop_discloses_name_and_preloads_selected(tmp_path):
    ctx = context(tmp_path, ['compare'])
    seen = []
    def model(messages, **kwargs):
        seen.append(messages)
        assert 'Compare costs' in json.dumps(messages)
        assert 'load_skill' in json.dumps(kwargs['tools'])
        return _turn(('finish_research', {'draft': '没有取得外部证据，本轮仅说明方法。', 'coverage': []}))
    sr.run_lead_loop(ctx.store, ctx.config, [], budget=sr.Budget(ctx.config, None), context=ctx, model_call=model)
    assert seen
    assert ctx.skills.loaded['compare']


def test_wrapup_restores_method_and_reference(tmp_path):
    ctx = context(tmp_path)
    ctx.execute('load_skill', {'name': 'compare'})
    ctx.execute('read_skill_reference', {'name': 'compare', 'path': 'references/check.md'})
    def model(messages, **kwargs):
        assert 'Compare costs' in json.dumps(messages)
        assert 'Check units.' in json.dumps(messages)
        return _turn(('finish_research', {'draft': '尚未取得可引用材料。', 'coverage': []}))
    sr._wrapup_turn(ctx.store, ctx.config, [], ctx, model_call=model, budget=sr.Budget(ctx.config, None))


def test_compaction_preserves_loaded_content(tmp_path):
    from test_context_compaction import setup_context, summary
    from dra.context_compaction import compact_context
    ctx, h, base, messages, budget = setup_context(tmp_path)
    ctx.skills = skills.SkillSession(bundle(tmp_path))
    ctx.execute('load_skill', {'name': 'compare'})
    result = compact_context(ctx, messages, [], budget, call_model=summary)
    assert 'Compare costs' in json.dumps(result)
    assert 'Compare costs' in json.dumps(sr._build_messages(base, ctx, [], h, seconds_left=None))


def test_repeated_activation_is_receipted_without_reloading(tmp_path):
    """重复加载同一技能只回执“未变化”，不重复加载；结束由模型交稿决定。"""
    ctx = context(tmp_path)
    ctx.config.max_llm_calls = None
    ctx.config.max_tool_calls = None
    seen = []
    def model(messages, **kwargs):
        seen.append(messages)
        if len(seen) <= 5:
            return _turn(('load_skill', {'name':'compare'}))
        return _turn(('finish_research', {'draft': '按技能完成比较。', 'gaps': []}))
    result = sr.run_lead_loop(ctx.store, ctx.config, [], budget=sr.Budget(ctx.config, None), context=ctx, model_call=model)
    assert result['stop_reason'] == 'finished' and len(seen) == 6
    assert len(ctx.store.state['skills']['loaded']) == 1
    receipts = [json.loads(r['payload']) for r in ctx.receipts if r['tool'] == 'load_skill']
    assert len(receipts) == 5 and [r.get('changed') for r in receipts[1:]] == [False] * 4


def test_automatic_loading_discloses_only_catalog_until_requested(tmp_path):
    ctx = context(tmp_path)
    count = 0
    def model(messages, **kwargs):
        nonlocal count
        count += 1
        text = json.dumps(messages)
        if count == 1:
            assert 'Compare costs' not in text
            assert 'Compare options' in text and 'compare' in text
            return _turn(('load_skill', {'name':'compare'}))
        assert 'Compare costs' in text
        if count == 2:
            assert 'Check units.' not in text
            return _turn(('read_skill_reference', {'name':'compare','path':'references/check.md'}))
        assert 'Check units.' in text
        return _turn(('finish_research', {'draft':'方法已读取，尚未取得外部事实证据。','coverage':[]}))
    sr.run_lead_loop(ctx.store, ctx.config, [], budget=sr.Budget(ctx.config, None), context=ctx, model_call=model)
    assert count == 3
    assert len(ctx.store.state['skills']['loaded']) == 2
