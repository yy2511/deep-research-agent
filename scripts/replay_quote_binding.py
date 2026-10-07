"""Replay quote location offline against complete snapshots and actor read ranges.

This does not edit runs or invoke a model. Old accepted locations are regression
references; new matches need inspection and are not automatically gold labels.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time

from dra.quote_binding import bind_quote, _ranges, FUZZY_SEARCH_ERROR_RATE, FUZZY_AUTO_ERROR_RATE


def _json(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def read_cases(directory):
    directory = Path(directory)
    state = json.loads((directory / 'simple.json').read_text())
    metadata = {s['ref']: s for s in state['sources']}
    sources, skipped_sources = {}, {}
    for ref, source in metadata.items():
        if source.get('snapshot_kind') != 'full':
            continue
        path = directory / 'sources' / f'{ref}.txt'
        if not path.exists():
            skipped_sources[ref] = 'missing_snapshot'
            continue
        # Preserve actual CRLF, as the source hash and offsets do.
        raw = path.read_bytes().decode('utf-8')
        if hashlib.sha256(raw.encode()).hexdigest() != source['content_sha256']:
            skipped_sources[ref] = 'snapshot_hash_mismatch'
            continue
        sources[ref] = raw
    markup = {ref for ref in sources if not any(metadata[ref].get(k)
              for k in ('document', 'download', 'computation', 'mcp', 'observation'))}
    exposures = defaultdict(dict)
    cases, invalid_receipts, seen = [], [], set()

    def expose(actor, ref, quote, location):
        m = re.fullmatch(r'chars=(\d+)-(\d+)', location or '')
        if ref not in sources or not m:
            return
        a, b = map(int, m.groups())
        if sources[ref][a:b] != quote:
            invalid_receipts.append({'agent': actor, 'source_ref': ref, 'location': location})
            return
        exposures[actor][ref] = _ranges([*exposures[actor].get(ref, []), (a, b)], len(sources[ref]))

    # Only actual newline separates JSONL records; page text may contain U+2028.
    for number, line in enumerate((directory / 'tool_calls.jsonl').read_text().split('\n'), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get('phase') != 'done' or row.get('record_id') in seen:
            continue
        seen.add(row.get('record_id'))
        actor = row['agent_id']
        result = _json(row.get('result'))
        if not result:
            continue
        args = row.get('arguments', {})
        if row['tool'] == 'save_evidence':
            old_rows = {r['index']: r for r in result.get('results', [])}
            for index, item in enumerate(args.get('cards', []), 1):
                if not isinstance(item, dict):
                    continue
                old = old_rows.get(index, {})
                old_quotes = {q['quote_index']: q for q in old.get('quote_results', [])}
                pieces = item.get('quotes', [item])
                if not isinstance(pieces, list):
                    continue
                for qi, piece in enumerate(pieces, 1):
                    if not isinstance(piece, dict) or not isinstance(piece.get('quote'), str) or piece.get('candidate_id'):
                        continue
                    prior = old_quotes.get(qi, old)
                    saved = old.get('quotes', [])
                    expected = saved[qi-1] if old.get('accepted') and len(saved) >= qi else prior
                    cases.append({'run': directory.name, 'line': number, 'agent': actor,
                        'card_index': index, 'quote_index': qi, 'quote': piece['quote'],
                        'source_ref': piece.get('source_ref'),
                        'old_status': 'bound' if old.get('accepted') else prior.get('binding_status', 'unknown'),
                        'expected_ref': expected.get('source_ref'), 'expected_location': expected.get('location'),
                        'exposures': deepcopy(exposures[actor])})
            # Context becomes visible only after this entire save call returns.
            for old in old_rows.values():
                if not old.get('accepted'):
                    continue
                for quote in old.get('quotes', []):
                    expose(actor, quote['source_ref'], quote['quote'], quote['location'])
                    for context in quote.get('context', []):
                        expose(actor, quote['source_ref'], context['text'], context['location'])
        elif row.get('status') == 'ok':
            views = ([result] if row['tool'] == 'read_source' or result.get('source_kind') == 'observation'
                     else result.get('sources', []))
            for view in views:
                if not isinstance(view, dict) or view.get('archived'):
                    continue
                for excerpt in view.get('excerpts', []):
                    expose(actor, view.get('source_ref'), excerpt.get('quote'), excerpt.get('location'))
            # Bash and auxiliary vision explicitly return their displayed text.
            if row['tool'] == 'bash' and result.get('source_kind') == 'computation' and result.get('shown_range'):
                a, b = map(int, re.fullmatch(r'chars=(\d+)-(\d+)', result['shown_range']).groups())
                expose(actor, result['source_ref'], result['stdout'][:b-a], result['shown_range'])
    return cases, sources, markup, {'skipped_sources': skipped_sources, 'invalid_receipts': invalid_receipts}


def replay(directory):
    cases, sources, markup, audit = read_cases(directory)
    rows, transitions = [], Counter()
    for case in cases:
        print(f"Replaying {Path(directory).name}:{case['line']} card={case['card_index']} quote={case['quote_index']}", flush=True)
        scope = {ref: raw for ref, raw in sources.items()
                 if not case['source_ref'] or ref == case['source_ref']}
        t = time.perf_counter()
        result = bind_quote(case['quote'], scope, case['exposures'], markup_sources=markup)
        if case['source_ref'] and result['status'] == 'not_found':
            others = {ref: raw for ref, raw in sources.items() if ref != case['source_ref']}
            alternative = bind_quote(case['quote'], others, case['exposures'], markup_sources=markup)
            if alternative['candidates'] and alternative['status'] != 'needs_read':
                result = {**alternative, 'status': 'source_mismatch'}
            elif not case['exposures'].get(case['source_ref']):
                result['status'] = 'needs_read'
        elapsed = time.perf_counter()-t
        regression = None
        if case['old_status'] == 'bound' and case['expected_ref'] and case['expected_location']:
            regression = result['status'] == 'bound' and any(
                c['source_ref'] == case['expected_ref'] and f"chars={c['start']}-{c['end']}" == case['expected_location']
                for c in result['candidates'])
        method = result['candidates'][0].get('binding', {}).get('method', 'exact') if result['candidates'] else None
        transitions[f"{case['old_status']} -> {result['status']} ({method or '-'})"] += 1
        rows.append({**{k:v for k,v in case.items() if k != 'exposures'},
                     'read_ranges': case['exposures'], 'result': result,
                     'expected_location_retained': regression, 'elapsed_ms': round(elapsed*1000, 3)})
    return {'run': str(Path(directory).resolve()), 'case_count': len(rows), 'transitions': dict(transitions),
            'exact_location_regressions': sum(r['expected_location_retained'] is False for r in rows),
            'audit': audit, 'cases': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'search_error_rate': FUZZY_SEARCH_ERROR_RATE, 'auto_error_rate': FUZZY_AUTO_ERROR_RATE,
              'note': 'Offline replay, no model calls. New location candidates are not automatic correctness labels.',
              'runs': [replay(p) for p in args.runs]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps([{k:v for k,v in r.items() if k != 'cases'} for r in result['runs']], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
