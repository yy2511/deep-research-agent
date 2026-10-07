"""Quote location, not claim entailment. Controlled offline regression fixtures."""
import json
from urllib.parse import unquote

import pytest

from dra.quote_binding import bind_quote
from test_quote_binding_integration import make


def locate(quote, raw, *, markup=False, exposures=None):
    return bind_quote(quote, {'s': raw}, {'s': exposures or [(0, len(raw))]},
                      markup_sources={'s'} if markup else set())


def test_formatting_preserves_unicode_offsets_and_original_markup():
    raw = '🙂前言。由[北方研究所](https://example.test/a_(b))发布“年度报告”，共有１２份。'
    supplied = '由北方研究所发布"年度报告"，共有12份。'
    result = locate(supplied, raw, markup=True)
    assert result['status'] == 'bound'
    match = result['candidates'][0]
    assert raw[match['start']:match['end']] == match['quote'] == raw[4:]
    assert match['binding']['method'] == 'format'
    assert match['binding']['changes']


def test_computation_text_does_not_strip_literal_markdown():
    raw = '结果 [name](value) &amp; literal'
    result = locate('结果 name & literal', raw)
    assert result['status'] != 'bound'
    assert locate('结果 [name](value) &amp; literal', raw)['status'] == 'bound'


def test_superscript_is_not_format_equivalence():
    result = locate('面积12m2', '面积12m²')
    assert result['status'] != 'bound'
    assert all(c.get('binding', {}).get('method') != 'format' for c in result['candidates'])


def test_long_copy_typo_is_corrected_and_does_not_validate_claim_content():
    original = '本次共调拨冬衣、帐篷和其他救援物资，相关部门已将全部物资运送至指定地点，并登记数量、运输日期及接收人员。'
    for submitted in (original.replace('拨', '摋'), original.replace('全部', '全郜')):
        result = locate(submitted, original)
        assert result['status'] == 'bound'
        match = result['candidates'][0]
        assert match['quote'] == original
        assert match['binding']['method'] == 'fuzzy'
        assert match['binding']['edit_distance'] == 1
        assert match['binding']['changes']


def test_numbers_receive_the_same_alignment_treatment_as_other_characters():
    original = 'This source says the archive contains 120 documents and records each document separately.'
    result = locate(original.replace('120', '130'), original)
    assert result['status'] == 'bound'
    assert result['candidates'][0]['quote'] == original


def test_same_page_and_cross_source_fuzzy_duplicates_remain_selectable():
    original = 'This original passage explains how the equipment is checked before the experiment begins.'
    copied = original.replace('checked', 'checled')
    result = locate(copied, original + '\nAnother section:\n' + original)
    assert result['status'] == 'needs_selection'
    assert len(result['candidates']) == 2
    result = bind_quote(copied, {'a': original, 'b': original},
                        {'a': [(0, len(original))], 'b': [(0, len(original))]})
    assert result['status'] == 'needs_selection'
    assert {c['source_ref'] for c in result['candidates']} == {'a', 'b'}


def test_whole_query_tail_is_recorded_not_silently_dropped():
    raw = 'This original passage explains how the equipment is checked before the experiment begins.'
    copied = raw + ' now'
    result = locate(copied, raw)
    assert result['status'] == 'needs_selection'
    assert result['candidates'][0]['binding']['missing_edge']
    assert any(c['submitted'] == ' now' and not c['original']
               for c in result['candidates'][0]['binding']['changes'])


def test_unread_end_requires_reading_not_candidate_confirmation():
    raw = 'The source records the complete observations and a final condition that must remain visible.'
    result = locate(raw, raw, exposures=[(0, len(raw)-4)])
    assert result['status'] == 'needs_read'
    assert all(c['end'] <= len(raw)-4 for c in result['candidates'])


def test_candidate_only_can_save_fuzzy_quote_and_keeps_initial_copy(tmp_path):
    ctx = make(tmp_path, ['精度0.02mm'])
    ctx.read_source({'source_ref': 's1'})
    result, ok = ctx.save_evidence({'cards': [{'claim': '待核对的描述', 'quotes': [{'quote': '精度0.05mm'}]}]})
    candidate = json.loads(result)['results'][0]['quote_results'][0]['candidates'][0]
    assert ok
    result, ok = ctx.save_evidence({'cards': [{'claim': '我的结论不由定位器判断',
                                             'quotes': [{'candidate_id': candidate['candidate_id']}]}]})
    assert ok and json.loads(result)['results'][0]['accepted']
    span = ctx.store.cards()[0].quotes[0]
    assert span.quote == '精度0.02mm'
    assert span.submitted_quote == '精度0.05mm'
    assert span.binding.confirmed
    assert not ctx.pending_quote_refs


