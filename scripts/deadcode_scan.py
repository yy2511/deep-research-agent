"""死代码与无消费方机制扫描：把"算了但没人读""定义了但没人发"这类东西列出来。

用法：``uv run python scripts/deadcode_scan.py``（只读，不改任何文件）

**这是什么**：一份启发式清单，不是证明。命名引用用正则数，进不了字符串拼接、
``getattr``、跨语言字符串等动态用法，所以它只负责"把可疑的东西捞出来给人看"，
判断仍要人工做（例如 ``artifact_dir`` 曾被误报，实际 ``scripts/`` 在用）。

**为什么固化**：升级期间一次写进大量机制，其中一部分没有消费方。一次性大清理会
重新长回来，所以每次收尾跑一遍这个脚本，比"记得去查"可靠。

扫描项：
1. ``src/`` 里模块级定义在源码内零引用（排除 FastAPI 路由处理函数）
2. ``src/dra`` 的模型字段没有任何跨文件消费方（同文件内读写不算）
3. 事件类型：定义了却从不以 ``EventType.X`` 发出
4. ``web/src`` 里导出却无人引用的符号
5. ``SimpleConfig`` 旋钮：除默认值外，是否有生产代码显式设过
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _py_files(base: str) -> list[Path]:
    return sorted((ROOT / base).rglob("*.py"))


def _read(paths: list[Path]) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in paths)


def _refs(name: str, text: str) -> int:
    """数 name 的出现次数。

    只在**词字符**一侧加 ``\\b``：``name`` 可能是 ``'"subagent_tool_call"'`` 这种
    以引号开头的串（前端消费事件名就是小写带引号的形式），此时加前缀 ``\\b`` 会
    永远匹配不到——第一版扫描脚本正是踩了这个坑，把 32 个正常事件误报成"无人消费"。
    """
    left = r"\b" if name[:1].isalnum() or name[:1] == "_" else ""
    right = r"\b" if name[-1:].isalnum() or name[-1:] == "_" else ""
    return len(re.findall(left + re.escape(name) + right, text))


def scan_zero_ref_defs() -> list[tuple[str, str, int]]:
    """模块级函数/类/常量在 src/ 内零引用（路由处理函数由装饰器注册，不算）。"""
    files = _py_files("src")
    all_src = _read(files)
    found: list[tuple[str, str, int]] = []
    for f in files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(ast.unparse(d).startswith("app.") for d in node.decorator_list):
                    continue
                name = node.name
            elif isinstance(node, ast.ClassDef):
                name = node.name
            elif isinstance(node, ast.Assign):
                name = next(
                    (t.id for t in node.targets if isinstance(t, ast.Name) and t.id.isupper()),
                    "",
                )
                if not name:
                    continue
            else:
                continue
            if _refs(name, all_src) <= 1:
                found.append((str(f.relative_to(ROOT)), name, node.lineno))
    return found


def scan_orphan_fields() -> list[tuple[str, str, str]]:
    """模型字段没有任何消费方：写进了台账/结果，但没有别的模块读它。

    消费方 = ``src/`` 其他文件 + ``scripts/`` + ``web/src``。``*Config`` 类不算——
    它们是旋钮，由 scan_unturned_knobs 单独看（在同一个文件里被读是正常的）。
    """
    found: list[tuple[str, str, str]] = []
    web = _read(sorted((ROOT / "web/src").rglob("*.ts*")))
    for f in _py_files("src/dra"):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        own = f.read_text(encoding="utf-8")
        others = _read([p for p in _py_files("src") if p != f] + _py_files("scripts"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name.endswith("Config"):
                continue
            body = ast.get_source_segment(own, node) or ""
            for m in re.finditer(r"(?m)^    ([a-z_][a-z0-9_]*): ", body):
                field = m.group(1)
                if field.startswith("model_config"):
                    continue
                if _refs(field, others) == 0 and _refs(field, web) == 0:
                    found.append((str(f.relative_to(ROOT)), node.name, field))
    return found


def scan_events() -> tuple[list[str], list[str]]:
    """返回 (定义了却从不发出的事件, 前端从不消费的事件)。"""
    src = _read(_py_files("src"))
    web = _read(sorted((ROOT / "web/src").rglob("*.ts*")))
    events_src = (ROOT / "src/dra/events.py").read_text(encoding="utf-8")
    pairs = re.findall(r'(?m)^\s*([A-Z_]+) = "([a-z_]+)"', events_src)
    never_emitted = [n for n, _ in pairs if _refs("EventType." + n, src) <= 0]
    never_consumed = [
        n for n, v in pairs if _refs(n, web) == 0 and _refs(v, web) == 0
    ]
    return never_emitted, never_consumed


def scan_frontend_exports() -> list[tuple[str, str]]:
    """web/src 里导出但全前端无人引用的符号。"""
    files = [p for p in sorted((ROOT / "web/src").rglob("*.ts*"))
             if ".test." not in p.name and "testHarness" not in p.name]
    all_src = _read(files)
    found: list[tuple[str, str]] = []
    for f in files:
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"export (?:function|const|type|interface) (\w+)", text):
            name = m.group(1)
            if _refs(name, all_src) <= 1:
                found.append((str(f.relative_to(ROOT)), name))
    return found


def scan_unturned_knobs() -> list[tuple[str, str]]:
    """SimpleConfig 的旋钮：除内核默认值外，有没有生产代码显式设过。"""
    core = ROOT / "src/dra/simple_research.py"
    if not core.exists():
        return []
    src = core.read_text(encoding="utf-8")
    head = src[src.index("class SimpleConfig"):]
    head = head[: head.index("\nclass ", 10)]
    fields = re.findall(r"(?m)^    ([a-z_][a-z0-9_]*): ", head)
    others = _read([p for p in _py_files("src") if p != core]
                   + _py_files("scripts"))
    return [(f, "生产代码里显式设过" if _refs(f + "=", others) else "从未显式设过")
            for f in fields]


def main() -> int:
    print("=" * 72)
    print("死代码与无消费方机制扫描（启发式，需人工判断）")
    print("=" * 72)

    print("\n[1] src/ 内零引用的模块级定义")
    zero = scan_zero_ref_defs()
    print("    共 %d 处" % len(zero))
    for path, name, line in zero:
        print("      %-28s %-34s L%d" % (path, name, line))

    print("\n[2] 没有跨文件消费方的模型字段（算了但没人读）")
    fields = scan_orphan_fields()
    print("    共 %d 处" % len(fields))
    for path, cls, field in fields:
        print("      %-28s %-22s %s" % (path, cls, field))

    print("\n[3] 事件类型")
    never_emitted, never_consumed = scan_events()
    print("    定义了却从不发出（%d）：%s" % (len(never_emitted), " ".join(never_emitted) or "无"))
    print("    前端从不消费（%d）：%s" % (len(never_consumed), " ".join(never_consumed) or "无"))

    print("\n[4] 前端导出但无人引用")
    exports = scan_frontend_exports()
    print("    共 %d 处" % len(exports))
    for path, name in exports:
        print("      %-40s %s" % (path, name))

    print("\n[5] SimpleConfig 旋钮是否被生产代码拧过")
    for field, note in scan_unturned_knobs():
        print("      %-34s %s" % (field, note))

    print("\n扫描结束。清单是线索不是结论；判断请对照 STATUS.md 与实际调用入口。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
