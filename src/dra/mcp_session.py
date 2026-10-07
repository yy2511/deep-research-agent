"""同步研究线程使用的持久 MCP 连接；SDK 的进入/退出始终在同一个异步任务。"""
from __future__ import annotations

import asyncio
from concurrent.futures import Future, TimeoutError
import queue
import threading
import time

from mcp import Client, types
from dra.mcp_client import MCPError


class MCPSession:
    def __init__(self, transport, *, root, client_factory=Client):
        self.transport, self.root, self.client_factory = transport, root, client_factory
        self.requests = queue.Queue()
        self.stopping = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name='research-mcp', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            asyncio.run(self._lifecycle())
        except Exception:
            self.error = MCPError('浏览器 MCP 连接失败；请检查 Node、Playwright 安装和浏览器运行日志')
        finally:
            self.stopping.set()
            while not self.requests.empty():
                request = self.requests.get_nowait()
                if request is not None and not request[0].done():
                    request[0].set_exception(self.error or MCPError('MCP 会话已关闭'))

    async def _lifecycle(self):
        task = asyncio.create_task(self._serve())
        try:
            while not task.done():
                if self.stopping.is_set():
                    task.cancel()
                    break
                await asyncio.wait([task], timeout=.05)
            if not task.cancelled() and task.done():
                task.result()
        finally:
            await asyncio.gather(task, return_exceptions=True)

    async def _serve(self):
        async def roots(*_):
            return types.ListRootsResult(roots=[types.Root(uri=self.root.as_uri(), name='研究工作区')])
        async with self.client_factory(self.transport, list_roots_callback=roots, cache=None) as client:
            while True:
                request = await asyncio.to_thread(self.requests.get)
                if request is None:
                    return
                future, tool, arguments, deadline, cancelled = request
                if future.cancelled():
                    continue
                async def invoke():
                    if tool is not None:
                        return (await client.call_tool(tool, arguments)).model_dump(mode='json', by_alias=True, exclude_none=True)
                    tools, cursor = [], None
                    while True:
                        page = await client.list_tools(cursor=cursor)
                        tools.extend(t.model_dump(mode='json', by_alias=True, exclude_none=True) for t in page.tools)
                        cursor = page.next_cursor
                        if not cursor:
                            return tools
                task = asyncio.create_task(invoke())
                try:
                    while not task.done():
                        if self.stopping.is_set() or future.cancelled() or (cancelled and cancelled()):
                            raise MCPError('MCP 调用已取消')
                        if deadline is not None and time.monotonic() >= deadline:
                            raise MCPError('MCP 调用已到研究时限')
                        await asyncio.wait([task], timeout=.05)
                    result = task.result()
                    if not future.done():
                        future.set_result(result)
                except Exception as exc:
                    if not future.done():
                        future.set_exception(exc if isinstance(exc, MCPError) else MCPError('MCP 调用失败；会话日志已保留'))
                finally:
                    if not task.done():task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    def request(self, tool=None, arguments=None, *, deadline=None, cancelled=None):
        if self.stopping.is_set():raise self.error or MCPError('MCP 会话已关闭')
        future = Future()
        self.requests.put((future, tool, arguments or {}, deadline, cancelled))
        while True:
            if (cancelled and cancelled()) or (deadline is not None and time.monotonic() >= deadline):
                future.cancel()
                raise MCPError('MCP 调用已取消或到时')
            try:
                return future.result(timeout=.05)
            except TimeoutError:
                if self.stopping.is_set():raise self.error or MCPError('MCP 会话已关闭')

    def close(self):
        self.stopping.set()
        self.requests.put(None)
        self.thread.join()
