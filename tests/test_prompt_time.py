"""捕获实际模型边界消息，不发远程请求。"""
import json
from datetime import datetime

import pytest
from dra import simple_research as sr, research_brief as rb
from dra.runtime_config import BriefConfig


class Captured(BaseException):
    pass


@pytest.mark.parametrize('stage', ['lead', 'subagent', 'wrapup'])
def test_run_time_reaches_all_research_roles(tmp_path, monkeypatch, stage):
    store = sr.RunStore(tmp_path, run_id='r', query='截至 2020 年的资料')
    saved = json.loads((tmp_path / 'run.json').read_text())
    anchor = saved.get('research_time')
    assert anchor, '运行必须落盘时间基准'
    assert datetime.fromisoformat(anchor).utcoffset() is not None
    # 即使后来跨日，循环和收尾也不能重新取当前时间而漂移。
    monkeypatch.setattr(sr, 'current_research_time', lambda: '2099-01-01T00:00:00+00:00')
    config = sr.SimpleConfig()
    budget = sr.Budget(config, None)
    context = sr.ToolContext(store, config, budget=budget)
    def capture(messages, **kwargs):
        content = messages[0]['content']
        assert anchor in content
        assert '用户明确指定' in content
        assert '不是' in content
        raise Captured
    with pytest.raises(Captured):
        if stage == 'wrapup':
            sr._wrapup_turn(store, config, [], context, model_call=capture, budget=budget)
        else:
            sr.run_lead_loop(store, config, [], context=context, budget=budget,
                             model_call=capture, role=stage,
                             system_prompt=sr._SUBAGENT_SYSTEM if stage == 'subagent' else None)
    assert store.state['research_time'] == anchor


@pytest.mark.parametrize('function', [rb.assess_research_gaps, rb.build_research_brief])
def test_brief_has_time_before_model_call(function):
    def capture(messages, **kwargs):
        assert '【时间基准】' in messages[0]['content']
        assert '用户明确指定' in messages[0]['content']
        raise Captured
    with pytest.raises(Captured):
        function('今年有哪些变化？', BriefConfig(), model_call=capture)
