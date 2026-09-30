"""Resolve quotes against server-owned, actor-specific read exposures.

sources maps immutable snapshot refs to raw text. exposures MUST be supplied by
the server for one actor, never by the model. Coordinates are Python characters,
end-exclusive, matching the existing snapshot contract.
"""


def _normalize(raw):
    text, positions = [], []
    for index, char in enumerate(raw):
        if char.isspace():
            if text and text[-1] == ' ':
                positions[-1] = (positions[-1][0], index + 1)
                continue
            char = ' '
        text.append(char)
        positions.append((index, index + 1))
    return ''.join(text), positions


def _ranges(ranges, length):
    merged = []
    for start, end in sorted(ranges):
        start, end = max(0, start), min(length, end)
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def bind_quote(quote, sources, exposures, *, selection=None):
    # selection 只能由服务端候选表提供。候选不授予阅读权限，也不能替代原话核验。
    if selection is not None:
        ref = selection['source_ref']
        raw = sources.get(ref)
        lo, hi = selection['start'], selection['end']
        if (raw is None or not 0 <= lo < hi <= len(raw)
                or raw[lo:hi] != selection['quote']
                or not any(a <= lo and hi <= b for a, b in _ranges(exposures.get(ref, []), len(raw)))):
            return {'status': 'invalid_selection', 'candidates': []}
        match = bind_quote(quote, {ref: raw}, {ref: [(lo, hi)]})
        if (match['status'] == 'bound' and match['candidates'][0]['start'] == lo
                and match['candidates'][0]['end'] == hi):
            return match
        return {'status': 'invalid_selection', 'candidates': []}
    needle = _normalize(quote)[0].strip()
    if not needle:
        return {'status': 'invalid', 'candidates': []}
    candidates = []
    total = 0
    for ref, raw in sources.items():
        for lo, hi in _ranges(exposures.get(ref, []), len(raw)):
            normalized, positions = _normalize(raw[lo:hi])
            cursor = 0
            while (found := normalized.find(needle, cursor)) != -1:
                start = lo + positions[found][0]
                end = lo + positions[found + len(needle) - 1][1]
                total += 1
                if len(candidates) < 10:
                    candidates.append({'source_ref': ref, 'start': start, 'end': end,
                                       'quote': raw[start:end]})
                cursor = found + 1
    status = 'not_found' if total == 0 else 'bound' if total == 1 else 'ambiguous'
    result = {'status': status, 'candidates': candidates}
    if total > 1:
        result['total_matches'] = total
    return result
