#!/usr/bin/env python3
"""导出可提交给代码审查者的脱敏 Web run 轨迹。

原始 ``runs/`` 永远保持 gitignored。本脚本默认只复制三个展示文件；诊断白名单须显式开启，并对所有
字符串递归脱敏；JSONL 会逐行解析后重新序列化，遇到坏行直接失败，避免悄悄产出
不完整审查材料。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any


ALLOWED_FILES = ("meta.json", "events.jsonl", "report.md")
# P4 显式导出诊断资料；仍不允许 checkpoint、环境文件或任意文件名。
DIAGNOSTIC_FILES = (
    "summary.json",
    "config.json",
    "requests.jsonl",
    "retrieval.jsonl",
    "initial_state.json",
    "result.json",
)
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 末尾不能用 Unicode ``\b``：凭据后若紧跟中文，Python 会把两边都视为
    # word char，导致真实日志漏脱敏。改用 ASCII 凭据字符集的负向前瞻。
    (
        "openai_key",
        re.compile(
            r"(?<![A-Za-z0-9_-])(?:"
            r"sk-(?:proj|svcacct|or-v1)-[A-Za-z0-9_-]{16,}"
            r"|sk-[A-Za-z0-9]{20,}"
            r")(?![A-Za-z0-9_-])"
        ),
    ),
    (
        "github_token",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:github_pat_|gh[opusr]_|ghr_)"
            r"[A-Za-z0-9_]{12,}(?![A-Za-z0-9_])"
        ),
    ),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{12,}\b")),
    (
        "bearer_token",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_text(
    text: str,
    *,
    home: str,
    counts: dict[str, int],
) -> str:
    if home and home in text:
        counts["user_home"] = counts.get("user_home", 0) + text.count(home)
        text = text.replace(home, "<USER_HOME>")
    for label, pattern in _SECRET_PATTERNS:
        text, n_replaced = pattern.subn(f"<REDACTED_{label.upper()}>", text)
        if n_replaced:
            counts[label] = counts.get(label, 0) + n_replaced
    return text


def _redact_value(
    value: Any,
    *,
    home: str,
    counts: dict[str, int],
) -> Any:
    if isinstance(value, str):
        return _redact_text(value, home=home, counts=counts)
    if isinstance(value, list):
        return [_redact_value(item, home=home, counts=counts) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, home=home, counts=counts) for key, item in value.items()}
    return value


def _write_json(
    source: Path,
    target: Path,
    *,
    home: str,
    counts: dict[str, int],
) -> None:
    data = json.loads(source.read_text(encoding="utf-8"))
    redacted = _redact_value(data, home=home, counts=counts)
    target.write_text(
        json.dumps(redacted, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(
    source: Path,
    target: Path,
    *,
    home: str,
    counts: dict[str, int],
) -> None:
    output: list[str] = []
    for line_no, line in enumerate(
        source.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{source}:{line_no}: invalid JSONL") from exc
        redacted = _redact_value(event, home=home, counts=counts)
        output.append(json.dumps(redacted, ensure_ascii=False, separators=(",", ":")))
    target.write_text("\n".join(output) + "\n", encoding="utf-8")


def export_runs(
    run_ids: Iterable[str],
    *,
    source_root: Path,
    output_root: Path,
    home: str | None = None,
    include_diagnostics: bool = False,
) -> dict[str, Any]:
    """导出 run，并返回包含文件哈希与脱敏计数的 manifest。"""
    home_value = str(Path.home()) if home is None else home
    manifest: dict[str, Any] = {
        "format": 1,
        "note": "Sanitized review fixtures; raw runs remain under gitignored runs/.",
        "runs": [],
    }
    output_root.mkdir(parents=True, exist_ok=True)

    for run_id in run_ids:
        source_dir = source_root / run_id
        if not source_dir.is_dir():
            raise FileNotFoundError(f"missing run directory: {source_dir}")
        target_dir = output_root / run_id
        target_dir.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        exported: list[dict[str, str]] = []

        filenames = ALLOWED_FILES + (DIAGNOSTIC_FILES if include_diagnostics else ())
        for filename in filenames:
            source = source_dir / filename
            if not source.exists():
                continue
            target = target_dir / filename
            if filename.endswith(".json"):
                _write_json(source, target, home=home_value, counts=counts)
            elif filename.endswith(".jsonl"):
                _write_jsonl(source, target, home=home_value, counts=counts)
            else:
                text = _redact_text(
                    source.read_text(encoding="utf-8"),
                    home=home_value,
                    counts=counts,
                )
                target.write_text(text, encoding="utf-8")
            exported.append({"path": filename, "sha256": _sha256(target)})

        if not any(item["path"] == "events.jsonl" for item in exported):
            raise FileNotFoundError(f"{source_dir} has no events.jsonl")
        manifest["runs"].append(
            {
                "run_id": run_id,
                "files": exported,
                "redactions": dict(sorted(counts.items())),
            }
        )

    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("runs"))
    parser.add_argument("--output", type=Path, default=Path("review_runs"))
    parser.add_argument(
        "--include-diagnostics", action="store_true", help="显式附带 P4 配置、结果和请求摘要白名单"
    )
    parser.add_argument("run_ids", nargs="+", help="要导出的本机运行 ID")
    args = parser.parse_args()
    manifest = export_runs(
        args.run_ids,
        source_root=args.source,
        output_root=args.output,
        include_diagnostics=args.include_diagnostics,
    )
    print(f"exported {len(manifest['runs'])} sanitized runs to {args.output}")


if __name__ == "__main__":
    main()
