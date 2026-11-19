"""管理员专用运营统计接口；显式响应模型避免意外暴露原始记录。"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from dra.api.common import service_errors
from dra.api.deps import ADMIN_ONLY, ReadyDatabaseDep
from dra.api.services import operations

router = APIRouter(dependencies=ADMIN_ONLY)


def _iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期须为 YYYY-MM-DD")
    return value


ISODate = Annotated[date, BeforeValidator(_iso_date)]
Count = Annotated[int, Field(ge=0)]


class Aggregate(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Roles(Aggregate):
    guest: Count
    user: Count
    admin: Count
    owner: Count


class Totals(Aggregate):
    users: Count
    enabled_users: Count
    disabled_users: Count
    roles: Roles
    showcases: Count


class Activity(Aggregate):
    new_users: Count
    research_users: Count
    research_tasks: Count
    conversations: Count


class DayActivity(Activity):
    date: date


class DateRange(Aggregate):
    start_date: date
    end_date: date


class Overview(Aggregate):
    as_of: datetime
    timezone: Literal["UTC"]
    scope: Literal["registered_database_records"]
    range: DateRange
    totals: Totals
    period: Activity
    daily: list[DayActivity]


@router.get("/api/admin/operations", response_model=Overview)
def get_operations(response: Response, db: ReadyDatabaseDep,
                   start_date: ISODate | None = None, end_date: ISODate | None = None,
                   all_time: bool = False) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with service_errors():
        return operations.overview(db, start_date, end_date, all_time)
