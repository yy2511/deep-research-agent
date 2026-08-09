"""llm 模块护栏测试：把过去踩过的坑变成永久断言。

锁定两个关键行为：
1. dotenv override=True：项目以 .env 为权威，shell 同名变量进不来（上一轮的坑）。
2. 4xx 立刻上抛 / 5xx 退避重试：避免「key 错也重试好几次」浪费时间。
"""

import httpx
import pytest
from openai import AuthenticationError, InternalServerError

import dra.llm as llm_module


def _reset_cached_client():
    """清掉 lazy client 缓存，让下次 _get_client 重新走 env 读取。"""
    llm_module._clients.clear()


def _make_api_error(status: int, message: str = "x"):
    """构造一个真实形态的 OpenAI APIStatusError 子类异常。"""
    request = httpx.Request("POST", "https://test/v1/chat/completions")
    response = httpx.Response(status, request=request)
    if status == 401:
        return AuthenticationError(message, response=response, body=None)
    if status == 500:
        return InternalServerError(message, response=response, body=None)
    raise ValueError(f"unsupported status {status} in this helper")


# ---------------------------------------------------------------------------
# 护栏 1：dotenv override=True，shell 变量被 .env 覆盖
# ---------------------------------------------------------------------------


def test_dotenv_overrides_shell_env(monkeypatch, tmp_path):
    """模拟 shell 注入错误 key；测试专用 .env 应以 override=True 覆盖它。"""
    import os

    test_key = "sk-test-from-dotenv"
    test_env = tmp_path / ".env"
    test_env.write_text(f"OPENAI_API_KEY={test_key}\n", encoding="utf-8")
    monkeypatch.setattr(llm_module, "_ENV_PATH", test_env)
    monkeypatch.setenv("OPENAI_API_KEY", "WRONG-FROM-SHELL")
    _reset_cached_client()

    llm_module._get_client()  # 内部会 load_dotenv(override=True)

    actual = os.environ.get("OPENAI_API_KEY", "")
    print("\n[override 护栏] 注入 WRONG-FROM-SHELL → 测试 .env 的假 key 生效")
    assert actual == test_key, ".env 必须 win over shell"

    _reset_cached_client()


# ---------------------------------------------------------------------------
# 护栏 2：4xx 立即上抛，5xx 退避重试
# ---------------------------------------------------------------------------


def test_4xx_immediately_raises_without_retry(monkeypatch):
    """401 → RuntimeError，且 create() 只被调 1 次（不重试）。"""
    from unittest.mock import MagicMock
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = _make_api_error(401, "Invalid token")
    llm_module._clients["openai"] = fake_client
    # sleep 别真等
    monkeypatch.setattr("dra.llm.time.sleep", lambda s: None)

    with pytest.raises(RuntimeError, match="401") as ei:
        llm_module.chat([{"role": "user", "content": "hi"}], max_retries=2)

    n = fake_client.chat.completions.create.call_count
    print(f"\n[4xx 护栏] 401 上抛: {ei.value} | create() 调用次数: {n}")
    assert n == 1, f"4xx 不应重试，期望 1 次调用，实际 {n}"

    _reset_cached_client()


def test_5xx_retries_until_exhausted(monkeypatch):
    """500 → 应重试 max_retries 次，共 create() 调 (max_retries+1) 次后才上抛。"""
    from unittest.mock import MagicMock
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = _make_api_error(500, "server down")
    llm_module._clients["openai"] = fake_client
    monkeypatch.setattr("dra.llm.time.sleep", lambda s: None)  # 加速

    with pytest.raises(RuntimeError, match="500"):
        llm_module.chat([{"role": "user", "content": "hi"}], max_retries=2)

    n = fake_client.chat.completions.create.call_count
    print(f"\n[5xx 护栏] 500 重试 → create() 调用次数: {n}（期望 3）")
    assert n == 3, f"5xx 应重试 max_retries=2 次，期望 3 次调用，实际 {n}"

    _reset_cached_client()


