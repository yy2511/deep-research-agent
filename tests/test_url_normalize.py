"""URL 归一化规则，供当前检索与历史复用使用。"""
from dra.models import normalize_url


def test_trailing_slash_equal():
    """路径尾部斜杠不影响归一化结果。"""
    assert normalize_url("https://example.com/post/") == normalize_url("https://example.com/post")


def test_utm_params_stripped():
    """utm_* 追踪参数被剥离，不参与归一化 key。"""
    a = normalize_url("https://example.com/post?utm_source=x&utm_medium=y")
    b = normalize_url("https://example.com/post")
    assert a == b


def test_real_query_param_kept():
    """非追踪查询参数是真实内容差异，必须保留（?page=2 ≠ 无 page）。"""
    a = normalize_url("https://example.com/list?page=2")
    b = normalize_url("https://example.com/list")
    assert a != b


def test_mixed_params_keeps_only_real_one():
    """真实参数 + utm 追踪参数混合 → 只剥 utm，保留真实参数。"""
    a = normalize_url("https://example.com/list?page=2&utm_source=x")
    b = normalize_url("https://example.com/list?page=2")
    assert a == b


def test_fragment_stripped():
    """#fragment 是页内锚点，不改变服务端内容，剥离。"""
    a = normalize_url("https://example.com/post#section2")
    b = normalize_url("https://example.com/post")
    assert a == b


def test_http_https_equal():
    """http → https 视为同页（协议统一）。"""
    assert normalize_url("http://example.com/post") == normalize_url("https://example.com/post")


def test_www_and_case_host_normalized():
    """host 大小写 + www. 前缀归一化。"""
    a = normalize_url("https://WWW.Example.COM/post")
    b = normalize_url("https://example.com/post")
    assert a == b


def test_path_case_preserved():
    """路径大小写是真实语义差异（大小写敏感的服务器），不能归一化掉。"""
    a = normalize_url("https://example.com/Post")
    b = normalize_url("https://example.com/post")
    assert a != b


def test_none_and_empty_return_empty_string():
    """None / 空串（含纯空白）→ 归一化为空串，与旧 (c.source_url or "") 兜底行为一致。"""
    assert normalize_url(None) == ""
    assert normalize_url("") == ""
    assert normalize_url("   ") == ""
