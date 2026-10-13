"""Locate copied quotes in immutable, actor-visible source ranges.

Matching corrects the stored quotation, never the claim. Coordinates are Python
characters, end-exclusive. Approximate matches retain the real snapshot slice.
"""
from difflib import SequenceMatcher
from html import unescape
import math
import re

import regex


# Alignment tolerances, not document/quote length limits. The offline replay
# report records their effect. The wider band only supplies selectable originals.
FUZZY_SEARCH_ERROR_RATE = 0.20
FUZZY_AUTO_ERROR_RATE = 0.02
MAX_CANDIDATES = 10  # Existing response limit; total_matches stays unabridged.
_QUOTES = str.maketrans({'‘': "'", '’': "'", '“': '"', '”': '"'})
_LINK = regex.compile(r'!?\[(?P<label>[^\]\n]+)\](?P<dest>\((?:[^()\\]|\\.|(?&dest))*\))')
_INLINE = re.compile(r'(?P<mark>\*\*|__|`)(?P<body>[^\n]+?)(?P=mark)')
_ENTITY = re.compile(r'&(?:#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]+);')


def _normalize(raw):
    return _collapse(raw, [(i, i + 1) for i in range(len(raw))])


def _collapse(text, positions):
    result, mapped = [], []
    for char, (lo, hi) in zip(text, positions):
        if char.isspace():
            if result and result[-1] == ' ':
                mapped[-1] = (mapped[-1][0], hi)
                continue
            char = ' '
        result.append(char)
        mapped.append((lo, hi))
    return ''.join(result), mapped


def _unwrap(text, positions, pattern, group):
    result, mapped, cursor = [], [], 0
    for match in pattern.finditer(text):
        result.append(text[cursor:match.start()])
        mapped.extend(positions[cursor:match.start()])
        lo, hi = match.span(group)
        result.append(text[lo:hi])
        inner = list(positions[lo:hi])
        # Keep the markup in the raw slice if its whole visible label is matched.
        inner[0] = (positions[match.start()][0], inner[0][1])
        inner[-1] = (inner[-1][0], positions[match.end() - 1][1])
        mapped.extend(inner)
        cursor = match.end()
    result.append(text[cursor:])
    mapped.extend(positions[cursor:])
    return ''.join(result), mapped


def visible_text(raw, *, markup=False):
    """Visible spelling plus raw coordinates; no punctuation folding here."""
    text, positions = raw, [(i, i + 1) for i in range(len(raw))]
    if markup:
        text, positions = _unwrap(text, positions, _LINK, 'label')
        text, positions = _unwrap(text, positions, _INLINE, 'body')
        result, mapped, cursor = [], [], 0
        for match in _ENTITY.finditer(text):
            result.append(text[cursor:match.start()])
            mapped.extend(positions[cursor:match.start()])
            decoded = unescape(match[0])
            result.append(decoded)
            mapped.extend([(positions[match.start()][0], positions[match.end() - 1][1])] * len(decoded))
            cursor = match.end()
        result.append(text[cursor:])
        mapped.extend(positions[cursor:])
        text, positions = ''.join(result), mapped
    return text, positions


def _format_view(raw, *, markup=False):
    text, positions = visible_text(raw, markup=markup)
    result, mapped = [], []
    for char, span in zip(text, positions):
        # NFKC also changes e.g. m² to m2; only fold full-width ASCII here.
        if '\uff01' <= char <= '\uff5e':
            char = chr(ord(char) - 0xfee0)
        char = char.translate(_QUOTES)
        if char in ('\u200b', '\ufeff', '\u00ad'):
            continue
        result.append(char)
        mapped.append(span)
    return _collapse(''.join(result), mapped)


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


def _changes(submitted, original, start):
    return [{'kind': kind, 'submitted': submitted[a:b], 'original': original[c:d],
             'submitted_start': a, 'submitted_end': b,
             'source_start': start + c, 'source_end': start + d}
            for kind, a, b, c, d in SequenceMatcher(None, submitted, original, autojunk=False).get_opcodes()
            if kind != 'equal']