def test_sdk_retries_are_disabled_at_client_boundary(monkeypatch):
    """项目的 chat 是唯一重试所有者，OpenAI SDK 不能再暗中多试两轮。"""
    from unittest.mock import MagicMock

    fake_client = MagicMock()
    constructor = MagicMock(return_value=fake_client)
    monkeypatch.setattr(llm_module, "OpenAI", constructor)
    monkeypatch.setattr(llm_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    _reset_cached_client()

    assert llm_module._get_client("openai") is fake_client
    assert constructor.call_args.kwargs["max_retries"] == 0
    _reset_cached_client()


def test_chat_passes_request_timeout_and_clamps_to_run_deadline(monkeypatch):
    """调用参数有明确 timeout；run 剩余时间更短时必须以它为准。"""
    import time
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))], usage=None,
    )
    llm_module._clients["openai"] = fake_client
    token = llm_module.set_request_deadline(time.monotonic() + 0.5)
    try:
        assert llm_module.chat(
            [{"role": "user", "content": "hi"}], request_timeout_s=12
        ) == "ok"
    finally:
        llm_module.reset_request_deadline(token)
        _reset_cached_client()

    timeout = fake_client.chat.completions.create.call_args.kwargs["timeout"]
    assert 0 < timeout <= 0.5


def test_transport_timeout_retries_once_then_raises_typed_error(monkeypatch):
    """普通节点最多人工重试一次，耗尽后给上层可识别的 timeout 类型。"""
    from unittest.mock import MagicMock

    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = httpx.ReadTimeout("slow")
    llm_module._clients["openai"] = fake_client
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)

    with pytest.raises(llm_module.LLMRequestTimeout):
        llm_module.chat([{"role": "user", "content": "hi"}], request_timeout_s=1)

    assert fake_client.chat.completions.create.call_count == 2
    _reset_cached_client()


# ---------------------------------------------------------------------------
# 流式 on_chunk：非侵入，返回值与非流式一致
# ---------------------------------------------------------------------------


class _Delta:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _Chunk:
    def __init__(self, content, usage=None):
        self.choices = [_Choice(content)]
        self.usage = usage


def test_on_chunk_streams_and_returns_full(monkeypatch):
    """给 on_chunk → 走 stream=True，逐片回调 + 返回完整字符串（与非流式一致）。"""
    from unittest.mock import MagicMock
    fake_client = MagicMock()

    def fake_create(**kwargs):
        assert kwargs.get("stream") is True       # on_chunk 触发流式
        return iter([_Chunk("Hel"), _Chunk("lo"), _Chunk("")])  # 末尾空片段不回调

    fake_client.chat.completions.create.side_effect = fake_create
    llm_module._clients["openai"] = fake_client

    pieces = []
    out = llm_module.chat(
        [{"role": "user", "content": "hi"}], on_chunk=pieces.append
    )
    print(f"\n[流式] 回调片段={pieces} 返回={out!r}")
    assert pieces == ["Hel", "lo"]        # 非空片段逐个回调
    assert out == "Hello"                 # 累计完整内容
    _reset_cached_client()


def test_no_on_chunk_does_not_stream(monkeypatch):
    """不给 on_chunk → 不应设 stream（现有调用方零变化）。"""
    from unittest.mock import MagicMock
    fake_client = MagicMock()
    resp = MagicMock()
    resp.choices[0].message.content = "plain"
    resp.usage.prompt_tokens = 1
    resp.usage.completion_tokens = 1
    fake_client.chat.completions.create.return_value = resp
    llm_module._clients["openai"] = fake_client

    out = llm_module.chat([{"role": "user", "content": "hi"}])
    assert out == "plain"
    assert "stream" not in fake_client.chat.completions.create.call_args.kwargs
    _reset_cached_client()


