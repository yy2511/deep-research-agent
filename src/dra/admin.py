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


def _showcase(args: argparse.Namespace) -> int:
    """示例（阶段 2 第 2 步 #47）：设为示例前列出将公开的内容，确认后才写入。"""
    from dra.api.services import showcases
    from dra.paths import RUNS_DIR
    from dra.runstore import RunStore

    store = RunStore(args.runs_dir or RUNS_DIR)
    database = _ready_database()
    try:
        if args.action == "list":
            items = showcases.list_public(database, store)
            if not items:
                print("还没有示例")
            for item in items:
                state = "" if item["available"] else "（运行文件缺失，打开时显示不可用）"
                print(f"{item['conversation_id']}  {item['title']}  · {item['turn_count']} 轮{state}")
            return 0
        if not args.conversation_id:
            raise _UsageError(f"showcase {args.action} 需要对话编号")
        if args.action == "remove":
            showcases.remove(database, args.conversation_id)
            print(f"已取消示例 {args.conversation_id}；该对话恢复为仅自己可见，可以继续追问")
            return 0
        if not args.title:
            raise _UsageError("设为示例需要 --title")
        review = showcases.review(database, store, args.conversation_id)
        print(f"将公开对话 {args.conversation_id}（所有人可见，包括未登录的访客）：")
        for turn in review.turns:
            print(f"  第 {turn['turn_index']} 轮：{turn['query']}")
        print(f"  附件 {len(review.artifacts)} 个" + (f"：{'、'.join(review.artifacts)}" if review.artifacts else ""))
        print(f"  代码执行记录 {review.executions} 次")
        print("  研究过程、来源原文、证据卡也会一起公开。作为示例期间这个对话不能再追问。")
        if not args.yes and input("确认设为示例？输入 y 确认：").strip().lower() != "y":
            print("已取消，没有改动")
            return 1
        showcases.add(database, store, args.conversation_id, args.title, sort_order=args.order)
        print(f"已设为示例：{args.title}")
        return 0
    finally:
        database.dispose()


def _governance(args: argparse.Namespace) -> int:
    from dra.api.services import user_management as management
    database = _ready_database()
    try:
        if args.command == "list-users":
            for user in management.cli_users(database):
                print(f"{user['id']}  {user['email'] or user['display_name'] or '(无显示名)'}  "
                      f"{user['role']}  {user['status']}")
            return 0
        candidates = {user['id']: user for user in management.cli_users(database)}
        target = candidates.get(args.user_id)
        if target is None:
            raise _UsageError("目标账号不存在")
        print(f"目标账号：id={target['id']}，显示名={target['display_name'] or '(无)'}，"
              f"邮箱={target['email'] or '(无)'}，角色={target['role']}，状态={target['status']}")
        if target['role'] != 'admin' or target['status'] != 'active':
            raise _UsageError("目标必须是有效的平台管理员")
        if args.command == "recover-owner":
            old = candidates.get(args.old_owner_id)
            if old is None:
                raise _UsageError("原负责人账号不存在")
            print(f"原负责人：id={old['id']}，显示名={old['display_name'] or '(无)'}，"
                  f"邮箱={old['email'] or '(无)'}，角色={old['role']}，状态={old['status']}")
            print(f"恢复原因：{args.reason}")
        print(f"将账号 id={args.user_id} 设为唯一平台负责人，原有登录会话会失效。")
        if not args.yes and input("确认目标身份？输入 y：").strip().lower() != "y":
            print("已取消，没有改动")
            return 1
        if args.command == "bootstrap-owner":
            management.bootstrap_owner(database, args.user_id)
        else:
            management.recover_owner(database, args.old_owner_id, args.user_id, args.reason)
        print(f"平台负责人已设为 id={args.user_id}，请重新登录。")
        return 0
    finally:
        database.dispose()


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

    showcase = commands.add_parser("showcase", help="管理公开示例：add 设为示例、remove 取消、list 查看")
    showcase.add_argument("action", choices=("add", "remove", "list"))
    showcase.add_argument("conversation_id", nargs="?", help="对话编号（即该对话第一轮的研究编号）")
    showcase.add_argument("--title", help="示例标题（add 时必填）")
    showcase.add_argument("--order", type=int, default=0, help="排序，小的在前，默认 0")
    showcase.add_argument("--yes", action="store_true", help="不询问，直接确认（脚本用）")
    showcase.add_argument("--runs-dir", type=Path, help="运行目录，默认 <数据目录>/runs")
    showcase.set_defaults(handler=_showcase)
    listing = commands.add_parser("list-users", help="只读列出账号ID、身份与状态")
    listing.set_defaults(handler=_governance)
    for name in ("bootstrap-owner", "recover-owner"):
        command = commands.add_parser(name, help="显式初始化/恢复唯一平台负责人")
        command.add_argument("--user-id", type=int, required=True)
        command.add_argument("--yes", action="store_true", help="已人工核对，确认变更")
        if name == "recover-owner":
            command.add_argument("--old-owner-id", type=int, required=True)
            command.add_argument("--reason", required=True)
        command.set_defaults(handler=_governance)
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
