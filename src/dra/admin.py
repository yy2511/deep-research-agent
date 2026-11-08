"""管理命令：``python -m dra.admin <命令>``（开发时前面加 ``uv run``）。

设计见 docs/design/阶段1-数据库与用户.md §2「管理命令」。与 ``dra.web`` 一样是入口：
直接调用业务层（``dra.api.services``），不经过 HTTP。

密码默认交互输入两次；脚本里可用 ``--password-stdin`` 从标准输入读一行。密码不出现在
命令行参数里（会进 shell 历史和进程列表）。
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from dra.api.services import auth, run_records
from dra.api.services.errors import ServiceError
from dra.db import Database, DatabaseConfigError, DatabaseNotReady, head_revision


class _UsageError(Exception):
    pass


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("密码：")
    if getpass.getpass("再输入一次：") != first:
        raise _UsageError("两次输入的密码不一致")
    return first


def _ready_database() -> Database:
    database = Database.from_env()
    try:
        database.check_ready()
    except DatabaseNotReady:
        database.dispose()
        raise
    return database


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


def _create_admin(args: argparse.Namespace) -> int:
    password = _read_password(args)
    database = _ready_database()
    try:
        user = auth.create_user(database, args.email, password, role="admin")
    finally:
        database.dispose()
    print(f"已创建管理员 {user.email}（id {user.id}）")
    return 0


def _create_invite(args: argparse.Namespace) -> int:
    database = _ready_database()
    try:
        code, expires_at = auth.create_invite(database, days=args.days, note=args.note)
    finally:
        database.dispose()
    print(f"邀请码：{code}")
    print(f"有效期至 {expires_at:%Y-%m-%d %H:%M} UTC，只能使用一次；这是唯一一次显示，请现在保存。")
    return 0


def _reset_password(args: argparse.Namespace) -> int:
    password = _read_password(args)
    database = _ready_database()
    try:
        auth.reset_password(database, args.email, password)
    finally:
        database.dispose()
    print(f"已重设 {args.email.strip().lower()} 的密码，并让其所有登录失效")
    return 0


def _who(args: argparse.Namespace) -> str:
    return args.email.strip().lower() if args.email else f"GitHub 用户 {args.github.strip()}"


def _set_status(status: str):
    def run(args: argparse.Namespace) -> int:
        database = _ready_database()
        try:
            auth.set_user_status(database, args.email, status, github=args.github)
        finally:
            database.dispose()
        done = "已停用，并让其所有登录失效" if status == "disabled" else "已恢复"
        print(f"{_who(args)} {done}")
        return 0
    return run


def _set_role(args: argparse.Namespace) -> int:
    database = _ready_database()
    try:
        auth.set_role(database, args.role, email=args.email, github=args.github)
    finally:
        database.dispose()
    print(f"{_who(args)} 的角色已改为 {args.role}")
    return 0


def _import_legacy(args: argparse.Namespace) -> int:
    from dra.paths import RUNS_DIR

    runs_root = args.runs_dir or RUNS_DIR
    database = _ready_database()
    try:
        report = run_records.import_legacy(database, runs_root, args.owner)
    finally:
        database.dispose()
    print(f"运行目录：{runs_root}")
    print(f"新登记 {len(report.imported)} 条；已登记过、跳过 {len(report.already)} 条；"
          f"读不了、跳过 {len(report.unreadable)} 条。文件均未改动。")
    for run_id, reason in report.unreadable:
        print(f"  读不了：{run_id}（{reason}）")
    # 旧的全站资料库移到管理员自己的目录（#37）。服务停止时执行。
    from dra.library import LIBRARY_BASE, LibraryError, adopt_legacy_library, user_library_root

    library_base = args.library_dir or LIBRARY_BASE
    try:
        print(f"资料库：{adopt_legacy_library(library_base, user_library_root(report.owner_id))}")
    except LibraryError as exc:
        print(f"错误：资料库未搬移：{exc}", file=sys.stderr)
        return 2
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m dra.admin", description="研究工作台管理命令")
    commands = parser.add_subparsers(dest="command", required=True, metavar="命令")

    migrate = commands.add_parser(
        "migrate", help=f"把数据库升级到最新迁移（当前最新 {head_revision()}）"
    )
    migrate.set_defaults(handler=_migrate)

    def with_password(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--email", required=True)
        sub.add_argument("--password-stdin", action="store_true",
                         help="从标准输入读取密码（一行），供脚本使用")

    create_admin = commands.add_parser("create-admin", help="创建管理员账号")
    with_password(create_admin)
    create_admin.set_defaults(handler=_create_admin)

    create_invite = commands.add_parser("create-invite", help="生成一个邀请码（只显示一次）")
    create_invite.add_argument("--days", type=int, default=auth.INVITE_DEFAULT_DAYS,
                               help=f"有效天数，默认 {auth.INVITE_DEFAULT_DAYS}")
    create_invite.add_argument("--note", help="备注，例如发给谁")
    create_invite.set_defaults(handler=_create_invite)

    reset = commands.add_parser("reset-password", help="重设密码并让该用户所有登录失效")
    with_password(reset)
    reset.set_defaults(handler=_reset_password)

    legacy = commands.add_parser(
        "import-legacy",
        help="把 runs/ 下的旧研究登记到指定管理员名下，并把旧的全站资料库移到其目录（请在服务停止时执行）")
    legacy.add_argument("--owner", required=True, help="管理员邮箱")
    legacy.add_argument("--runs-dir", type=Path, help="运行目录，默认 <数据目录>/runs")
    legacy.add_argument("--library-dir", type=Path, help="旧的全站资料库目录，默认 <数据目录>/library")
    legacy.set_defaults(handler=_import_legacy)

    def with_user(sub: argparse.ArgumentParser) -> None:
        who = sub.add_mutually_exclusive_group(required=True)
        who.add_argument("--email", help="邮箱注册的账号")
        who.add_argument("--github", help="GitHub 登录的账号（GitHub 登录名）")

    for name, status, text in (("disable-user", "disabled", "停用账号并让其所有登录失效"),
                               ("enable-user", "active", "恢复账号")):
        sub = commands.add_parser(name, help=text)
        with_user(sub)
        sub.set_defaults(handler=_set_status(status))

    set_role = commands.add_parser("set-role", help="调整角色，例如把 GitHub 访客升为 user")
    with_user(set_role)
    set_role.add_argument("--role", required=True, choices=auth.ROLES)
    set_role.set_defaults(handler=_set_role)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except (DatabaseConfigError, DatabaseNotReady, ServiceError, _UsageError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
