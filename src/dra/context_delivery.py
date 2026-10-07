"""Reuse identical original ranges in delivery without dropping surrounding text."""
from dra.evidence_judge import evidence_state


def delivery_evidence(cards):
    seen = {}
    results = []

    def reuse(row, field, ref, anchor):
        text = row.get(field)
        location = row.get('location')
        if text is None or not location:
            return
        key = (ref, location, text)
        if key in seen:
            row.pop(field)
            row[field + '_ref'] = seen[key]
        else:
            row['evidence_id'] = anchor
            seen[key] = anchor

    for card in cards:
        rows = evidence_state(card.model_dump())['evidence']
        # evidence_state does not copy nested context dictionaries.
        from copy import deepcopy
        rows = deepcopy(rows)
        for index, row in enumerate(rows):
            anchor = f'{card.card_id}/quote{index + 1}'
            reuse(row, 'quote', row.get('source_ref'), anchor)
            for j, item in enumerate(row.get('context', [])):
                reuse(item, 'text', row.get('source_ref'), f'{anchor}/context{j + 1}')
        results.append(rows)
    return results
