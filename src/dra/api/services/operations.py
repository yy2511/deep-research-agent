"""管理员运营汇总口径；不访问运行文件，不返回身份或研究内容。"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.exc import SQLAlchemyError

from dra.api.repositories import operations as repository
from dra.api.services.errors import InvalidRequest, Unavailable
from dra.db import Database


def overview(db: Database, start_date: date | None = None, end_date: date | None = None,
             all_time: bool = False) -> dict:
    if (start_date is None) != (end_date is None):
        raise InvalidRequest("请同时填写开始日期和结束日期")
    if all_time and start_date is not None:
        raise InvalidRequest("全部时间不能同时指定起止日期")
    try:
        with repository.snapshot(db) as snapshot:
            today = snapshot.as_of.date()
            if all_time:
                start_date, end_date = snapshot.earliest_date(), today
            elif start_date is None:
                start_date, end_date = today - timedelta(days=6), today
            if start_date > end_date:
                raise InvalidRequest("开始日期不能晚于结束日期")
            if end_date > today:
                raise InvalidRequest("结束日期不能晚于 UTC 今日")
            try:
                end_exclusive = end_date + timedelta(days=1)
            except OverflowError:
                raise InvalidRequest("结束日期无法转换为有效的统计区间") from None
            totals = snapshot.totals()
            period, sparse = snapshot.activity(
                datetime.combine(start_date, time.min, UTC),
                datetime.combine(end_exclusive, time.min, UTC),
            )
            # 先结束短数据库事务，再补齐响应的空日期。
            as_of = snapshot.as_of
    except SQLAlchemyError:
        raise Unavailable("运营统计暂不可用，请稍后重试") from None
    daily = []
    current = start_date
    while current < end_exclusive:
        day = current.isoformat()
        daily.append({"date": day, **{key: sparse.get(day, {}).get(key, 0) for key in period}})
        current += timedelta(days=1)
    return {"as_of": as_of, "timezone": "UTC", "scope": "registered_database_records",
            "range": {"start_date": start_date, "end_date": end_date},
            "totals": totals, "period": period, "daily": daily}
