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
    def __init__(self, content, finish_reason=None):
        self.delta = _Delta(content)
        self.finish_reason = finish_reason


class _Chunk:
    def __init__(self, content, usage=None, finish_reason=None):
        self.choices = [_Choice(content, finish_reason)]
        self.usage = usage


def test_on_chunk_streams_and_returns_full(monkeypatch):
    """给 on_chunk → 走 stream=True，逐片回调 + 返回完整字符串（与非流式一致）。"""
    from unittest.mock import MagicMock
    fake_client = MagicMock()

    def fake_create(**kwargs):
        assert kwargs.get("stream") is True       # on_chunk 触发流式
        return iter([_Chunk("Hel"), _Chunk("lo"), _Chunk("", finish_reason="stop")])  # 末尾空片段不回调

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


def test_on_reasoning_streams_thinking_without_changing_result():
    """给 on_reasoning → 走流式，思考片段逐段回调，返回值仍只有正式回答。"""
    from unittest.mock import MagicMock
    fake_client = MagicMock()

    class Delta:
        def __init__(self, content=None, reasoning_content=None):
            self.content, self.reasoning_content = content, reasoning_content

    class Chunk:
        def __init__(self, **delta):
            finish = delta.pop("finish_reason", None)
            self.choices = [type("C", (), {"delta": Delta(**delta), "finish_reason": finish})()]
            self.usage = None

    def fake_create(**kwargs):
        assert kwargs.get("stream") is True
        return iter([Chunk(reasoning_content="先看"), Chunk(reasoning_content="题目"), Chunk(content='{"ok":1}', finish_reason="stop")])

    fake_client.chat.completions.create.side_effect = fake_create
    llm_module._clients["openai"] = fake_client
    thoughts = []
    out = llm_module.chat([{"role": "user", "content": "hi"}], on_reasoning=thoughts.append)
    assert thoughts == ["先看", "题目"]
    assert out == '{"ok":1}'
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
    fake_client.chat.completions.create.side_effect = lambda **k: iter([_Chunk("a"), _Chunk("b", finish_reason="stop")])
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
# 护栏 4：base_url 变量改名后仍读旧名，避免静默连错厂商
# ---------------------------------------------------------------------------


def _isolate_env(monkeypatch, tmp_path):
    """隔离真实 .env：本组测试只受 monkeypatch 设置的环境变量影响。"""
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    monkeypatch.setattr(llm_module, "_ENV_PATH", env)
    monkeypatch.setattr(llm_module, "load_dotenv", lambda *a, **k: False)
    _reset_cached_client()


def test_legacy_deepseek_base_url_env_is_still_read(monkeypatch, tmp_path):
    """只配旧名时必须仍生效：否则 base_url 解析成 None，SDK 静默改连官方 OpenAI。"""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DRA_DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://legacy.example/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    assert llm_module._resolve_base_url("deepseek") == "https://legacy.example/v1"
    client = llm_module._get_client("deepseek")
    assert str(client.base_url).rstrip("/") == "https://legacy.example/v1"
    _reset_cached_client()