def _candidate(ref, raw, lo, positions, start, end, submitted, method):
    a, b = lo + positions[start][0], lo + positions[end - 1][1]
    candidate = {'source_ref': ref, 'start': a, 'end': b, 'quote': raw[a:b]}
    if method != 'exact':
        candidate['binding'] = {'method': method, 'confirmed': False,
                                'changes': _changes(submitted, raw[a:b], a)}
    return candidate


def _exact_matches(needle, text):
    cursor = 0
    while (start := text.find(needle, cursor)) != -1:
        yield start, start + len(needle)
        cursor = start + 1


def _edit_columns(pattern, text, *, anchored):
    """Myers bit-parallel edit distance, one Python big integer per column.

    Yields (characters consumed, distance of the whole pattern). Unanchored, the
    match may start anywhere in text (distance to the best substring ending
    there); anchored, it must start at text[0] (distance to that prefix). Cost
    is len(text) x len(pattern)/word, so long quotes stay fast; no length cap.
    """
    m = len(pattern)
    peq = {}
    for i, char in enumerate(pattern):
        peq[char] = peq.get(char, 0) | (1 << i)
    mask, top = (1 << m) - 1, 1 << (m - 1)
    pv, mv, score = mask, 0, m
    for j, char in enumerate(text, 1):
        eq = peq.get(char, 0)
        xv = eq | mv
        xh = (((eq & pv) + pv) ^ pv) | eq
        ph = mv | (~(xh | pv) & mask)
        mh = pv & xh
        if ph & top:
            score += 1
        elif mh & top:
            score -= 1
        ph = ((ph << 1) | anchored) & mask
        mh = (mh << 1) & mask
        pv = mh | (~(xv | ph) & mask)
        mv = ph & xv
        yield j, score


def _fuzzy_matches(needle, text, errors):
    # Exact Levenshtein search over the whole query: every end position within
    # the error budget, then the best start for each locally best end. Several
    # local minima are kept because overlapping occurrences can be distinct
    # evidence locations. Unicode offsets stay in characters, never UTF-8 bytes.
    m, matches = len(needle), {}
    runs, run = [], []
    for end, distance in _edit_columns(needle, text, anchored=False):
        if distance <= errors:
            if run and run[-1][0] != end - 1:
                runs.append(run)
                run = []
            run.append((end, distance))
    if run:
        runs.append(run)
    reversed_needle = needle[::-1]
    for run in runs:
        plateaus = []
        for end, distance in run:
            if plateaus and plateaus[-1][0] == distance:
                plateaus[-1][1].append(end)
            else:
                plateaus.append((distance, [end]))
        for i, (distance, ends) in enumerate(plateaus):
            if (i and plateaus[i - 1][0] <= distance) or (i + 1 < len(plateaus) and plateaus[i + 1][0] <= distance):
                continue
            for end in ends:
                lo = max(0, end - m - errors)
                # Walk back from the end; the best start has the fewest edits.
                # On a tie keep the longer slice: an edge character counts as
                # replaced, not as dropped from the copy (which blocks auto-binding).
                size, best = min(((size, d) for size, d in
                                  _edit_columns(reversed_needle, text[lo:end][::-1], anchored=True)),
                                 key=lambda item: (item[1], -item[0]))
                if best <= errors:
                    span = (end - size, end)
                    matches[span] = min(matches.get(span, best), best)
    # Drop worse endpoint variants of the same occurrence, not distinct
    # overlapping occurrences. A shifted endpoint is not another source passage.
    # With equal edits the enclosing slice wins, as with the start above.
    kept = []
    for (lo, hi), distance in sorted(matches.items(), key=lambda item: (
            item[1], item[0][0] - item[0][1], item[0])):
        if any((a <= lo and hi <= b or lo <= a and b <= hi) and
               (d < distance or b - a > hi - lo)
               for a, b, d in kept):
            continue
        kept.append((lo, hi, distance))
    return kept


def _result(candidates, *, status=None):
    total = len(candidates)
    result = {'status': status or ('not_found' if not total else 'bound' if total == 1 else 'ambiguous'),
              'candidates': candidates[:MAX_CANDIDATES]}
    if total > 1:
        result['total_matches'] = total
    return result


