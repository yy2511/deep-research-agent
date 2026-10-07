"""引文定位规则：模型提交的原话如何对应到原文里的确切位置（唯一匹配、多处出现、未读部分不能引用等）。"""
import pytest

from dra.quote_binding import bind_quote as bind


def test_unique_returns_original_offsets():
    assert bind('参数 0.02mm', {'p': '标题\n参数 0.02mm。'}, {'p': [(0, 13)]}) == {
        'status': 'bound', 'candidates': [{'source_ref': 'p', 'start': 3, 'end': 12, 'quote': '参数 0.02mm'}]}


def test_product_parameter_quote_cannot_bind_invented_claim():
    # Controlled page text keeps the original 0.02 mm versus 0.05 mm regression.
    raw = '产品参数：精度 0.02mm。'
    assert '0.02mm' in raw and '0.05mm' not in raw
    assert bind('0.05mm超高精度，2秒极速扫描，20秒快速生成3D模型',
                {'s42': raw}, {'s42': [(0, len(raw))]})['status'] == 'not_found'


def test_controlled_homepage_is_selected_only_after_reading():
    # Controlled fixture, NOT a historical snapshot or historical read event.
    quote = '0.05mm超高精度，2秒极速扫描，20秒快速生成3D模型'
    sources = {'product': '产品精度0.02mm', 'homepage': '介绍：' + quote + '。'}
    assert bind(quote, sources, {'product': [(0, 10)]})['status'] == 'not_found'
    result = bind(quote, sources, {'homepage': [(0, len(sources['homepage']))]})
    assert result['status'] == 'bound'
    assert result['candidates'][0]['source_ref'] == 'homepage'


@pytest.mark.parametrize('sources', [{'a': '原话', 'b': '原话'}, {'a': '原话和原话'}])
def test_multiple_occurrences_require_disambiguation(sources):
    result = bind('原话', sources, {k: [(0, len(v))] for k, v in sources.items()})
    assert result['status'] == 'ambiguous'
    assert len(result['candidates']) == 2


def test_cross_chunk_and_overlapping_exposures():
    raw = 'x' * 698 + '精度0.02mm' + 'y' * 20
    result = bind('精度0.02mm', {'a': raw}, {'a': [(0, 700), (699, len(raw)), (0, 700)]})
    assert result['status'] == 'bound'
    assert result['candidates'][0]['start'] == 698


def test_unread_gap_cannot_be_bridged():
    assert bind('abcdef', {'a': 'abcdef'}, {'a': [(0, 2), (3, 6)]})['status'] == 'not_found'


def test_actor_read_isolation():
    sources = {'a': '独立子任务的原话'}
    child = {'a': [(0, len(sources['a']))]}
    assert bind('独立子任务', sources, {})['status'] == 'not_found'
    assert bind('独立子任务', sources, child)['status'] == 'bound'


def test_whitespace_normalization_preserves_raw_span():
    raw = '前言:精度\r\n  0.02mm\u00a0稳定。'
    result = bind('精度 0.02mm 稳定', {'a': raw}, {'a': [(0, len(raw))]})
    candidate = result['candidates'][0]
    assert result['status'] == 'bound'
    assert candidate == {'source_ref': 'a', 'start': 3, 'end': len(raw)-1, 'quote': raw[3:-1]}


def test_normalized_duplicate_does_not_silently_prefer_exact_match():
    assert bind('a b', {'a': 'a b', 'b': 'a\nb'}, {'a': [(0, 3)], 'b': [(0, 3)]})['status'] == 'ambiguous'


@pytest.mark.parametrize('quote', ['精度0.05mm', '精度为0.02mm', '精度0.02 mm'])
def test_short_approximate_quotes_require_selection(quote):
    result = bind(quote, {'a': '精度0.02mm'}, {'a': [(0, 8)]})
    assert result['status'] == 'needs_selection'
    assert result['candidates'][0]['quote'] == '精度0.02mm'


@pytest.mark.parametrize('quote', ['', ' \n\t'])
def test_empty_quote_rejected(quote):
    assert bind(quote, {'a': '文本'}, {'a': [(0, 2)]})['status'] == 'invalid'


def test_overlapping_occurrences_not_lost():
    assert len(bind('aaa', {'a': 'aaaa'}, {'a': [(0, 4)]})['candidates']) == 2


def test_whitespace_is_not_removed_between_numbers():
    assert bind('10', {'a': '1 0'}, {'a': [(0, 3)]})['status'] == 'not_found'


def test_valid_quote_does_not_prove_claim():
    # A caller may attach any wrong claim; binding alone cannot check entailment.
    assert bind('精度0.02mm', {'a': '精度0.02mm'}, {'a': [(0, 8)]})['status'] == 'bound'
