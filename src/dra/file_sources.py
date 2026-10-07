"""Shared original retrieval for read_source and execution inputs."""
from __future__ import annotations

import hashlib
import time

from dra import llm, source_download
from dra.execution import ExecutionError
from dra.models import RetrievedDoc, normalize_url


def fetch_original(url, **kwargs):
    """Transport boundary replaceable by frozen evaluators and offline fixtures."""
    return source_download.fetch(url, **kwargs)


def execution_available(ctx):
    return bool(ctx.config.execution.enabled and ((ctx.execution_capability or {}).get('available')
        or ctx.execution is not None))


def original_bytes(store, source):
    metadata = source.document or source.download
    data = (store.sources_dir / f'{source.ref}.original').read_bytes()
    if hashlib.sha256(data).hexdigest() != metadata['original_sha256']:
        raise ExecutionError('source_changed', '来源原件校验失败')
    return data


def acquire(ctx, url):
    if ctx.config.source_mode == 'library':
        raise ExecutionError('network_disabled', '本轮仅允许所选资料')
    try:
        url = source_download.validate_public_url(url)
    except ValueError as exc:
        raise ExecutionError('url_denied', str(exc)) from exc
    if source_download.is_blocked(url, ctx.config.blocked_sites):
        raise ExecutionError('site_blocked', '下载地址在用户设置的网站黑名单里')
    if llm.cancellation_requested() or ctx.budget_expired():
        raise ExecutionError('cancelled', '研究已停止或超过时限')
    source = next((ctx.store.original_source(s['ref']) for s in reversed(ctx.store.state['sources'])
        if s.get('download') and s['download'].get('transport') != 'mcp' and not s.get('origin') and url in (
            s['download']['requested_url'], s['download']['final_url'])), None)
    reused = source is not None
    if source is not None:
        data = original_bytes(ctx.store, source)
    else:
        record = ctx.store.record_action('source_download', {'url': url, 'attempted_at': time.time()})
        ctx._commit_ledger()
        try:
            data, text, metadata = fetch_original(url, deadline=ctx.budget_deadline,
                cancelled=llm.cancellation_requested, blocked_sites=ctx.config.blocked_sites)
            doc = RetrievedDoc(title=metadata['filename'], source_url=metadata['final_url'],
                               raw_content=text or '', snippet='')
            if text is None:
                source = ctx.store.write_original(doc, data, download=metadata)
            else:
                if ctx.config.storage_max_chars is not None and len(text) > ctx.config.storage_max_chars:
                    raise ExecutionError('source_too_large', '原文件超过本轮文本保存上限；未截断')
                source, _ = ctx.store.write_source(doc, original_bytes=data, download=metadata)
            with ctx.store._lock:
                record['payload'].update(ok=True, source_ref=source.ref, **metadata)
        except ExecutionError as exc:
            with ctx.store._lock:
                record['payload'].update(ok=False, error_code=exc.code, error=str(exc))
            raise
        except (OSError, ValueError) as exc:
            raise llm.LLMPersistenceError('原件或来源台账保存失败') from exc
        finally:
            with ctx.store._lock:
                record['payload']['finished_at'] = time.time()
                ctx._commit_ledger()
    if source.quotable:
        reading = ctx._local_reading(source.ref)
        with ctx._lock:
            ctx.readings[source.ref] = reading
            kind = (source.download or {}).get('detected_type')
            keys = (source.ref,) if kind == 'text/html' else (source.ref, url, source.url, normalize_url(url), normalize_url(source.url))
            for key in keys:
                ctx.sessions[key] = source.ref
    return source, data, reused


def receipt(source, ctx=None):
    tools = []
    if ctx and ctx.config.vision.enabled:
        tools.append('view_image')
    if ctx and execution_available(ctx):
        tools.append('bash')
    next_step = ('本轮可用：' + '、'.join(tools) + '。主、子 Agent 均可处理原件，子任务把结果及路径交回主线。') if tools else '本轮没有可用的文件解析/看图工具；原件可下载，需启用相应能力后处理。'
    return {'source_ref': source.ref, 'snapshot_kind': source.snapshot_kind,
            'title': source.title, 'url': source.url, 'download': source.download,
            'document': source.document, 'quotable': source.quotable,
            'available_tools': tools,
            'note': '原件已保存，尚未解析为正文。' + next_step + '下载不等于已读或存证。'}
