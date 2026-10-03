"""研究循环的温度：配置了才传给模型调用；不配置时不传，沿用 llm 默认。"""
from dra import simple_research as sr
from dra.subagent import SubAgentConfig
from test_budget_wrapup import _turn


class Recorder:
    def __init__(self):
        self.kwargs = []

    def __call__(self, messages, **kwargs):
        self.kwargs.append(kwargs)
        return _turn(("finish_research", {"draft": "无。", "coverage": [], "gaps": []}))


def _temps(tmp_path, adapter):
    model = Recorder()
    sr.run_simple_research("q", sr.requirements_from_query("q"), config=sr.SimpleConfig(adapter=adapter),
                           run_dir=tmp_path, model_call=model, search_fn=lambda _q: [])
    return [k.get("temperature", "未传") for k in model.kwargs]


def test_temperature_is_passed_only_when_configured(tmp_path):
    assert set(_temps(tmp_path / "a", SubAgentConfig())) == {"未传"}
    assert set(_temps(tmp_path / "b", SubAgentConfig(temperature=1.0))) == {1.0}
