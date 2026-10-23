"""ETag byte-identity: same content same etag, changed content different etag."""

from duckdb_graph.etag import compute_etag


def test_etag_is_deterministic():
    first = compute_etag("def run():\n    pass\n")
    second = compute_etag("def run():\n    pass\n")
    assert first == second


def test_etag_changes_with_content():
    a = compute_etag("def run():\n    pass\n")
    b = compute_etag("def run():\n    return 1\n")
    assert a != b


def test_etag_is_sha256_hexdigest():
    etag = compute_etag("hello")
    assert len(etag) == 64
    assert all(c in "0123456789abcdef" for c in etag)
