"""pytest 全局配置：注册 --run-live 开关，默认跳过 live 测试。

本地纯逻辑测试默认跑（秒级、不花钱）；
联网 / 付费 API 测试用 @pytest.mark.live 标注，加 --run-live 才执行。
"""

import os
import shutil
from pathlib import Path

import pytest
from dotenv import load_dotenv

TEST_DATABASE_URL_ENV = "DRA_TEST_DATABASE_URL"


@pytest.fixture(scope="session")
def _database_source(request, tmp_path_factory):
    """测试数据库来源（ADR 0006）。

    默认：迁移一次到临时 SQLite 模板，每个测试复制一份，互不影响，不需要启动任何服务。
    --run-postgres：连 DRA_TEST_DATABASE_URL，开始时清空并迁移一次，每个测试后清空数据。
    """
    from dra.db import Database

    if request.config.getoption("--run-postgres"):
        database = Database.from_url(os.environ[TEST_DATABASE_URL_ENV])
        _reset_postgres_schema(database)
        database.upgrade()
        yield "postgres", database
        database.dispose()
        return
    template = tmp_path_factory.mktemp("database") / "template.sqlite"
    database = Database.from_url(f"sqlite:///{template}", allow_sqlite=True)
    database.upgrade()
    database.dispose()
    yield "sqlite", template


def _reset_postgres_schema(database) -> None:
    from sqlalchemy import text

    with database.engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


def _truncate_postgres(database) -> None:
    from sqlalchemy import text

    from dra.db_tables import Base

    names = ", ".join(table.name for table in Base.metadata.sorted_tables)
    with database.engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        # Keep the migration's singleton seed after clearing test accounts. SQLite
        # gets this row from its template copy; PostgreSQL reuses one migrated DB.
        connection.execute(text("INSERT INTO platform_governance (id,version,tracking_started_at) "
                                "VALUES (1,0,CURRENT_TIMESTAMP)"))


@pytest.fixture(autouse=True)
def _isolate_libraries(tmp_path, monkeypatch):
    """资料库按用户分目录（#37）：测试不碰本机数据目录，按目录缓存每个测试重新开始。"""
    from dra import library

    monkeypatch.setattr(library, "LIBRARY_BASE", tmp_path / "library-base")
    monkeypatch.setattr(library, "_libraries", {})


def install_library(store, user_id: int | None = None):
    """把测试用的资料库（通常带 TinyEncoder）装进按目录缓存。

    给了 user_id 时，store 必须建在该用户的资料库目录（library.user_library_root）。
    """
    from dra import library

    if user_id is not None:
        assert store.root.resolve() == library.user_library_root(user_id).resolve()
    library._libraries[store.root.resolve()] = store
    return store


@pytest.fixture(autouse=True)
def database(_database_source, tmp_path, monkeypatch):
    """每个测试一份已迁移到最新的数据库，并放进 Web 应用（app.state.db）。"""
    from dra.api.app import app
    from dra.db import Database

    # 本机 .env 里的开发库地址不参与离线测试。
    monkeypatch.setattr("dra.db.ENV_FILE", tmp_path / "no-such.env")
    kind, source = _database_source
    if kind == "sqlite":
        path = tmp_path / "dra-test.sqlite"
        shutil.copyfile(source, path)
        db = Database.from_url(f"sqlite:///{path}", allow_sqlite=True)
    else:
        db = source
    monkeypatch.setattr(app.state, "db", db)
    try:
        yield db
    finally:
        if kind == "sqlite":
            db.dispose()
        else:
            _truncate_postgres(db)


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
def _execution_requires_explicit_test_opt_in(monkeypatch, tmp_path, request):
    """个人 .env 开启执行，不应把全部离线 Web 测试变成真实 Docker 测试。

    网页设置里的执行开关会覆盖环境默认，所以离线测试同时隔离 runtime_config.json：
    不读取本机设置，也不在数据目录生成默认文件。
    """
    monkeypatch.setenv('DRA_EXECUTION_ENABLED', 'false')
    monkeypatch.setenv('DRA_EXECUTION_NETWORK_MODE', 'none')
    if "live" not in request.keywords:
        monkeypatch.setattr("dra.runtime_config._CONFIG_PATH", tmp_path / "runtime_config.json")
        monkeypatch.setattr("dra.runtime_config._cache", None)


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


TEST_ADMIN_EMAIL = "admin@test.local"


