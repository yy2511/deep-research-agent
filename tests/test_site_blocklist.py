"""网站黑名单：规则写法、检索过滤、按 URL 读取、跳转、网页接口与追问继承。"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from dra import research, simple_research as sr, subagent, web
from dra.site_filter import is_blocked, normalize_blocked_sites, provider_exclude_domains
from test_budget_wrapup import Script, _turn
from test_simple_research import _doc
from test_web_research_e2e import isolated_web, _wait_done  # noqa: F401


def test_rules_are_normalized_and_bad_rules_rejected():
    rules = normalize_blocked_sites([" https://www.MDPI.com/2227-9709/11/3/64/ ", "*.example.org",
                                     "example.org", "", "doi.org/10.3390/Informatics11030064?x=1"])
    assert rules == ["mdpi.com/2227-9709/11/3/64", "example.org", "doi.org/10.3390/informatics11030064"]
    with pytest.raises(ValueError, match="不是有效"):
        normalize_blocked_sites(["not a site"])
    with pytest.raises(ValueError, match="最多"):
        normalize_blocked_sites([f"s{i}.com" for i in range(51)])


def test_domain_rule_covers_subdomains_and_path_rule_only_its_pages():
    rules = normalize_blocked_sites(["example.org", "mdpi.com/2227-9709/11/3/64"])
    assert is_blocked("https://example.org/a", rules)
    assert is_blocked("http://news.Example.org/b?x=1", rules)
    assert not is_blocked("https://notexample.org/", rules)
    assert is_blocked("https://www.mdpi.com/2227-9709/11/3/64", rules)
    assert is_blocked("https://www.mdpi.com/2227-9709/11/3/64/htm", rules)
    assert not is_blocked("https://www.mdpi.com/2227-9709/11/3/640", rules)
    assert not is_blocked("https://www.mdpi.com/other", rules)
    assert not is_blocked(None, rules)
    encoded = normalize_blocked_sites(["semanticscholar.org/paper/A%3A-Review"])
    assert is_blocked("https://www.semanticscholar.org/paper/a:-review/123", encoded)
    assert provider_exclude_domains(rules) == ["example.org"]


def test_search_services_receive_domain_exclusions(tmp_path, monkeypatch):
    seen = {}

    def fake(name):
        def search(query, top_k=5, **kw):
            seen[name] = kw.get("exclude_domains")
            return []
        return search

    monkeypatch.setattr(subagent, "web_search", fake("tavily"))
    monkeypatch.setattr(subagent, "exa_search", fake("exa"))
    config = sr.SimpleConfig(blocked_sites=["example.org", "mdpi.com/x"])
    sr.ToolContext(sr.RunStore(tmp_path / "r", run_id="r", query="q"), config).search_sources({"query": "q"})
    assert seen == {"tavily": ["example.org"], "exa": ["example.org"]}


def test_blocked_results_are_dropped_and_model_is_told(tmp_path):
    config = sr.SimpleConfig(blocked_sites=["mdpi.com/2227-9709"])
    docs = [_doc(id="a", url="https://www.mdpi.com/2227-9709/11/3/64"), _doc(id="b", url="https://sqlite.org/a")]
    ctx = sr.ToolContext(sr.RunStore(tmp_path / "r", run_id="r", query="q"), config, search_fn=lambda q: docs)
    payload, ok = ctx.search_sources({"query": "q"})
    data = json.loads(payload)
    assert ok and [row["url"] for row in data["results"]] == ["https://sqlite.org/a"]
    assert "剔除 1 条" in data["warnings"][0]
    assert "mdpi.com/2227-9709" in sr._source_scope_prompt(config)


def test_blocked_url_is_not_fetched_and_redirect_to_blocked_site_is_discarded(tmp_path, monkeypatch):
    calls = []

    def fetch(url, **kw):
        calls.append(url)
        return {"content": "正文" * 50, "url": "https://blocked.example.org/moved", "title": "t",
                "credits": 1, "request_id": "r1"}

    monkeypatch.setattr(sr, "fetch_url_content", fetch)
    config = sr.SimpleConfig(blocked_sites=["example.org"])
    ctx = sr.ToolContext(sr.RunStore(tmp_path / "r", run_id="r", query="q"), config)
    payload, ok = ctx.read_source({"url": "https://www.example.org/paper"})
    assert not ok and "黑名单" in payload and calls == []
    payload, ok = ctx.read_source({"url": "https://short.link/abc"})
    assert "跳转到了网站黑名单" in payload and calls == ["https://short.link/abc"]
    final = ctx.store.state["actions"][-1]
    assert final["action"] == "url_extract" and final["payload"]["ok"] is False
    assert final["payload"]["credits"] == 1, "已发出的请求仍要如实记账"


def test_web_request_saves_blocklist_and_followup_inherits_it(isolated_web, monkeypatch):  # noqa: F811
    _, registry = isolated_web
    seen = []
    original = research.run_minimal_research

    def run(query, requirements=None, *, config, **kw):
        seen.append(config.blocked_sites)
        script = Script([_turn(("finish_research", {"draft": "没有查到可引用的来源。"}))])
        return original(query, requirements, config=config, model_call=script, **kw)

    monkeypatch.setattr(research, "run_minimal_research", run)
    with TestClient(web.app) as client:
        bad = client.post("/api/research", json={"query": "q", "mode": "simple", "blocked_sites": ["???"]})
        assert bad.status_code == 400 and "不是有效" in bad.text
        response = client.post("/api/research", json={"query": "q", "mode": "simple",
                                                      "blocked_sites": ["https://www.Example.org/"]})
        assert response.status_code == 200, response.text
        run_id = response.json()["run_id"]
        done = _wait_done(client, run_id)
        assert done["meta"]["config_summary"]["blocked_sites"] == ["example.org"]
        deadline = time.monotonic() + 3
        while registry.runs[run_id].busy and time.monotonic() < deadline:
            time.sleep(.01)
        following = client.post(f"/api/research/{run_id}/followup", json={"request_id": "n1", "text": "继续"})
        assert following.status_code == 202, following.text
        _wait_done(client, following.json()["run_id"])
    assert seen == [["example.org"], ["example.org"]]
