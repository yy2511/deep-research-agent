"""管理命令：``python -m dra.admin <命令>``（开发时前面加 ``uv run``）。

本步（#28）只有 ``migrate``；账号相关命令在阶段1-2 加入。
设计见 docs/design/阶段1-数据库与用户.md §2「管理命令」。
"""
from __future__ import annotations

import argparse
import sys

from dra.db import Database, DatabaseConfigError, DatabaseNotReady, head_revision


def _migrate(_args: argparse.Namespace) -> int:
    database = Database.from_env()
    try:
        before = database.current_revision()
        after = database.upgrade()
    finally:
        database.dispose()
    if before == after:
        print(f"数据库 {database.safe_url} 已是最新（{after}）")
    else:
        print(f"数据库 {database.safe_url} 已从 {before or '空库'} 升级到 {after}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m dra.admin", description="研究工作台管理命令")
    commands = parser.add_subparsers(dest="command", required=True, metavar="命令")
    migrate = commands.add_parser(
        "migrate", help=f"把数据库升级到最新迁移（当前最新 {head_revision()}）"
    )
    migrate.set_defaults(handler=_migrate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (DatabaseConfigError, DatabaseNotReady) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
