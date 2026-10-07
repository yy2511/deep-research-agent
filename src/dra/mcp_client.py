"""受控的外部 MCP 资料工具。HTTP / 凭据 / 协议留在后端，模型只见工具定义。

每次操作独立管理 SDK 连接，适配当前同步研究线程及并行子任务，不共享跨线程 event loop。
研究开始时固定工具目录；网络操作始终在锁外，超时/取消包括发现与连接阶段。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx2
from jsonschema import validators
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, model_validator

from dra.durable import atomic_json
from dra.paths import DATA_DIR

_CONFIG_PATH = DATA_DIR / "mcp_servers.json"
_CONFIG_LOCK = threading.RLock()
MAX_TOOLS = 16
MAX_RESULT_CHARS = 200_000


class MCPError(ValueError):
    """可交回用户/模型的脱敏错误；不传递 SDK 异常原文。"""


class MCPToolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_result_mode(cls, value):
        # 兼容已经保存的连接；新配置只记录允许工具，不再分类返回用途。
        if isinstance(value, dict) and value.get("result_mode") in ("discovery", "source"):
            return {k: v for k, v in value.items() if k != "result_mode"}
        return value


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,23}$")
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(max_length=2048)
    enabled: bool = True
    headers_env: dict[str, str] = Field(default_factory=dict, max_length=8)
    timeout_s: float = Field(default=30, ge=1, le=60)
    tools: list[MCPToolConfig] = Field(default_factory=list, max_length=MAX_TOOLS)

    @model_validator(mode="after")
    def validate_server(self):
        try:
            parsed = urlsplit(self.url)
            _ = parsed.port
        except ValueError:
            raise ValueError("MCP 地址不合法") from None
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() for c in self.url)):
            raise ValueError("MCP 地址须为无账号、查询参数或 fragment 的 HTTP 服务地址；凭据使用环境变量")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("远程 MCP 使用 HTTPS；HTTP 仅允许本机测试地址")
        if len({t.name for t in self.tools}) != len(self.tools):
            raise ValueError("同一服务的工具名称不能重复")
        lowered = set()
        for header, variable in self.headers_env.items():
            if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", header)
                    or header.lower() in lowered
                    or header.lower() in {"host", "content-type", "accept", "content-length", "connection"}
                    or header.lower().startswith("mcp-")):
                raise ValueError("凭据 header 名称不合法或覆盖了协议字段")
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", variable):
                raise ValueError("凭据填写环境变量名称，不能填写密钥值")
            lowered.add(header.lower())
        return self


class MCPSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    servers: list[MCPServerConfig] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def unique_ids(self):
        if len({s.id for s in self.servers}) != len(self.servers):
            raise ValueError("MCP 服务 ID 不能重复")
        return self


def load_settings(path: Path | None = None) -> MCPSettings:
    path = path or _CONFIG_PATH
    with _CONFIG_LOCK:
        if not path.exists():
            return MCPSettings()
        try:
            return MCPSettings.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise MCPError("MCP 配置读取失败，请检查 mcp_servers.json；未自动覆盖原文件") from None


def save_settings(settings: MCPSettings, path: Path | None = None) -> None:
    with _CONFIG_LOCK:
        atomic_json(path or _CONFIG_PATH, settings.model_dump(mode="json"))


def pin_servers(ids: list[str], path: Path | None = None) -> list[MCPServerConfig]:
    if len(ids) != len(set(ids)) or len(ids) > 4:
        raise MCPError("每次研究最多选择 4 个不同 MCP 服务")
    if not ids:
        return []
    configured = {s.id: s for s in load_settings(path).servers}
    selected = []
    for server_id in ids:
        server = configured.get(server_id)
        if server is None or not server.enabled or not server.tools:
            raise MCPError(f"MCP 服务 {server_id} 不存在、已停用或尚未选择工具")
        selected.append(server.model_copy(deep=True))
    if sum(len(s.tools) for s in selected) > MAX_TOOLS:
        raise MCPError(f"一次研究最多启用 {MAX_TOOLS} 个外部工具")
    return selected


def _headers(server: MCPServerConfig) -> dict[str, str]:
    headers = {}
    for name, variable in server.headers_env.items():
        value = os.environ.get(variable, "")
        if not value or '\r' in value or '\n' in value:
            raise MCPError(f"MCP 凭据环境变量 {variable} 未设置或无效")
        headers[name] = value
    return headers


def _check_secret_echo(payload: object, headers: dict[str, str]) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    for value in headers.values():
        secrets = [value, value.split(' ', 1)[-1]]
        if any(secret and secret in text for secret in secrets):
            # 快照必须逐字；含凭据回显时整份拒收，不清洗后冒充原始回执。
            raise MCPError("MCP 服务回显了配置凭据，本次结果未展示或保存")


async def _exchange(server, *, tool=None, arguments=None):
    headers = _headers(server)
    async with httpx2.AsyncClient(headers=headers, timeout=server.timeout_s, trust_env=False) as http:
        transport = streamable_http_client(server.url, http_client=http)
        async with Client(transport, read_timeout_seconds=server.timeout_s, cache=None,
                          input_required_max_rounds=1) as client:
            if tool is not None:
                response = await client.call_tool(tool, arguments)
                payload = response.model_dump(mode="json", by_alias=True, exclude_none=True)
                _check_secret_echo(payload, headers)
                if _text_result_size(payload) > MAX_RESULT_CHARS:
                    raise MCPError("MCP 返回超过 200000 字符上限，请缩小查询范围；未保存截断结果")
                if payload.get('resultType', 'complete') != 'complete':
                    raise MCPError("该 MCP 工具需要额外交互或后台任务；第一版仅支持直接返回结果")
                return payload
            rows, cursor, seen = [], None, set()
            for _ in range(10):
                response = await client.list_tools(cursor=cursor)
                page = response.model_dump(mode="json", by_alias=True, exclude_none=True)
                _check_secret_echo(page, headers)
                rows.extend(page.get('tools', []))
                if len(rows) > 200 or len(json.dumps(rows)) > 500_000:
                    raise MCPError("服务工具目录超过上限，请在服务端缩小工具集")
                cursor = page.get('nextCursor')
                if not cursor:
                    names = [row['name'] for row in rows]
                    if len(names) != len(set(names)):
                        raise MCPError("服务返回了重复工具名称，无法安全路由")
                    return rows
                if cursor in seen:
                    raise MCPError("服务工具分页重复，已停止读取")
                seen.add(cursor)
            raise MCPError("服务工具目录超过分页上限")


async def _bounded(server, *, tool=None, arguments=None, deadline=None, cancelled=None):
    expires = min(time.monotonic() + server.timeout_s, deadline or float('inf'))
    if cancelled and cancelled():
        raise MCPError("MCP 调用已取消")
    if time.monotonic() >= expires:
        raise MCPError("MCP 调用超时或研究时限已到")
    task = asyncio.create_task(_exchange(server, tool=tool, arguments=arguments))
    try:
        while True:
            done, _ = await asyncio.wait([task], timeout=min(.05, max(0, expires - time.monotonic())))
            if cancelled and cancelled():
                raise MCPError("MCP 调用已取消；远端已执行的请求可能仍有消耗")
            if time.monotonic() >= expires:
                raise MCPError("MCP 调用超时或研究时限已到")
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
        # SDK transport 的退出要在同一个 task 中完成，避免 anyio cancel scope 跨 task。
        await asyncio.gather(task, return_exceptions=True)


def _run(server, **kwargs):
    try:
        return asyncio.run(_bounded(server, **kwargs))
    except MCPError:
        raise
    except Exception as exc:
        # ExceptionGroup / HTTP 异常可能含 URL、header、响应正文，不原样进入日志。
        pending = [exc]
        while pending:
            item = pending.pop()
            if isinstance(item, MCPError):
                raise item from None
            pending.extend(getattr(item, 'exceptions', []))
            response = getattr(item, 'response', None)
            if response is not None and getattr(response, 'status_code', None):
                raise MCPError(f"MCP HTTP {response.status_code}：请检查地址、凭据和服务权限") from None
        raise MCPError("MCP 连接或协议失败，请检查服务地址、凭据及 Streamable HTTP 支持") from None


def discover(server: MCPServerConfig, *, deadline=None, cancelled=None) -> list[dict]:
    return _run(server, deadline=deadline, cancelled=cancelled)


def call_tool(server: MCPServerConfig, name: str, arguments: dict, *, deadline=None, cancelled=None) -> dict:
    if not server.enabled or name not in {t.name for t in server.tools}:
        raise MCPError("本次研究未允许这个 MCP 工具")
    if not isinstance(arguments, dict) or len(json.dumps(arguments, ensure_ascii=False)) > 32_000:
        raise MCPError("MCP 工具参数须为不超过 32000 字符的对象")
    return _run(server, tool=name, arguments=arguments, deadline=deadline, cancelled=cancelled)


def model_tool_name(server_id: str, tool_name: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9_]', '_', tool_name)[:20]
    digest = hashlib.sha256(tool_name.encode()).hexdigest()[:8]
    return f"mcp_{server_id}_{slug}_{digest}"


def _text_result_size(payload):
    # 图片字节由原件/视觉链路保存，不把 base64 当作文本回执预算。
    return len(json.dumps({**payload, 'content': [{k: v for k, v in block.items() if k != 'data'}
        if block.get('type') == 'image' else block for block in payload.get('content', [])]}, ensure_ascii=False))


def source_parts(payload: dict, *, bounded=True) -> tuple[list[dict], list[str]]:
    if bounded and _text_result_size(payload) > MAX_RESULT_CHARS:
        raise MCPError("MCP 返回超过保存上限")
    parts, warnings = [], []
    for index, block in enumerate(payload.get('content') or []):
        if block.get('type') == 'text' and isinstance(block.get('text'), str):
            parts.append({'text': block['text'], 'block': index, 'format': 'text'})
        elif block.get('type') == 'resource' and isinstance(block.get('resource', {}).get('text'), str):
            resource = block['resource']
            parts.append({'text': resource['text'], 'block': index, 'format': 'resource',
                          'uri': resource.get('uri', '')})
        elif block.get('type') != 'image':
            warnings.append(f"第 {index + 1} 个内容块为 {block.get('type', '未知')}，未转换为可引用文本")
    structured = payload.get('structuredContent')
    if structured is not None:
        # SDK 常把同一份 JSON 同时放在 text 和 structuredContent 中；仅跳过确定重复的表示。
        # text 也可能只是概述，不能因此丢掉结构化字段。
        duplicate = False
        for part in parts:
            if structured == {'result': part['text']}:
                duplicate = True  # SDK 对纯字符串返回的包装，没有额外数据字段。
                break
            try:
                if json.loads(part['text']) == structured:
                    duplicate = True
                    break
            except (ValueError, TypeError):
                pass
        if not duplicate:
            parts.append({'text': json.dumps(structured, ensure_ascii=False, indent=2),
                          'block': len(payload.get('content') or []), 'format': 'structured_json'})
    parts = [p for p in parts if p['text'].strip()]
    if bounded and len(parts) > 20:
        raise MCPError("MCP 文本块数量超过上限，请缩小请求")
    return parts, warnings


def _valid_schema(schema):
    """只支持模型 function calling 能表达的对象参数；禁止校验时访问远程 $ref。"""
    if not isinstance(schema, dict) or schema.get('type') != 'object' or len(json.dumps(schema)) > 16_000:
        raise MCPError("工具参数 schema 不受支持")
    def check(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {'$ref', '$dynamicRef'} and (not isinstance(value, str) or not value.startswith('#')):
                    raise MCPError("工具 schema 包含外部引用")
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)
    check(schema)
    try:
        validator = validators.validator_for(schema)
        validator.check_schema(schema)
        return validator(schema)
    except Exception:
        raise MCPError("工具参数 schema 无效") from None


class MCPRegistry:
    def __init__(self, servers: list[MCPServerConfig]):
        self.servers = [s.model_copy(deep=True) for s in servers]
        self.tools: dict[str, dict] = {}
        self.schemas: list[dict] = []
        self.warnings: list[str] = []
        self.prepared = False

    def prepare(self, *, deadline=None, cancelled=None):
        if self.prepared:
            return
        self.prepared = True  # 每轮只发现一次；子任务共享已冻结的目录。
        for server in sorted(self.servers, key=lambda s: s.id):
            if not server.enabled or not server.tools:
                continue
            try:
                available = {t['name']: t for t in discover(server, deadline=deadline, cancelled=cancelled)}
                for allowed in sorted(server.tools, key=lambda t: t.name):
                    tool = available.get(allowed.name)
                    if tool is None:
                        self.warnings.append(f"{server.name} / {allowed.name}：服务未提供该工具")
                        continue
                    annotations = tool.get('annotations') or {}
                    # 自报只读并非权限保证；显式声称会写入的工具在第一版拒绝。
                    if annotations.get('readOnlyHint') is False:
                        self.warnings.append(f"{server.name} / {allowed.name}：第一版不启用写入工具")
                        continue
                    try:
                        validator = _valid_schema(tool.get('inputSchema'))
                    except MCPError as exc:
                        self.warnings.append(f"{server.name} / {allowed.name}：{exc}")
                        continue
                    if len(self.schemas) >= MAX_TOOLS:
                        raise MCPError("外部工具数量超过上限")
                    name = model_tool_name(server.id, allowed.name)
                    self.tools[name] = {'server': server, 'tool': tool, 'validator': validator}
                    purpose = '返回中已展示的原话可直接 save_evidence；未展示部分用 read_source 继续读取'
                    self.schemas.append({'type': 'function', 'function': {'name': name,
                        'description': f"外部资料工具：{server.name} / {allowed.name}。{purpose}。\n"
                                       + str(tool.get('description') or '')[:1500],
                        'parameters': tool['inputSchema']}})
            except MCPError as exc:
                self.warnings.append(f"{server.name}：{exc}")

    def invoke(self, name, arguments, *, deadline=None, cancelled=None):
        entry = self.tools.get(name)
        if entry is None:
            raise MCPError("本次研究未允许这个 MCP 工具，或连接未成功")
        try:
            entry['validator'].validate(arguments)
        except Exception:
            raise MCPError("MCP 工具参数不符合已发现的 schema，请按工具定义修正") from None
        start = time.monotonic()
        payload = call_tool(entry['server'], entry['tool']['name'], arguments,
                            deadline=deadline, cancelled=cancelled)
        return entry, payload, round((time.monotonic() - start) * 1000)
