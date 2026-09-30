"""网站黑名单：用户指定研究时不使用哪些网站。

规则写法：``example.com`` 屏蔽该域名及其所有子域名；``example.com/path`` 只屏蔽这个
路径及其下级页面。协议、``www.``、查询参数和结尾斜杠都会被忽略，大小写不敏感，
路径里的 ``%3A`` 这类转义按原字符比较。

生效位置（见 ``simple_research.ToolContext``）：
1. 检索时把纯域名规则交给 Tavily / Exa 的排除参数，少浪费结果名额；
2. 所有检索结果（含兜底搜索和测试替身）再按完整规则过滤一遍；
3. 按 URL 读正文前检查一次，读回后按最终地址（可能被跳转）再查一次。
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

MAX_BLOCKED_SITES = 50
_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def _split(text: str) -> tuple[str, str]:
    """把网址或规则拆成（主机名，路径），都转小写，去掉 www. 和结尾斜杠。"""
    text = text.strip().lower()
    if "://" not in text:
        text = "//" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").removeprefix("*.").removeprefix("www.")
    return host, unquote(parts.path).rstrip("/")


def normalize_blocked_sites(rules: list[str]) -> list[str]:
    """校验并规整用户填写的规则，去重保序；写法不对直接报错，不静默丢弃。"""
    cleaned: list[str] = []
    for rule in rules:
        if not rule.strip():
            continue
        host, path = _split(rule)
        if not _HOST.match(host):
            raise ValueError(f"网站黑名单里的「{rule.strip()}」不是有效的网址或域名")
        item = host + path
        if item not in cleaned:
            cleaned.append(item)
    if len(cleaned) > MAX_BLOCKED_SITES:
        raise ValueError(f"网站黑名单最多 {MAX_BLOCKED_SITES} 条")
    return cleaned


def is_blocked(url: str | None, rules: list[str]) -> bool:
    """该网址是否命中黑名单。没有网址的结果（本地资料）不受影响。"""
    if not url or not rules:
        return False
    host, path = _split(url)
    for rule in rules:
        rule_host, _, rule_path = rule.partition("/")
        if host != rule_host and not host.endswith("." + rule_host):
            continue
        if not rule_path or path == "/" + rule_path or path.startswith("/" + rule_path + "/"):
            return True
    return False


def provider_exclude_domains(rules: list[str]) -> list[str]:
    """可以直接交给搜索服务排除的纯域名规则；带路径的规则只能靠结果过滤。"""
    return [rule for rule in rules if "/" not in rule]
