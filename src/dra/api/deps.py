"""依赖注入：存储与运行管理器挂在 app.state，接口通过 Depends 获取。

实例在 dra.api.app 里创建一次；测试替换 app.state 上的对象即可，不改模块全局变量。
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from dra.api.services.runs import RunRegistry
from dra.db import Database
from dra.runstore import PlanAttemptStore, RunStore


def get_store(request: Request) -> RunStore:
    return request.app.state.store


def get_plan_store(request: Request) -> PlanAttemptStore:
    return request.app.state.plan_store


def get_registry(request: Request) -> RunRegistry:
    return request.app.state.registry


def get_db(request: Request) -> Database | None:
    """数据库对象（连接池）。会话由业务层按操作自己开关，接口不持有会话（ADR 0006）。

    启动检查通过前为 None。
    """
    return request.app.state.db


StoreDep = Annotated[RunStore, Depends(get_store)]
PlanStoreDep = Annotated[PlanAttemptStore, Depends(get_plan_store)]
RegistryDep = Annotated[RunRegistry, Depends(get_registry)]
DatabaseDep = Annotated[Database | None, Depends(get_db)]
