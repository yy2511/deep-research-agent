"""有界的资料解析子进程。原件永不改写，输出是独立的解析文本坐标系。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

MAX_CHARS = 200_000
MAX_PAGES = 200


def parse(path: Path, suffix: str) -> dict:
    pages, warnings = [], []
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError("不支持加密 PDF，请上传未加密的文本 PDF")
        if len(reader.pages) > MAX_PAGES:
            raise ValueError(f"PDF 超过 {MAX_PAGES} 页")
        parts, end = [], 0
        for number, page in enumerate(reader.pages, 1):
            # 解压后的页面流也有界；整个解析另受进程内存/时间限制。
            content = page.get_contents()
            if content and len(content.get_data()) > 20 * 1024 * 1024:
                raise ValueError(f"第 {number} 页内容流过大")
            part = page.extract_text() or ""
            if not part.strip():
                warnings.append(f"第 {number} 页无文本层，未执行 OCR")
            if parts:
                parts.append("\n\n")
                end += 2
            start = end
            parts.append(part)
            end += len(part)
            pages.append({"page": number, "start": start, "end": end})
            if end > MAX_CHARS:
                raise ValueError(f"解析文本超过 {MAX_CHARS} 字符；请拆分文件后导入")
        text = "".join(parts)
    else:
        # 保留 CRLF / BOM 等原始文本，不做 markdown 清洗或 Unicode 归一化。
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("文本文件必须采用 UTF-8 编码") from exc
        if "\x00" in text:
            raise ValueError("文件含二进制内容，不能作为文本导入")
    if not text.strip():
        raise ValueError("未提取到正文；扫描 PDF 暂不支持 OCR")
    if len(text) > MAX_CHARS:
        raise ValueError(f"解析文本超过 {MAX_CHARS} 字符；请拆分文件后导入")
    return {"text": text, "pages": pages, "warnings": warnings, "parser": "pypdf-text-v1" if suffix == ".pdf" else "utf8-verbatim-v1"}


def main():
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    # macOS 不支持降低这些资源上限，由父进程监测 RSS 并终止；Linux 硬限地址空间。
    if sys.platform != "darwin":
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        ceiling = min(1024 ** 3, hard) if hard != resource.RLIM_INFINITY else 1024 ** 3
        resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))
    try:
        output = parse(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:
        output = {"error": str(exc) if isinstance(exc, ValueError) else "PDF 解析失败，文件可能损坏或超出解析限制"}
    Path(sys.argv[3]).write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
