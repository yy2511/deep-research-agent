"""Download public text data without a page extractor or newline conversion.

The controller owns retrieval and provenance; generated workspace files cannot
register themselves as external evidence. No credentials or cookies from the
controller environment are sent to a source website.
"""
from __future__ import annotations

import codecs
import hashlib
import http.client
import ipaddress
import re
import socket
import time
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import getproxies

from dra.execution import ExecutionError
from dra.site_filter import is_blocked
from dra.tools import validate_public_url

TIMEOUT_S = 45


def _connection(url, timeout):
    """Resolve once and pin the connection to a public address, retaining TLS SNI."""
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    ips = [ipaddress.ip_address(row[4][0]) for row in addresses]
    # Some local VPNs return 198.18/15 fake DNS answers. Route those through
    # the user's existing loopback proxy, which resolves the public hostname;
    # never connect to the fake address or relax private-IP checks generally.
    if ips and all(ip in ipaddress.ip_network('198.18.0.0/15') for ip in ips):
        proxy = urlsplit(getproxies().get(parts.scheme, ''))
        try:
            local = ipaddress.ip_address(proxy.hostname or '').is_loopback
        except ValueError:
            local = proxy.hostname == 'localhost'
        if proxy.scheme == 'http' and local and not proxy.username and not proxy.password:
            cls = http.client.HTTPSConnection if parts.scheme == 'https' else http.client.HTTPConnection
            conn = cls(proxy.hostname, proxy.port or 80, timeout=timeout)
            conn.set_tunnel(parts.hostname, port)
            return conn
        raise ExecutionError('url_denied', '本机使用虚拟 DNS，但没有可用的本机 HTTP 代理')
    if not ips or any(not ip.is_global for ip in ips):
        raise ExecutionError('url_denied', '下载地址解析到非公网 IP')
    address = addresses[0][4][0]
    cls = http.client.HTTPSConnection if parts.scheme == 'https' else http.client.HTTPConnection
    conn = cls(parts.hostname, port, timeout=timeout)
    # HTTPSConnection still verifies the certificate against the original host.
    conn._create_connection = lambda _address, timeout, source_address=None: socket.create_connection(
        (address, port), timeout, source_address)
    return conn


def _decode(data, content_type, filename):
    kind = content_type.split(';', 1)[0].strip().lower()
    suffix = PurePosixPath(filename).suffix.lower()
    textual = (kind.startswith('text/') or kind in ('application/json', 'application/xml', 'application/csv')
               or kind.endswith(('+json', '+xml')))
    if not textual and suffix not in ('.csv', '.tsv', '.txt', '.json', '.xml', '.md'):
        raise ExecutionError('unsupported_file_type', '目前原件存证支持 CSV、TSV 和文本数据；二进制文件请使用其他读取工具')
    charset = re.search(r'charset\s*=\s*["\']?([\w.-]+)', content_type, re.I)
    encoding = charset.group(1) if charset else 'utf-8'
    if data.startswith(codecs.BOM_UTF8):
        encoding = 'utf-8-sig'
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = 'utf-16'
    try:
        text = data.decode(encoding, errors='strict')
    except (UnicodeError, LookupError) as exc:
        raise ExecutionError('decode_failed', '原件不能按声明的编码解码；未用替换字符或猜测内容登记来源') from exc
    if not text.strip() or '\x00' in text:
        raise ExecutionError('invalid_text', '下载结果为空或不是可读取的文本数据')
    if suffix in ('.csv', '.tsv') and (kind in ('text/html', 'application/xhtml+xml')
            or re.match(r'\s*<(?:!doctype\s+html|html|head|body)\b', text, re.I)):
        raise ExecutionError('unexpected_html', '数据文件链接返回了 HTML 网页，可能是登录页或访问拦截页；不能当作 CSV')
    return text, encoding


def fetch(url, *, deadline=None, cancelled=lambda: False, blocked_sites=()):
    """Direct GET with a deadline, preserving the complete original file."""
    end = min(deadline if deadline is not None else float('inf'), time.monotonic() + TIMEOUT_S)
    original_url = url
    redirects = []

    def remaining():
        if cancelled():
            raise ExecutionError('cancelled', '下载已取消')
        left = end - time.monotonic()
        if left <= 0:
            raise ExecutionError('download_timeout', '下载超过本轮剩余时间或单次时限')
        return min(10, left)

    for hop in range(5):
        remaining()
        try:
            url = validate_public_url(url)
        except ValueError as exc:
            raise ExecutionError('url_denied', str(exc)) from exc
        if is_blocked(url, blocked_sites):
            raise ExecutionError('site_blocked', '下载地址在用户设置的网站黑名单里')
        conn = None
        try:
            conn = _connection(url, remaining())
            parts = urlsplit(url)
            target = parts.path or '/'
            if parts.query:
                target += '?' + parts.query
            conn.request('GET', target, headers={'User-Agent': 'ResearchWorkbench/1.0', 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            status = response.status
            if status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if not location or hop == 4:
                    raise ExecutionError('redirect_limit', '下载重定向缺少目标或次数过多')
                redirects.append({'url': url, 'status': status})
                url = urljoin(url, location)
                continue
            if status != 200:
                raise ExecutionError('download_http_error', f'原文件下载 HTTP {status}')
            encoding = response.getheader('Content-Encoding', 'identity').lower()
            if encoding not in ('', 'identity'):
                raise ExecutionError('unsupported_encoding', '下载站点忽略 identity 请求，返回压缩传输；未冒充原始文件')
            length = response.getheader('Content-Length')
            body = bytearray()
            while True:
                timeout = remaining()
                if conn.sock is not None:
                    conn.sock.settimeout(timeout)
                chunk = response.read1(65536)
                if not chunk:
                    break
                body.extend(chunk)
            remaining()
            if length and len(body) != int(length):
                raise ExecutionError('incomplete_download', '原文件下载不完整，未登记为来源')
            data = bytes(body)
            filename = PurePosixPath(unquote(parts.path)).name or 'source.txt'
            # Never use a server-supplied filename as a host path.
            filename = re.sub(r'[^\w.\-]', '_', filename)[-180:] or 'source.txt'
            content_type = response.getheader('Content-Type', 'application/octet-stream')
            text, text_encoding = _decode(data, content_type, filename)
            return data, text, {
                'requested_url': original_url, 'final_url': url, 'redirects': redirects,
                'filename': filename, 'content_type': content_type, 'encoding': text_encoding,
                'original_sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
                'fetched_at': time.time(), 'etag': response.getheader('ETag'),
                'last_modified': response.getheader('Last-Modified'),
            }
        except ExecutionError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise ExecutionError('download_failed', f'原文件下载失败（{type(exc).__name__}），未登记为来源') from exc
        finally:
            if conn is not None:
                conn.close()
