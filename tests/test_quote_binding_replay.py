"""Replay reads only actual per-agent receipts; fixtures contain no real run data."""
import hashlib
import json

from scripts.replay_quote_binding import read_cases, replay


def test_replay_preserves_crlf_unicode_and_actor_visibility(tmp_path):
    raw = '标题\r\n材料A\u2028材料B。'
    (tmp_path / 'sources').mkdir()
    (tmp_path / 'sources/s1.txt').write_bytes(raw.encode())
    (tmp_path / 'simple.json').write_text(json.dumps({'sources': [{
        'ref': 's1', 'snapshot_kind': 'full',
        'content_sha256': hashlib.sha256(raw.encode()).hexdigest()}]}))
    rows = []

    def tool(name, actor, args, result):
        rows.append({'phase': 'done', 'record_id': str(len(rows)), 'agent_id': actor,
                     'tool': name, 'status': 'ok', 'arguments': args,
                     'result': json.dumps(result, ensure_ascii=False)})

    quote = raw[4:]
    tool('read_source', 'child', {}, {'source_ref': 's1', 'excerpts': [
        {'quote': quote, 'location': f'chars=4-{len(raw)}'}]})
    for actor, accepted in [('lead', False), ('child', True)]:
        tool('save_evidence', actor, {'cards': [{'quotes': [{'quote': quote, 'source_ref': 's1'}]}]},
             {'results': [{'index': 1, 'accepted': accepted,
                           'quotes': [{'quote': quote, 'source_ref': 's1', 'location': f'chars=4-{len(raw)}'}]
                           if accepted else [], 'binding_status': 'not_found'}]})
    # Non-object tool results aren't source receipts and must not crash replay.
    tool('search_sources', 'lead', {}, ['search summary'])
    (tmp_path / 'tool_calls.jsonl').write_text('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows))
    report = replay(tmp_path)
    assert report['audit'] == {'skipped_sources': {}, 'invalid_receipts': []}
    assert report['cases'][0]['result']['status'] == 'needs_read'
    assert report['cases'][1]['expected_location_retained'] is True
    assert report['exact_location_regressions'] == 0


def test_replay_recognizes_execution_and_observation_receipts(tmp_path):
    (tmp_path / 'sources').mkdir()
    sources, rows = [], []
    for ref, kind, body in [('s1', 'computation', 'rows=120'), ('s2', 'observation', '图片中可见两条曲线')]:
        (tmp_path / f'sources/{ref}.txt').write_text(body)
        sources.append({'ref': ref, 'snapshot_kind': 'full', kind: {'fixture': True},
                        'content_sha256': hashlib.sha256(body.encode()).hexdigest()})
        result = {'source_ref': ref, 'source_kind': kind}
        if kind == 'computation':
            result.update(stdout=body, shown_range=f'chars=0-{len(body)}')
        else:
            result['excerpts'] = [{'quote': body, 'location': f'chars=0-{len(body)}'}]
        rows.append({'record_id': ref, 'phase': 'done', 'agent_id': 'lead', 'status': 'ok',
                     'tool': 'bash' if kind == 'computation' else 'read_image', 'result': result})
    rows.append({'record_id': 'save', 'phase': 'done', 'agent_id': 'lead', 'tool': 'save_evidence',
                 'arguments': {'cards': [{'quotes': [{'quote': 'rows=120'}]}]},
                 'result': {'results': [{'index': 1, 'accepted': False, 'binding_status': 'unknown'}]}})
    (tmp_path / 'simple.json').write_text(json.dumps({'sources': sources}))
    (tmp_path / 'tool_calls.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
    cases, _, markup, audit = read_cases(tmp_path)
    assert cases[0]['exposures'] == {'s1': [(0, 8)], 's2': [(0, 9)]}
    assert not markup and not audit['invalid_receipts']
    # A changed snapshot is explicitly excluded; no stale receipt authorizes it.
    (tmp_path / 'sources/s1.txt').write_text('changed')
    cases, _, _, audit = read_cases(tmp_path)
    assert audit['skipped_sources'] == {'s1': 'snapshot_hash_mismatch'}
    assert 's1' not in cases[0]['exposures']
