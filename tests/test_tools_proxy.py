"""tools._get_client 双模式测试：直连 / 代理。

设计说明（呼应 _get_client 的 4 条分支）：
1. 直连默认：TAVILY_BASE_URL 未配 → 不传 api_base_url（行为字节级与老路一致）
2. 代理 + MASTER_KEY：BASE_URL 配且 MASTER_KEY 配 → 用 MASTER_KEY 作 key + 注入 api_base_url
3. 代理 URL 但无 MASTER_KEY：BASE_URL 配、MASTER_KEY 未配 → 回退用 TAVILY_API_KEY（调试友好）
4. 都没配：抛 RuntimeError（缺凭据守门）

陷阱：`_get_client` 调 `load_dotenv(..., override=True)`，会从磁盘 .env 反复重置环境变量，
绕过 monkeypatch.setenv。所以每个测试都要 mock 掉 load_dotenv（让它 no-op）。
"""

from unittest.mock import MagicMock, patch

import pytest

from dra import tools


@pytest.fixture(autouse=True)
def _reset_tavily_client_singleton():
    """每个用例前后清掉单例，避免跨测试串味。"""
    tools._client = None
    yield
    tools._client = None


@pytest.fixture
def _no_dotenv_reload():
    """禁用 _get_client 里的 load_dotenv，防磁盘 .env 反复覆盖 monkeypatch 设的变量。"""
    with patch("dra.tools.load_dotenv") as m:
        m.return_value = True
        yield m


def test_direct_mode_default(monkeypatch, _no_dotenv_reload):
    """直连模式（默认）：BASE_URL 未配 → TavilyClient 只收 api_key，不带 api_base_url。"""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-direct-key")
    monkeypatch.delenv("TAVILY_BASE_URL", raising=False)
    monkeypatch.delenv("TAVILY_MASTER_KEY", raising=False)

    with patch("dra.tools.TavilyClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        tools._get_client()

    mock_cls.assert_called_once()
    kwargs = mock_cls.call_args.kwargs
    assert kwargs == {"api_key": "tvly-direct-key"}  # 字节级老路：仅 api_key


def test_proxy_mode_with_master_key(monkeypatch, _no_dotenv_reload):
    """代理 + MASTER_KEY：用 master 作 key + 注入 api_base_url。"""
    monkeypatch.setenv("TAVILY_BASE_URL", "http://localhost:8088")
    monkeypatch.setenv("TAVILY_MASTER_KEY", "mk-secret-xyz")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-real-key")  # 在场但不应被用

    with patch("dra.tools.TavilyClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        tools._get_client()

    kwargs = mock_cls.call_args.kwargs
    assert kwargs == {
        "api_key": "mk-secret-xyz",
        "api_base_url": "http://localhost:8088",
    }


def test_proxy_url_only_falls_back_to_api_key(monkeypatch, _no_dotenv_reload):
    """代理 URL 配了但 MASTER_KEY 未配：回退用 TAVILY_API_KEY。

    这条分支是调试友好：本机 xuncv 起来还没填 MASTER_KEY 时，
    不强逼用户写两份；用现成 TAVILY_API_KEY 也能跑通。
    """
    monkeypatch.setenv("TAVILY_BASE_URL", "http://localhost:8088")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-fallback-key")
    monkeypatch.delenv("TAVILY_MASTER_KEY", raising=False)

    with patch("dra.tools.TavilyClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        tools._get_client()

    kwargs = mock_cls.call_args.kwargs
    assert kwargs == {
        "api_key": "tvly-fallback-key",
        "api_base_url": "http://localhost:8088",
    }


def test_missing_all_credentials_raises(monkeypatch, _no_dotenv_reload):
    """都没配 → RuntimeError（缺凭据守门，对应原 _get_client 行为）。"""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_BASE_URL", raising=False)
    monkeypatch.delenv("TAVILY_MASTER_KEY", raising=False)

    with patch("dra.tools.TavilyClient"):
        with pytest.raises(RuntimeError, match="TAVILY"):
            tools._get_client()


def test_client_is_singleton_within_run(monkeypatch, _no_dotenv_reload):
    """惰性初始化 + 单例：多次调 _get_client 不重新建 TavilyClient。"""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-direct-key")
    monkeypatch.delenv("TAVILY_BASE_URL", raising=False)

    with patch("dra.tools.TavilyClient") as mock_cls:
        mock_cls.return_value = MagicMock()
        c1 = tools._get_client()
        c2 = tools._get_client()

    assert c1 is c2
    assert mock_cls.call_count == 1  # 第二次 hit 单例