def test_non_json_html_response_reports_base_url_error_without_retry(monkeypatch):
    """中转首页返回 HTTP 200 HTML 时，应直报 API 路径错误，而不是访问 str.usage。"""
    from unittest.mock import MagicMock

    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = "<!doctype html><html></html>"
    llm_module._clients["codex521"] = fake_client

    with pytest.raises(llm_module.LLMProtocolError, match=r"CODEX521_BASE_URL.*?/v1"):
        llm_module.chat(
            [{"role": "user", "content": "hi"}],
            provider="codex521",
            model="gpt-test",
            max_retries=2,
        )

    assert fake_client.chat.completions.create.call_count == 1
    _reset_cached_client()


def test_on_chunk_callback_error_swallowed(monkeypatch):
    """回调里抛错不能弄崩主流程——仍返回完整内容。"""
    from unittest.mock import MagicMock
    fake_client = MagicMock()
    fake_client.chat.completions.create.side_effect = lambda **k: iter([_Chunk("a"), _Chunk("b")])
    llm_module._clients["openai"] = fake_client

    def boom(_):
        raise ValueError("callback boom")

    out = llm_module.chat([{"role": "user", "content": "hi"}], on_chunk=boom)
    assert out == "ab"   # 回调炸了也不影响返回
    _reset_cached_client()


# ---------------------------------------------------------------------------
# 护栏 3：多 provider 路由（接入 deepseek judge 引入）
# ---------------------------------------------------------------------------


def test_unknown_provider_raises():
    """拼错 provider 必须立刻报错，绝不默默走默认厂商（防 judge 偷偷退化成同源）。"""
    _reset_cached_client()
    with pytest.raises(RuntimeError, match="未知 provider"):
        llm_module._get_client("depseek")  # 故意拼错
    _reset_cached_client()


def test_providers_cached_independently(monkeypatch):
    """不同 provider 各自缓存、互不覆盖；chat 的 provider 参数应路由到对应 client。"""
    from unittest.mock import MagicMock
    _reset_cached_client()

    openai_client = MagicMock(name="openai")
    deepseek_client = MagicMock(name="deepseek")
    openai_client.chat.completions.create.return_value = _fake_completion("from-openai")
    deepseek_client.chat.completions.create.return_value = _fake_completion("from-deepseek")
    llm_module._clients["openai"] = openai_client
    llm_module._clients["deepseek"] = deepseek_client

    out_default = llm_module.chat([{"role": "user", "content": "hi"}])
    out_ds = llm_module.chat([{"role": "user", "content": "hi"}], provider="deepseek")

    print(f"\n[provider 路由] 默认→{out_default} | deepseek→{out_ds}")
    assert out_default == "from-openai", "默认 provider 应路由到 openai client"
    assert out_ds == "from-deepseek", "provider=deepseek 应路由到 deepseek client"
    assert openai_client.chat.completions.create.call_count == 1
    assert deepseek_client.chat.completions.create.call_count == 1

    _reset_cached_client()


def _fake_completion(text: str):
    """构造一个最小的 chat.completions.create 返回对象。"""
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = text
    return resp


# ---------------------------------------------------------------------------
# trace 插桩：chat 咽喉按 obs 上下文（timing.get_ctx）emit llm_call —— 实时可视化
# 中间节点的 input/output/耗时。input 截头尾、output 全量。
# ---------------------------------------------------------------------------


def test_truncate_input_short_passthrough():
    """短 input 原样返回（不截断）。"""
    out = llm_module._truncate_input([{"role": "user", "content": "hello"}])
    assert "hello" in out and "省略" not in out


def test_truncate_input_long_headtail():
    """超长 input 截头尾 + 省略标记（含整篇文档时页面不爆）。"""
    out = llm_module._truncate_input([{"role": "user", "content": "A" * 3000 + "MIDDLE" + "Z" * 3000}])
    assert "省略" in out
    assert out.startswith("【user】\nAAA")    # 头部保留
    assert out.rstrip().endswith("ZZZ")       # 尾部保留
    assert "MIDDLE" not in out                # 中段被切
    assert len(out) < 4000