@pytest.fixture(autouse=True)
def logged_in_user(database, request):
    """默认让请求以一个管理员身份通过登录检查（设计 §6），现有接口测试不用各自登录。

    库里真有这个用户（以后的归属记录要引用它），密码哈希是占位值、不能用来登录。
    标了 ``real_auth`` 的测试不用这个默认，走真实的注册、登录与 Cookie。
    """
    if "real_auth" in request.keywords:
        yield None
        return
    from dra.api.app import app
    from dra.api.deps import current_user
    from dra.api.services.auth import UserInfo
    from dra.db_tables import User

    with database.session() as session:
        user = User(email=TEST_ADMIN_EMAIL, password_hash="!test-only", role="admin")
        session.add(user)
        session.flush()
        info = UserInfo(id=user.id, email=user.email, role=user.role)
    app.dependency_overrides[current_user] = lambda: info
    # 现有测试的研究多是直接写文件建的、没在数据库登记；归属检查（#31）默认放行，
    # 但仍保留编号格式检查。真实的归属行为由标了 real_auth 的 tests/test_run_ownership.py 测。
    from fastapi import HTTPException

    from dra.api import deps
    from dra.api.services.ids import RUN_ID_RE

    def owned_run(run_id: str) -> str:
        if not RUN_ID_RE.fullmatch(run_id):
            raise HTTPException(404, "run 不存在")
        return run_id

    def owned_conversation(conversation_id: str) -> str:
        if not RUN_ID_RE.fullmatch(conversation_id):
            raise HTTPException(404, "研究会话不存在")
        return conversation_id

    # 查看组（#47）同样默认放行，保留编号格式检查；真实行为由 tests/test_showcases.py 测。
    overrides = {current_user: lambda: info, deps.owned_run: owned_run,
                 deps.owned_conversation: owned_conversation,
                 deps.readable_run: owned_run, deps.readable_conversation: owned_conversation,
                 deps.visible_runs: lambda: (lambda _id: True),
                 deps.conversation_runs: lambda: (lambda _id: True)}
    app.dependency_overrides.update(overrides)
    try:
        yield info
    finally:
        for key in overrides:
            app.dependency_overrides.pop(key, None)


def pytest_addoption(parser):
    parser.addoption('--run-browser', action='store_true', default=False, help='真实本机浏览器，测试页面仅在本机，不调用模型或外部网站')
    parser.addoption("--run-native", action="store_true", default=False, help="运行真实 macOS OS 沙箱验收（无模型、无外网调用）")
    parser.addoption("--run-docker", action="store_true", default=False, help="运行真实本机 Docker 验收（不调用 LLM）")
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="同时运行标了 @pytest.mark.live 的联网/付费集成测试",
    )
    parser.addoption(
        "--run-postgres",
        action="store_true",
        default=False,
        help=f"改连 PostgreSQL（地址取 {TEST_DATABASE_URL_ENV}，库名须含 test），并运行 postgres 测试",
    )


def pytest_configure(config):
    """只有显式运行 live 测试时才加载真实凭据；普通测试保持干净、可复现。"""
    if config.getoption("--run-postgres"):
        _check_test_database_url()
    if config.getoption("--run-live"):
        # override=True：live 测试以项目 .env 为准，避免被 shell 历史变量污染。
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=True)


def _check_test_database_url() -> None:
    """--run-postgres 会清空整个 public schema：没给地址不静默改回 SQLite，库名不含 test 拒绝执行。"""
    from sqlalchemy.engine import make_url

    raw = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if not raw:
        raise pytest.UsageError(f"--run-postgres 需要设置 {TEST_DATABASE_URL_ENV}")
    name = make_url(raw).database or ""
    if "test" not in name:
        raise pytest.UsageError(
            f"{TEST_DATABASE_URL_ENV} 的库名须含 test（当前 {name!r}）：测试会清空整个库"
        )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-postgres"):
        for item in items:
            if "postgres" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="需要 --run-postgres 才跑"))
    if not config.getoption('--run-browser'):
        for item in items:
            if 'browser' in item.keywords:item.add_marker(pytest.mark.skip(reason='需要 --run-browser 才跑'))
    """没传 --run-live 时，给所有 live 测试动态加 skip 标记。"""
    if not config.getoption("--run-native"):
        for item in items:
            if "native" in item.keywords:item.add_marker(pytest.mark.skip(reason="需要 --run-native 才跑"))

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
