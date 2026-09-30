"""给 Claude Code 用的本机 Tavily 搜索 MCP 服务，走项目 .env 里配置的 key 池。

连接方式与研究工作台完全相同（复用 ``dra.tools._get_client``）：
配了 ``TAVILY_BASE_URL`` 就走 key 池并用 ``TAVILY_MASTER_KEY`` 认证，否则直连官方并用 ``TAVILY_API_KEY``。
地址和 key 只从项目 .env 读取，不写进 MCP 配置。

注册（只需一次）::

    claude mcp add tavily-pool -s user -- \
        uv run --directory /path/to/research-workbench-clean python scripts/tavily_pool_mcp.py

提供五个只读工具：搜索、提取网页正文、列网站结构（map）、抓取多个页面（crawl）、
让 Tavily 做一整轮调研（research）。通过标准输入输出与 Claude Code 通信。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dra.tools import _get_client  # noqa: E402

try:  # mcp SDK 2.x 把 FastMCP 改名为 MCPServer；兼容两种写法。
    from mcp.server.fastmcp import FastMCP as _Server
except ImportError:  # pragma: no cover
    from mcp.server.mcpserver import MCPServer as _Server  # type: ignore[no-redef]

server = _Server("tavily-pool")


def _compact(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    return [{k: row[k] for k in keys if row.get(k) not in (None, "")} for row in rows]


@server.tool()
def tavily_search(
    query: str,
    max_results: int = 5,
    search_depth: Literal["basic", "advanced"] = "basic",
    topic: Literal["general", "news"] = "general",
    time_range: Literal["day", "week", "month", "year"] | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> str:
    """联网搜索，返回标题、网址、摘要和发布日期（经本机 Tavily key 池）。"""
    kwargs: dict = {"query": query, "max_results": max(1, min(max_results, 20)),
                    "search_depth": search_depth, "topic": topic}
    if time_range:
        kwargs["time_range"] = time_range
    if include_domains:
        kwargs["include_domains"] = include_domains
    if exclude_domains:
        kwargs["exclude_domains"] = exclude_domains
    result = _get_client().search(**kwargs)
    rows = _compact(result.get("results", []), ("title", "url", "content", "published_date", "score"))
    return json.dumps({"query": query, "results": rows}, ensure_ascii=False, indent=1)


@server.tool()
def tavily_extract(urls: list[str]) -> str:
    """读取一个或多个网页的正文（经本机 Tavily key 池）。每页正文最多返回 20000 字。"""
    result = _get_client().extract(urls=urls[:10])
    pages = [{"url": row.get("url"), "content": (row.get("raw_content") or "")[:20000]}
             for row in result.get("results", [])]
    failed = [row.get("url") for row in result.get("failed_results", [])]
    return json.dumps({"results": pages, "failed": failed}, ensure_ascii=False, indent=1)


@server.tool()
def tavily_map(
    url: str,
    instructions: str | None = None,
    max_depth: int = 1,
    limit: int = 50,
    select_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> str:
    """列出一个网站的页面地址（网站结构图），不取正文。instructions 可用自然语言说明要找哪类页面。"""
    kwargs: dict = {"url": url, "max_depth": max(1, min(max_depth, 5)), "limit": max(1, min(limit, 200))}
    if instructions:
        kwargs["instructions"] = instructions
    if select_paths:
        kwargs["select_paths"] = select_paths
    if exclude_paths:
        kwargs["exclude_paths"] = exclude_paths
    result = _get_client().map(**kwargs)
    return json.dumps({"base_url": result.get("base_url", url), "urls": result.get("results", [])},
                      ensure_ascii=False, indent=1)


@server.tool()
def tavily_crawl(
    url: str,
    instructions: str | None = None,
    max_depth: int = 1,
    limit: int = 10,
    select_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
) -> str:
    """从一个网址出发抓取多个页面的正文。instructions 可用自然语言说明要抓哪类内容。每页正文最多返回 8000 字。"""
    kwargs: dict = {"url": url, "max_depth": max(1, min(max_depth, 5)), "limit": max(1, min(limit, 50)),
                    "format": "markdown"}
    if instructions:
        kwargs["instructions"] = instructions
    if select_paths:
        kwargs["select_paths"] = select_paths
    if exclude_paths:
        kwargs["exclude_paths"] = exclude_paths
    result = _get_client().crawl(**kwargs)
    pages = [{"url": row.get("url"), "content": (row.get("raw_content") or "")[:8000]}
             for row in result.get("results", [])]
    return json.dumps({"base_url": result.get("base_url", url), "pages": pages}, ensure_ascii=False, indent=1)


@server.tool()
def tavily_research(
    input: str,
    model: Literal["mini", "pro", "auto"] = "mini",
    wait_seconds: int = 300,
) -> str:
    """让 Tavily 自己做一整轮调研并返回带引用的报告。耗时较长、消耗额度明显多于普通搜索，只在需要综合调研时使用。

    先提交任务，再每 5 秒查一次结果，最多等 wait_seconds 秒；超时则返回任务编号和当前状态。
    """
    client = _get_client()
    started = client.research(input=input, model=model)
    request_id = started.get("request_id")
    if not request_id:  # 服务端直接返回了结果
        return json.dumps(started, ensure_ascii=False, indent=1)
    deadline = time.monotonic() + max(10, min(wait_seconds, 900))
    status = started.get("status", "pending")
    while time.monotonic() < deadline:
        result = client.get_research(request_id)
        status = result.get("status", status)
        if status in ("completed", "failed"):
            return json.dumps(result, ensure_ascii=False, indent=1)
        time.sleep(5)
    return json.dumps({"request_id": request_id, "status": status,
                       "note": f"{wait_seconds} 秒内没有完成，可稍后用同一任务编号再查"}, ensure_ascii=False)


if __name__ == "__main__":
    server.run()