def test_trace_emits_llm_call_on_trace_step():
    """obs step 命中 cross_worker_audit 前缀 → emit llm_call，带 I/O/耗时/token。"""
    import time
    from types import SimpleNamespace
    import dra.events as E
    import dra.timing as T

    tok = T.set_ctx(step="cross_worker_audit")
    got = []
    fn = E.subscribe(got.append)
    try:
        usage = SimpleNamespace(prompt_tokens=11, completion_tokens=22)
        llm_module._trace_llm_call(
            [{"role": "user", "content": "材料"}], "模型判断输出", time.monotonic(), "m1", usage
        )
    finally:
        E.unsubscribe(fn)
        T.reset_ctx(tok)

    calls = [e for e in got if e["type"] == "llm_call"]
    assert len(calls) == 1
    c = calls[0]
    assert c["step"] == "cross_worker_audit"
    assert c["sid"] is None and c["worker_iteration"] is None
    assert c["model"] == "m1" and c["output"] == "模型判断输出"
    assert c["io_complete"] is False
    assert c["in_tok"] == 11 and c["out_tok"] == 22
    assert isinstance(c["ms"], (int, float)) and c["ms"] >= 0


def test_trace_skips_non_trace_step():
    """非 TRACE 节点（format_gate 等纯函数）→ 不 emit llm_call（只计时、不抓 io）。"""
    import time
    import dra.events as E
    import dra.timing as T

    tok = T.set_ctx(sid="s1", worker_iteration=1, step="format_gate")
    got = []
    fn = E.subscribe(got.append)
    try:
        llm_module._trace_llm_call([{"role": "user", "content": "x"}], "out", time.monotonic(), "m", None)
    finally:
        E.unsubscribe(fn)
        T.reset_ctx(tok)
    assert not [e for e in got if e["type"] == "llm_call"]


def test_trace_output_full_not_truncated():
    """output 全量保留（看模型判断全貌）：万字 output 不截；usage=None 时无 token 字段。"""
    import time
    import dra.events as E
    import dra.timing as T

    tok = T.set_ctx(step="write_report")
    got = []
    fn = E.subscribe(got.append)
    long_out = "判断" * 5000  # 1 万字，远小于 _OUTPUT_CAP
    try:
        llm_module._trace_llm_call([{"role": "user", "content": "x"}], long_out, time.monotonic(), "m", None)
    finally:
        E.unsubscribe(fn)
        T.reset_ctx(tok)
    c = next(e for e in got if e["type"] == "llm_call")
    assert c["output"] == long_out      # output 不截
    assert "in_tok" not in c            # usage=None → 无 token 字段


def test_trace_full_io_mode_preserves_complete_long_input_and_output():
    """Web 调试档显式开启时，长 input/output 均不得经过普通 trace 截断。"""
    import time
    import dra.events as E
    import dra.timing as T

    long_input = "A" * 5000 + "MIDDLE" + "Z" * 5000
    long_output = "结果" * 40000
    ctx_token = T.set_ctx(step="assess_research_nodes")
    trace_token = llm_module.set_trace_full_io(True)
    got = []
    fn = E.subscribe(got.append)
    try:
        llm_module._trace_llm_call(
            [{"role": "system", "content": long_input}],
            long_output,
            time.monotonic(),
            "m",
            None,
        )
    finally:
        E.unsubscribe(fn)
        llm_module.reset_trace_full_io(trace_token)
        T.reset_ctx(ctx_token)

    call = next(e for e in got if e["type"] == "llm_call")
    assert call["input"] == f"【system】\n{long_input}"
    assert call["output"] == long_output
    assert call["io_complete"] is True


def test_trace_prefixes_cover_orchestration_nodes():
    """编排级节点都在 TRACE 范围；plan-node/Research Round I/O 不能成为 Web 黑盒。"""
    p = llm_module._TRACE_STEP_PREFIXES
    for n in (
        "build_research_plan",
        "revise_research_plan",
        "assess_research_nodes",
        "resolve_decisions",
        "compile_ready_tasks",
        "build_report_plan",
        "condense",
        "summarize",
    ):
        assert n in p