def test_partial_card_retry_reuses_successful_spans(tmp_path):
    ctx = make(tmp_path, ['甲：已经核对的原文。乙：精度0.02mm'])
    ctx.read_source({'source_ref': 's1'})
    body, ok = ctx.save_evidence({'cards': [{'claim': '两段材料', 'quotes': [
        {'quote': '已经核对的原文。'}, {'quote': '精度0.05mm'}]}]})
    rows = json.loads(body)['results'][0]['quote_results']
    assert ok and not ctx.store.cards()
    ids = [rows[0]['candidate_id'], rows[1]['candidates'][0]['candidate_id']]
    body, ok = ctx.save_evidence({'cards': [{'claim': '两段材料', 'quotes': [{'candidate_id': i} for i in ids]}]})
    assert ok and json.loads(body)['results'][0]['accepted']
    assert [q.quote for q in ctx.store.cards()[0].quotes] == ['已经核对的原文。', '精度0.02mm']


def test_receipt_and_card_keep_correction_and_rendered_deep_link(tmp_path):
    ctx = make(tmp_path, ['来源：[北方研究所](https://example.test/a_(b))发布“年度报告”。'])
    ctx.read_source({'source_ref': 's1'})
    body, ok = ctx.save_evidence({'cards': [{'claim': '年度报告', 'quotes': [
        {'quote': '北方研究所发布"年度报告"。'}]}]})
    assert ok
    row = json.loads(body)['results'][0]['quotes'][0]
    assert row['binding']['method'] == 'format'
    span = ctx.store.cards()[0].quotes[0]
    assert '[北方研究所]' in span.quote
    assert 'https://example.test/a_(b)' not in unquote(span.deep_link)
    assert '北方研究所发布“年度报告”' in unquote(span.deep_link)


def test_vision_schema_preserves_candidate_only_branch():
    from dra.vision import tool_schemas
    from dra.simple_research import TOOL_SCHEMAS
    schemas = tool_schemas(TOOL_SCHEMAS, enabled=True, originals=True)
    s = next(s['function'] for s in schemas if s['function']['name'] == 'save_evidence')
    q = s['parameters']['properties']['cards']['items']['properties']['quotes']['items']
    assert {'required': ['candidate_id']} in q['anyOf']
    assert {'required': ['image_ref', 'observation']} in q['anyOf']


def test_literal_file_syntax_survives_a_copy_error():
    original = 'Output [name](value) &amp; literal data: every row is retained for the following calculation.'
    result = locate(original.replace('every', 'evary'), original)
    assert result['status'] == 'bound'
    assert result['candidates'][0]['quote'] == original
    assert result['candidates'][0]['binding']['edit_distance'] == 1


def test_overlapping_fuzzy_occurrences_are_not_merged_into_one():
    copied = 'a' * 25 + 'b' + 'a' * 24
    result = locate(copied, 'a' * 52)
    assert result['status'] == 'needs_selection'
    assert {(c['start'], c['end']) for c in result['candidates']} >= {(0, 50), (1, 51), (2, 52)}


def test_other_close_copy_is_found_even_when_first_match_has_fewer_edits():
    original = ''.join(chr(0x4e00 + i) for i in range(200))
    first = original[:50] + 'A' + original[51:]
    second = original[:50] + 'ABC' + original[53:]
    result = locate(original, first + '\n' + second)
    assert result['status'] == 'needs_selection'
    assert {c['quote'] for c in result['candidates']} == {first, second}


def test_fuzzy_binding_never_bridges_unread_gap():
    raw = 'The source records the complete observations and a final condition that must remain visible.'
    result = locate(raw, raw, exposures=[(0, 50), (51, len(raw))])
    assert result['status'] != 'bound'
    assert all(c['end'] <= 50 or c['start'] >= 51 for c in result['candidates'])


def test_source_mismatch_requires_selection_and_keeps_actual_source(tmp_path):
    ctx = make(tmp_path, ['另一篇文章的无关材料。', '第二篇文章的完整资料。'])
    ctx.read_source({'source_ref': 's1'})
    ctx.read_source({'source_ref': 's2'})
    body, ok = ctx.save_evidence({'cards': [{'claim': '资料', 'quotes': [
        {'source_ref': 's1', 'quote': '第二篇文章的完整资料。'}]}]})
    row = json.loads(body)['results'][0]['quote_results'][0]
    assert ok and not ctx.store.cards() and row['binding_status'] == 'source_mismatch'
    body, ok = ctx.save_evidence({'cards': [{'claim': '资料', 'quotes': [
        {'candidate_id': row['candidates'][0]['candidate_id']}]}]})
    assert ok and json.loads(body)['results'][0]['accepted']
    assert ctx.store.cards()[0].quotes[0].source_ref == 's2'
