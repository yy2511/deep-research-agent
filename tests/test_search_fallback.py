"""三级检索兜底：Tavily+Exa 双源全挂 → DDG 保底；DDG 也挂才算真失败。"""
import pytest

import dra.subagent as subagent_mod
from dra.models import RetrievedDoc
from dra.subagent import SubAgentConfig, _retrieve


def _boom(*a, **k):
    raise RuntimeError("SSLEOFError")


def test_fallback_to_ddg(monkeypatch):
    monkeypatch.setattr(subagent_mod, "web_search", _boom)
    monkeypatch.setattr(subagent_mod, "exa_search", _boom)
    monkeypatch.setattr(subagent_mod, "ddg_search",
                        lambda q, top_k=5: [RetrievedDoc(title="d", snippet="s")])
    docs = _retrieve("q", SubAgentConfig())
    assert len(docs) == 1 and docs[0].title == "d"


def test_all_three_fail_raises(monkeypatch):
    monkeypatch.setattr(subagent_mod, "web_search", _boom)
    monkeypatch.setattr(subagent_mod, "exa_search", _boom)
    monkeypatch.setattr(subagent_mod, "ddg_search", _boom)
    with pytest.raises(RuntimeError, match="all sources failed"):
        _retrieve("q", SubAgentConfig())


def test_fallback_disabled(monkeypatch):
    monkeypatch.setattr(subagent_mod, "web_search", _boom)
    monkeypatch.setattr(subagent_mod, "exa_search", _boom)
    called = {"ddg": False}
    monkeypatch.setattr(subagent_mod, "ddg_search",
                        lambda q, top_k=5: called.__setitem__("ddg", True) or [])
    with pytest.raises(RuntimeError):
        _retrieve("q", SubAgentConfig(enable_search_fallback=False))
    assert called["ddg"] is False
