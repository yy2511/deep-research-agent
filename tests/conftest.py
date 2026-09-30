"""pytest 全局配置：注册 --run-live 开关，默认跳过 live 测试。

本地纯逻辑测试默认跑（秒级、不花钱）；
联网 / 付费 API 测试用 @pytest.mark.live 标注，加 --run-live 才执行。
"""

from pathlib import Path

import pytest
from dotenv import load_dotenv


@pytest.fixture(autouse=True)
def _isolate_provider_connections(monkeypatch, tmp_path, request):
    """离线测试不读取或改写用户新增的密钥与模型目录。"""
    if "live" not in request.keywords:
        monkeypatch.setattr("dra.provider_registry._PATH", tmp_path / "providers.json")
        monkeypatch.setattr("dra.provider_registry._CATALOG_PATH", tmp_path / "catalog.json")


@pytest.fixture(autouse=True)
def _close_execution_pools():
    """测试结束回收待用容器，防测试间串用和真实 Docker 资源残留。"""
    yield
    from dra.execution_pool import shutdown_pools
    shutdown_pools()


@pytest.fixture(autouse=True)
def _execution_requires_explicit_test_opt_in(monkeypatch):
    """个人 .env 开启执行，不应把全部离线 Web 测试变成真实 Docker 测试。"""
    monkeypatch.setenv('DRA_EXECUTION_ENABLED', 'false')


@pytest.fixture(autouse=True)
def _isolate_obs_ctx():
    """每个测试后清空可观测 contextvar，防裸调 _run_subagent_sync 等在主线程 set 的
    上下文泄漏污染后续测试。生产路径经 asyncio.to_thread 的 copy_context 天然隔离，
    无需此清理——纯测试侧（裸调、无 context copy）的隔离。"""
    yield
    try:
        from dra import timing
        timing.clear_ctx()
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _block_real_exa(monkeypatch, request):
    """保险栓:防止非 live 测试真打 Exa API(烧钱+不确定)。

    Web 检索默认使用 Tavily + Exa 双源并发，任何
    mock 了 web_search 但没 mock exa_search 的旧测试会触发真 Exa 调用。
    autouse fixture 用 RuntimeError 兜底,把忘 mock 转成可见错误。

    @pytest.mark.live 测试放行(可以真打 API)。

    raising=False:测试 import dra.tools / dra.subagent 之前调用也不挂。
    """
    if "live" in request.keywords:
        return  # live 测试放行真 API 调用
    def _fail(*args, **kwargs):
        raise RuntimeError(
            "test must mock dra.subagent.exa_search "
            "(autouse fixture _block_real_exa). 请显式 "
            "monkeypatch.setattr(\"dra.subagent.exa_search\", ...)"
        )
    # 只 patch _retrieve 用的 subagent 命名空间;不动 dra.tools.exa_search
    # (test_tools_exa.py 直接测原函数行为,需要拿原引用)
    monkeypatch.setattr("dra.subagent.exa_search", _fail, raising=False)


def pytest_addoption(parser):
    parser.addoption("--run-docker", action="store_true", default=False, help="运行真实本机 Docker 验收（不调用 LLM）")
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="同时运行标了 @pytest.mark.live 的联网/付费集成测试",
    )


def pytest_configure(config):
    """只有显式运行 live 测试时才加载真实凭据；普通测试保持干净、可复现。"""
    if config.getoption("--run-live"):
        # override=True：live 测试以项目 .env 为准，避免被 shell 历史变量污染。
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)


def pytest_collection_modifyitems(config, items):
    """没传 --run-live 时，给所有 live 测试动态加 skip 标记。"""
    if not config.getoption("--run-docker"):
        for item in items:
            if "docker" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="需要 --run-docker 才跑"))
    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="需要 --run-live 才跑（联网/付费）")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)
