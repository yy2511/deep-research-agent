"""Other benchmark entrypoints must not inherit an enabled Web evidence judge."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from dra import runtime_config
from dra.browser import BrowserConfig


@pytest.mark.parametrize('benchmark', ['bcp', 'drb2'])
def test_evaluation_defaults_disable_jev_without_changing_web(monkeypatch, tmp_path, benchmark):
    settings = runtime_config.RuntimeModelSettings(planner={'provider': 'fixture', 'model': 'fixture'},
        subagent={'provider': 'fixture', 'model': 'fixture'}, evidence_judge={'enabled': True})
    monkeypatch.setattr(runtime_config, 'current', lambda: settings)
    root = Path(__file__).resolve().parents[1]
    file = root / 'eval' / benchmark / ('run_bcp.py' if benchmark == 'bcp' else 'run_pilot.py')
    spec = importlib.util.spec_from_file_location('judge_mode_' + benchmark, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if benchmark == 'bcp':
        config = module.eval_config(model='fixture', provider='fixture')
        assert not config.evidence_judge.enabled
        assert not config.browser.enabled
        class Captured(Exception):pass
        def capture(*args, config, **kwargs):
            assert not config.browser.enabled
            raise Captured
        monkeypatch.setattr(module.research, 'run_minimal_research', capture)
        with pytest.raises(Captured):
            module.run_one({'query_id': 'fixture', 'query': 'fixture'}, tmp_path,
                corpus=SimpleNamespace(search=lambda *a, **kw: pytest.fail('offline')),
                config=config.model_copy(update={'browser': BrowserConfig(enabled=True)}))
    else:
        class Captured(Exception):
            pass
        def capture(*args, config, **kwargs):
            assert not config.evidence_judge.enabled
            assert not config.browser.enabled
            raise Captured
        monkeypatch.setattr(module.research, 'run_minimal_research', capture)
        with pytest.raises(Captured):
            module.run_one({'idx': 1, 'content': {'task': 'fixture', 'blocked': {}}}, tmp_path)
    assert settings.evidence_judge.enabled
    assert settings.browser.enabled


@pytest.mark.parametrize('benchmark', ['bcp', 'drb2', 'deepsearchqa'])
def test_evaluation_entrypoints_disable_compaction_even_when_web_enables_it(monkeypatch, tmp_path, benchmark):
    """网页开启长对话整理时，评测入口仍固定关闭，不改网页设置。"""
    settings = runtime_config.RuntimeModelSettings(planner={'provider': 'fixture', 'model': 'fixture'},
        subagent={'provider': 'fixture', 'model': 'fixture'}, compaction_enabled=True,
        context_window_mode='manual', context_window_tokens=128000)
    monkeypatch.setattr(runtime_config, 'current', lambda: settings)
    root = Path(__file__).resolve().parents[1]
    file = {'bcp': 'eval/bcp/run_bcp.py', 'drb2': 'eval/drb2/run_pilot.py',
            'deepsearchqa': 'eval/deepsearchqa/run_pilot.py'}[benchmark]
    monkeypatch.syspath_prepend(str((root / file).parent))
    spec = importlib.util.spec_from_file_location('compaction_mode_' + benchmark, root / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if benchmark == 'bcp':
        assert not module.eval_config(model='fixture', provider='fixture').compaction_enabled
        assert module.eval_config(model='fixture', provider='fixture', compaction_enabled=True).compaction_enabled
    elif benchmark == 'deepsearchqa':
        assert not module.research_config(settings, 600).compaction_enabled
    else:
        class Captured(Exception):
            pass
        def capture(*args, config, **kwargs):
            assert not config.compaction_enabled
            raise Captured
        monkeypatch.setattr(module.research, 'run_minimal_research', capture)
        with pytest.raises(Captured):
            module.run_one({'idx': 1, 'content': {'task': 'fixture', 'blocked': {}}}, tmp_path)
    assert settings.compaction_enabled


def _load_eval(name, rel, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str((root / rel).parent))
    spec = importlib.util.spec_from_file_location(name, root / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_drb2_offers_skills_and_guards_original_downloads(monkeypatch, tmp_path):
    from dra import file_sources
    settings = runtime_config.RuntimeModelSettings(planner={'provider': 'f', 'model': 'f'}, subagent={'provider': 'f', 'model': 'f'})
    monkeypatch.setattr(runtime_config, 'current', lambda: settings)
    module = _load_eval('drb2_skills', 'eval/drb2/run_pilot.py', monkeypatch)
    monkeypatch.setattr(file_sources, 'fetch_original', lambda url, **kw: (
        b'x', 'The Original Article Title reprinted' if 'copy' in url else 'other text', {'filename': 'f.pdf', 'final_url': url}))
    class Captured(Exception):
        pass
    def capture(*args, config, **kwargs):
        assert config.skill_bundle['skills'] and config.skill_bundle['selected'] == []
        assert file_sources.fetch_original('https://example.org/data.pdf')[1] == 'other text'
        with pytest.raises(module.ExecutionError):
            file_sources.fetch_original('https://example.org/copy.pdf')
        raise Captured
    monkeypatch.setattr(module.research, 'run_minimal_research', capture)
    with pytest.raises(Captured):
        module.run_one({'idx': 1, 'content': {'task': 'fixture', 'blocked': {'title': 'The Original Article Title'}}}, tmp_path)


def test_bcp_offers_skills_but_keeps_fixed_corpus_download_block(monkeypatch):
    settings = runtime_config.RuntimeModelSettings(planner={'provider': 'f', 'model': 'f'}, subagent={'provider': 'f', 'model': 'f'})
    monkeypatch.setattr(runtime_config, 'current', lambda: settings)
    module = _load_eval('bcp_skills', 'eval/bcp/run_bcp.py', monkeypatch)
    config = module.eval_config(model='f', provider='f')
    assert config.skill_bundle['skills'] and config.skill_bundle['selected'] == []
    with pytest.raises(module.ExecutionError, match='固定文档库'):
        module.FetchBlock().original('https://example.org/file.pdf')


def test_official_image_capability_does_not_depend_on_env_already_loaded(monkeypatch, tmp_path):
    """脚本入口未预先加载 .env 时，官方 DeepSeek 端点的看图能力也要与网页一致地识别。"""
    import os
    from dra import llm, provider_registry
    name = llm._PROVIDERS['deepseek'][1]
    env = tmp_path / '.env'
    env.write_text(f'{name}=https://api.deepseek.com\n')
    monkeypatch.setattr(llm, '_ENV_PATH', env)
    for alias in llm._base_url_env_names('deepseek'):
        monkeypatch.delenv(alias, raising=False)
    monkeypatch.setattr(provider_registry, 'custom_connections', lambda: {})
    snapshot = provider_registry.capacity_snapshot('deepseek', 'deepseek-flash')
    assert snapshot['image_input'] is True and snapshot['sources']['image_input'] == ['official']


def test_drb2_matches_deepsearchqa_web_aligned_research_and_plans_first(monkeypatch, tmp_path):
    settings = runtime_config.RuntimeModelSettings(planner={'provider': 'f', 'model': 'f'}, subagent={'provider': 'f', 'model': 'f'})
    monkeypatch.setattr(runtime_config, 'current', lambda: settings)
    module = _load_eval('drb2_aligned', 'eval/drb2/run_pilot.py', monkeypatch)
    expected = module.DSQA.research_config(settings, 2400).model_dump(mode='json')
    seen = {}
    monkeypatch.setattr(module.DSQA, 'prepare_plan', lambda problem, brief: (
        {'requirements': ['r'], 'brief_context': 'ctx', 'initial_route': []}, {'brief': problem}))

    class Captured(Exception):
        pass

    def capture(query, *, config, **kwargs):
        seen.update(config=config.model_dump(mode='json'), kwargs=kwargs)
        raise Captured
    monkeypatch.setattr(module.research, 'run_minimal_research', capture)
    task = {'idx': 1, 'content': {'task': 'fixture', 'blocked': {'urls': ['https://example.org/paper'], 'title': 'T'}}}
    with pytest.raises(Captured):
        module.run_one(task, tmp_path, settings=settings, seconds=2400, brief_config={'planner_model': 'f'})
    got = seen['config']
    assert got['blocked_sites'] == ['example.org/paper']
    assert {k: v for k, v in got.items() if k != 'blocked_sites'} == {k: v for k, v in expected.items() if k != 'blocked_sites'}
    assert got['wall_timeout_s'] == 2400 and got['execution'] == expected['execution']
    assert seen['kwargs']['brief_context'] == 'ctx' and (tmp_path / 'plan.json').exists()
