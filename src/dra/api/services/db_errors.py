"""业务层共用：把数据库连接类故障翻译成“暂时不可用”（接口层 503）。

约束冲突等由各业务函数自己处理，这里不吞。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.exc import InterfaceError, OperationalError

from dra.api.services.errors import Unavailable

DB_UNAVAILABLE = "数据库暂时不可用，请稍后再试"


@contextmanager
def database_errors() -> Iterator[None]:
    try:
        yield
    except (OperationalError, InterfaceError) as exc:
        raise Unavailable(DB_UNAVAILABLE) from exc
