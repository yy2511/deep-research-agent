"""Other benchmark entrypoints must not inherit an enabled Web evidence judge."""
import importlib.util
from pathlib import Path

import pytest

from dra import runtime_config


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
    else:
        class Captured(Exception):
            pass
        def capture(*args, config, **kwargs):
            assert not config.evidence_judge.enabled
            raise Captured
        monkeypatch.setattr(module.research, 'run_minimal_research', capture)
        with pytest.raises(Captured):
            module.run_one({'idx': 1, 'content': {'task': 'fixture', 'blocked': {}}}, tmp_path)
    assert settings.evidence_judge.enabled