def bind_quote(quote, sources, exposures, *, selection=None, markup_sources=()):
    if selection is not None:
        ref = selection['source_ref']
        raw = sources.get(ref)
        lo, hi = selection['start'], selection['end']
        if (raw is None or not 0 <= lo < hi <= len(raw)
                or raw[lo:hi] != selection['quote']
                or not any(a <= lo and hi <= b for a, b in _ranges(exposures.get(ref, []), len(raw)))):
            return _result([], status='invalid_selection')
        # Keep old quote+ID calls, but permit ID-only selection. An explicitly
        # different quote must not silently refer to another passage.
        if quote is not None and _normalize(quote)[0].strip() not in {
                _normalize(selection.get('submitted_quote', selection['quote']))[0].strip(),
                _normalize(selection['quote'])[0].strip()}:
            return _result([], status='invalid_selection')
        candidate = {k: v for k, v in selection.items() if k != 'submitted_quote'}
        if candidate.get('binding'):
            candidate['binding'] = {**candidate['binding'], 'confirmed': True}
        return _result([candidate])

    needle = _normalize(quote)[0].strip()
    if not needle:
        return _result([], status='invalid')
    windows = [(ref, raw, lo, hi) for ref, raw in sources.items()
               for lo, hi in _ranges(exposures.get(ref, []), len(raw))]
    formatted = {mode: _format_view(quote, markup=mode)[0].strip() for mode in (False, True)}
    for method in ('exact', 'format'):
        candidates, views = [], []
        for ref, raw, lo, hi in windows:
            wanted = needle if method == 'exact' else formatted[ref in markup_sources]
            if not wanted:
                continue
            text, positions = (_normalize(raw[lo:hi]) if method == 'exact' else
                               _format_view(raw[lo:hi], markup=ref in markup_sources))
            views.append((ref, raw, lo, hi, text, positions, wanted))
            for a, b in _exact_matches(wanted, text):
                candidates.append(_candidate(ref, raw, lo, positions, a, b, quote, method))
        if candidates:
            return _result(candidates)
    # Search the complete scope for all small-copy-error alternatives first.
    # Only widen when there is none; broad matches always need selection. This
    # avoids expensive large-distance alignment for an otherwise obvious typo.
    for error_rate in (FUZZY_AUTO_ERROR_RATE, FUZZY_SEARCH_ERROR_RATE):
        found = []
        for view in views:
            ref, raw, lo, hi, text, positions, wanted = view
            errors = math.floor(len(wanted) * error_rate)
            for a, b, distance in _fuzzy_matches(wanted, text, errors):
                found.append((distance, ref, lo + positions[a][0], a, b, view))
        if found:
            break
    if not found:
        return _result([])
    found.sort(key=lambda item: item[:3])

    def at_unread_edge(item):
        _, _, _, a, b, (_, raw, lo, hi, text, _, _) = item
        return (a == 0 and lo > 0) or (b == len(text) and hi < len(raw))

    def describe(item):
        distance, ref, _, a, b, (_, raw, lo, hi, text, positions, wanted) = item
        c = _candidate(ref, raw, lo, positions, a, b, quote, 'fuzzy')
        ops = SequenceMatcher(None, wanted, text[a:b], autojunk=False).get_opcodes()
        missing_edge = any(kind == 'delete' and (x == 0 or y == len(wanted))
                           for kind, x, y, _, _ in ops)
        c['binding'].update(edit_distance=distance, query_chars=len(wanted),
                            missing_edge=missing_edge, needs_read=missing_edge and at_unread_edge(item))
        return c

    # Repetitive tables can yield hundreds of matches. Only returned candidates
    # need a character diff; needs_read can hold only when every match touches
    # an unread edge, so those are all described. total_matches stays complete.
    every = all(at_unread_edge(item) for item in found)
    candidates = [describe(item) if every or i < MAX_CANDIDATES else {'binding': {'needs_read': False}}
                  for i, item in enumerate(found)]
    best = candidates[0]['binding']
    if all(c['binding']['needs_read'] for c in candidates):
        return _result(candidates, status='needs_read')
    automatic = (len(candidates) == 1 and best['edit_distance'] <= math.floor(best['query_chars'] * FUZZY_AUTO_ERROR_RATE)
                 and not best['missing_edge'])
    return {**_result(candidates, status='bound' if automatic else 'needs_selection'),
            'search_error_rate': error_rate}