def test_dotenv_with_only_the_legacy_name_resolves_end_to_end(monkeypatch, tmp_path):
    """高保真场景：部署方 .env 里只有旧名，且不 monkeypatch load_dotenv。

    直接调 `_resolve_base_url` 前必须已加载 .env，否则会把"已配置"读成未配置——
    这正是 check_provider 注释里记过的坑，所以这条走完整 `_get_client` 路径。
    """
    env = tmp_path / ".env"
    env.write_text(
        "DEEPSEEK_API_KEY=sk-test\nDEEPSEEK_BASE_URL=https://legacy.example/v1\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(llm_module, "_ENV_PATH", env)
    monkeypatch.delenv("DRA_DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    _reset_cached_client()

    client = llm_module._get_client("deepseek")  # 内部 load_dotenv(override=True)
    assert str(client.base_url).rstrip("/") == "https://legacy.example/v1"
    _reset_cached_client()


def test_primary_base_url_env_wins_over_legacy(monkeypatch, tmp_path):
    """新名优先；旧名只是回退，不能覆盖新配置。"""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.setenv("DRA_DEEPSEEK_BASE_URL", "https://new.example/v1")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://legacy.example/v1")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")

    assert llm_module._resolve_base_url("deepseek") == "https://new.example/v1"
    _reset_cached_client()


def test_no_base_url_env_resolves_to_none_instead_of_guessing(monkeypatch, tmp_path):
    """两个名字都没配时返回 None（走官方 OpenAI），不做任何猜测。"""
    _isolate_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DRA_DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    assert llm_module._resolve_base_url("deepseek") is None


def test_deepseek_keeps_its_legacy_base_url_alias():
    """改名必须保留回退名：删掉它等于让只配旧名的部署静默连错厂商。"""
    assert llm_module._PROVIDERS["deepseek"][1] == "DRA_DEEPSEEK_BASE_URL"
    assert "DEEPSEEK_BASE_URL" in llm_module._base_url_env_names("deepseek")


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
    """obs step 命中 lead_research 前缀 → emit llm_call，带 I/O/耗时/token。"""
    import time
    from types import SimpleNamespace
    import dra.events as E
    import dra.timing as T

    tok = T.set_ctx(step="lead_research")
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
    assert c["step"] == "lead_research"
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

    tok = T.set_ctx(step="lead_wrapup")
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
    ctx_token = T.set_ctx(step="research_brief")
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


def test_trace_prefixes_cover_current_roles():
    assert llm_module._TRACE_STEP_PREFIXES == (
        "condense", "vision_observation", "lead_research", "lead_wrapup", "subagent_research",
        "subagent_wrapup", "research_brief",
    )


def test_connection_error_discards_cached_client_before_retry(monkeypatch):
    """连接级故障必须先换连接再重试——否则重试复用断掉的 keep-alive 连接、原地再挂。

    回归 STATUS §4.1.15：三次整轮失败都停在连接阶段（约 5 秒后 SSL EOF、无回执），
    而 llm 侧此前既没有重试、也不丢弃缓存 client。tools.py 早已为 Tavily 修过同一坑。
    """
    created: list = []
    from unittest.mock import MagicMock
    state = {"calls": 0}  # 跨 client 共享：只有**第一次**尝试失败，重试必须能成功

    class _Completions:
        def create(self, **_kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise ConnectionError("SSL: UNEXPECTED_EOF_WHILE_READING")
            resp = MagicMock()
            resp.choices[0].message.content = "ok"
            resp.usage.prompt_tokens = 1
            resp.usage.completion_tokens = 1
            return resp

    class _FakeClient:
        def __init__(self):
            self.chat = MagicMock()
            self.chat.completions = _Completions()

    def constructor(*_args, **_kwargs):
        created.append(True)
        return _FakeClient()

    llm_module._clients.clear()
    monkeypatch.setattr(llm_module, "OpenAI", constructor)
    monkeypatch.setattr(llm_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)

    out = llm_module.chat([{"role": "user", "content": "hi"}], max_retries=1)
    assert out == "ok"
    assert len(created) == 2, "第一次连接失败后必须重建 client，而不是复用断连"
    _reset_cached_client()


# ---------------------------------------------------------------------------
# 流式空流 / 缺终止标记：按传输层中断重试，不再直接把整轮研究判死
# （2026-09-16 真实现场：历史运行 最后一轮拿到空流，
#  当时被当成不可恢复的协议错误 → 研究终止、只靠收尾拼稿）
# ---------------------------------------------------------------------------


class _FinishChoice:
    def __init__(self, content, finish_reason):
        self.delta = _Delta(content)
        self.finish_reason = finish_reason


class _ToolChunk:
    """带显式 finish_reason 的 chunk（缺省 None 模拟"流没有终止标记"）。"""

    def __init__(self, content="", finish_reason=None, usage=None):
        self.choices = [_FinishChoice(content, finish_reason)]
        self.usage = usage


def _install_stream_client(monkeypatch, streams):
    """假 client：每次 create 返回下一段流；记录构造次数（重试会重建 client）。"""
    from unittest.mock import MagicMock

    calls = {"n": 0}
    created: list[object] = []

    def fake_create(**_kwargs):
        stream = streams[calls["n"]]
        calls["n"] += 1
        return iter(stream)

    def constructor(*_args, **_kwargs):
        client = MagicMock()
        client.chat.completions.create.side_effect = fake_create
        created.append(client)
        return client

    llm_module._clients.clear()
    monkeypatch.setattr(llm_module, "OpenAI", constructor)
    monkeypatch.setattr(llm_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")
    monkeypatch.setattr(llm_module.time, "sleep", lambda _s: None)
    return calls, created


_TOOLS = [{"type": "function", "function": {"name": "search_sources", "parameters": {}}}]


def test_unterminated_stream_is_retried_on_a_fresh_client(monkeypatch):
    """空流（有 choice、无内容、无 finish_reason）→ 换连接重发一次，第二轮正常返回。"""
    calls, created = _install_stream_client(
        monkeypatch,
        [
            [_ToolChunk()],  # 第 1 次：空流
            [_ToolChunk("已核对完成", finish_reason="stop")],  # 第 2 次：正常收尾
        ],
    )
    pieces: list[str] = []
    turn = llm_module.call_tools(
        [{"role": "user", "content": "x"}],
        tools=_TOOLS,
        provider="opencode",
        model="deepseek-v4.1-flash",
        on_chunk=pieces.append,
        max_retries=1,
    )
    assert calls["n"] == 2, "空流必须重发一次"
    assert len(created) == 2, "重发要用新建的 client（复用可疑连接会原地再挂）"
    assert turn.attempts == 2, "重试次数要如实带上：账本据此才能看出重试触发过"
    assert turn.incomplete is False
    assert turn.content == "已核对完成"
    assert pieces == ["已核对完成"]
    _reset_cached_client()


def test_unterminated_stream_after_retries_returns_incomplete_turn(monkeypatch):
    """重试用尽仍是空流：返回未完成 turn（不抛异常），工具一个都不执行。

    上层据此记 protocol_error 并走收尾——这是原有语义，本次只是不再"一次即判死"。
    """
    calls, created = _install_stream_client(monkeypatch, [[_ToolChunk()], [_ToolChunk()]])
    turn = llm_module.call_tools(
        [{"role": "user", "content": "x"}],
        tools=_TOOLS,
        provider="opencode",
        model="deepseek-v4.1-flash",
        on_chunk=lambda _c: None,
        max_retries=1,
    )
    assert calls["n"] == 2
    assert len(created) == 2
    assert turn.attempts == 2, "重试用尽也要如实记几次尝试"
    assert turn.incomplete is True
    assert turn.tool_calls == []
    assert "终止标记" in turn.note
    _reset_cached_client()


def test_explicit_length_finish_is_not_retried(monkeypatch):
    """显式异常终止（finish_reason=length）不重试：重发解决不了，直接标不完整。"""
    calls, _created = _install_stream_client(
        monkeypatch, [[_ToolChunk("半截", finish_reason="length")]]
    )
    turn = llm_module.call_tools(
        [{"role": "user", "content": "x"}],
        tools=_TOOLS,
        provider="opencode",
        model="deepseek-v4.1-flash",
        on_chunk=lambda _c: None,
        max_retries=1,
    )
    assert calls["n"] == 1, "显式异常终止不应重试"
    assert turn.attempts == 1, "没重试就是 1 次尝试"
    assert turn.incomplete is True
    assert "finish_reason=length" in turn.note
    _reset_cached_client()


def test_stream_cut_before_finish_is_retried_not_returned_as_empty():
    """流在结束标记之前断开：按传输故障重试，而不是当成“模型返回了空内容”。"""
    from unittest.mock import MagicMock
    fake_client = MagicMock()
    streams = [iter([_Chunk("")]), iter([_Chunk("ok", finish_reason="stop")])]
    fake_client.chat.completions.create.side_effect = lambda **k: streams.pop(0)
    llm_module._clients["openai"] = fake_client
    import time as _time
    original_sleep, _time.sleep = _time.sleep, lambda _s: None
    try:
        out = llm_module.chat([{"role": "user", "content": "hi"}], on_chunk=lambda _p: None, max_retries=1)
    finally:
        _time.sleep = original_sleep
    assert out == "ok" and fake_client.chat.completions.create.call_count == 2
    _reset_cached_client()


def test_thinking_only_model_is_retried_with_thinking_and_remembered():
    """服务商拒绝关闭思考时：不带关闭参数重发一次，并记住该模型，之后不再尝试关闭。"""
    from unittest.mock import MagicMock
    import httpx
    from openai import BadRequestError

    fake_client = MagicMock()
    sent = []

    def create(**kwargs):
        sent.append(kwargs.get("extra_body"))
        if (kwargs.get("extra_body") or {}).get("thinking", {}).get("type") == "disabled":
            request = httpx.Request("POST", "https://example.test/v1/chat/completions")
            raise BadRequestError(
                "GLM-5.3 is a thinking-only model; disabling thinking is not supported.",
                response=httpx.Response(400, request=request), body=None)
        response = MagicMock()
        response.choices[0].message.content = "ok"
        return response

    fake_client.chat.completions.create.side_effect = create
    llm_module._clients["opencode"] = fake_client
    llm_module._THINKING_ONLY.discard(("opencode", "glm-5.3"))
    try:
        assert llm_module.chat([{"role": "user", "content": "hi"}], provider="opencode",
                               model="glm-5.3", reasoning=False) == "ok"
        assert sent == [{"thinking": {"type": "disabled"}}, None]
        assert llm_module.chat([{"role": "user", "content": "hi"}], provider="opencode",
                               model="glm-5.3", reasoning=False) == "ok"
        assert sent[-1] is None, "记住后不再发送关闭参数"
    finally:
        llm_module._THINKING_ONLY.discard(("opencode", "glm-5.3"))
        llm_module._clients.pop("opencode", None)


@pytest.mark.parametrize("reasoning, effort, given, expected", [
    (True, None, None, 0.6), (False, None, None, 0.3), (False, "low", None, 0.6), (True, None, 0.2, 0.2)])
def test_default_temperature_follows_thinking(reasoning, effort, given, expected):
    """开思考默认 0.6（DeepSeek 建议 0.5–0.7 防止无限重复），不开思考保持 0.3；显式指定时照用。"""
    assert llm_module._default_temperature(given, reasoning=reasoning, effort=effort) == expected
