"""Capacity-sized tool pages. Storage alone never grants read/quote permission."""
from copy import deepcopy
import json
import re
from uuid import uuid4

from dra.context_budget import assess
from dra.durable import atomic_json

PAGE_TOOL = {"type": "function", "function": {
    "name": "read_tool_result",
    "description": "按实际上下文空间分页回读本 Agent 的完整工具回执。cursor 是回执字符位置；连续读到 next_cursor=null 才覆盖全部。",
    "parameters": {"type": "object", "properties": {
        "result_id": {"type": "string"}, "cursor": {"type": "integer", "minimum": 0}},
        "required": ["result_id"], "additionalProperties": False}}}


def _json(value):
    return json.dumps(value, ensure_ascii=False)


def _path(ctx, result_id):
    return ctx.store.dir / 'context' / (ctx.delegation_id or ctx.tool_role) / (result_id + '.json')


def _merge(ranges):
    merged = []
    for lo, hi in sorted(ranges):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def _serialize(payload, allowed):
    """Serialize JSON with exact spans of verbatim nodes, including inherited source IDs."""
    pieces, spans = [], []
    offset = 0

    def emit(text):
        nonlocal offset
        pieces.append(text)
        offset += len(text)

    def walk(value, ref=None):
        start = offset
        if isinstance(value, dict):
            ref = value.get('source_ref', ref)
            emit('{')
            for index, (key, child) in enumerate(value.items()):
                if index:
                    emit(', ')
                emit(_json(key) + ': ')
                walk(child, ref)
            emit('}')
            match = re.fullmatch(r'chars=(\d+)-(\d+)', str(value.get('location', '')))
            if ref and match and isinstance(value.get('quote', value.get('text')), str):
                lo, hi = map(int, match.groups())
                if any(a <= lo and hi <= b for a, b in allowed.get(ref, [])):
                    spans.append({'start': start, 'end': offset, 'source_ref': ref, 'lo': lo, 'hi': hi})
        elif isinstance(value, list):
            emit('[')
            for index, child in enumerate(value):
                if index:
                    emit(', ')
                walk(child, ref)
            emit(']')
        else:
            emit(_json(value))
    walk(payload)
    return ''.join(pieces), spans


def fits_receipt(ctx, text):
    if not ctx.config.compaction_enabled:
        return True
    pending = getattr(ctx, 'tool_page_request', None)
    if pending is None:
        messages, schemas, call_id = [], [], 'page'
    else:
        messages, schemas, call_id = pending
    return assess(ctx, [*messages, {'role': 'tool', 'tool_call_id': call_id, 'content': text}], schemas)['fits']


def _page(ctx, result_id, data, cursor):
    raw = data['text']
    seen = ctx.tool_result_pages.get(result_id, [])

    def envelope(end):
        covered = _merge([*seen, (cursor, end)])
        visible = [s for s in data['spans'] if s['start'] < end and cursor < s['end']
                   and any(a <= s['start'] and s['end'] <= b for a, b in covered)]
        return {'result_id': result_id, 'tool': data['tool'], 'fragment': True,
                'format': data['format'], 'total_chars': len(raw), 'shown_range': [cursor, end],
                'text': raw[cursor:end], 'next_cursor': end if end < len(raw) else None,
                'read_back': {'tool': 'read_tool_result', 'result_id': result_id},
                'status': data['status'],
                'visible_ranges': [{'source_ref': s['source_ref'], 'location': f"chars={s['lo']}-{s['hi']}"} for s in visible],
                'note': '完整回执已保存；这是连续片段，未展示部分尚未读到。分片 JSON 需要连续回读，不能当完整 JSON 解析。'}

    lo, hi = cursor, len(raw)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits_receipt(ctx, _json(envelope(mid))):
            lo = mid
        else:
            hi = mid - 1
    page = envelope(lo)
    if lo == cursor and cursor < len(raw):
        # No source text has been exposed. This small handle lets the next boundary
        # reclaim history before a retry; the complete original remains on disk.
        return _json({'result_id': result_id, 'tool': data['tool'], 'status': data['status'],
                      'shown_range': [cursor, cursor], 'next_cursor': cursor,
                      'read_back': page['read_back'], 'note': '当前空间不足，尚未展示正文；整理上下文后回读。'})
    covered = _merge([*seen, (cursor, lo)])
    ctx.tool_result_pages[result_id] = covered
    for span in data['spans']:
        if any(a <= span['start'] and span['end'] <= b for a, b in covered):
            ref = span['source_ref']
            ctx.read_exposures[ref] = _merge([*ctx.read_exposures.get(ref, []), (span['lo'], span['hi'])])
    ctx.store.record_action('context_tool_page', {'result_id': result_id, 'agent_id': ctx.delegation_id or ctx.tool_role,
        'shown_range': page['shown_range'], 'next_cursor': page['next_cursor'], 'visible_ranges': page['visible_ranges']})
    return _json(page)


def deliver(ctx, tool, result, before_exposures):
    if tool == 'read_tool_result' or not ctx.config.compaction_enabled or fits_receipt(ctx, result):
        return result
    result_id = 'receipt-' + uuid4().hex
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        payload = None
    if payload is None:
        text, spans, fmt = result, [], 'text'
    else:
        text, spans = _serialize(payload, ctx.read_exposures)
        fmt = 'json'
    rows = payload.get('results', []) if isinstance(payload, dict) else []
    status = {'accepted': sum(bool(r.get('accepted')) for r in rows),
              'rejected': sum(not r.get('accepted', False) for r in rows)} if rows else {}
    if isinstance(payload, dict):
        status.update({key: payload[key] for key in ('saved_total', 'pending_selection') if key in payload})
    data = {'tool': tool, 'format': fmt, 'original': result, 'text': text, 'spans': spans, 'status': status}
    atomic_json(_path(ctx, result_id), data)
    ctx.tool_result_pages[result_id] = []
    ctx.read_exposures = deepcopy(before_exposures)
    # The saved receipt has a read-back handle. Avoid reinserting its full card bodies
    # as "new cards" during the very next context assembly.
    if ctx.message_view is not None:
        ctx.message_view.card_ids.update(r['card_id'] for r in rows if r.get('accepted') and r.get('card_id'))
    return _page(ctx, result_id, data, 0)


def read_page(ctx, args):
    result_id, cursor = args.get('result_id'), args.get('cursor', 0)
    if result_id not in ctx.tool_result_pages or type(cursor) is not int or cursor < 0:
        return '无权读取该工具回执，或 cursor 无效。', False
    data = json.loads(_path(ctx, result_id).read_text())
    if cursor > len(data['text']):
        return 'cursor 超出工具回执范围。', False
    return _page(ctx, result_id, data, cursor), True
