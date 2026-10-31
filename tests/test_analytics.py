"""Tests for the ``analytics`` whole-repo structural views.

Indexes a small Python tree carrying a deliberate import cycle, an unused
symbol, and a referenced symbol, then asserts each ``analytics`` kind (cycles,
clusters, dead_code, hotspots) reads the existing graph data correctly, and
that an unknown kind raises.
"""

import textwrap

import pytest

from duckdb_code.indexer import index_folder
from duckdb_code.store import CodeStore


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _cyclic_tree(tmp_path):
    """A tree where ``a`` and ``b`` call each other (an import/call cycle).

    ``used`` is referenced by ``entry``; ``orphan`` is referenced by nothing
    (a dead-code candidate). ``test_something`` lives in a test file so it must
    never surface as dead code even though nothing references it.
    """
    tree = tmp_path / "pkg"
    tree.mkdir()
    _write(
        tree / "a.py",
        """
        from pkg import b

        def ping():
            return b.pong()
        """,
    )
    _write(
        tree / "b.py",
        """
        from pkg import a

        def pong():
            return a.ping()
        """,
    )
    _write(
        tree / "api.py",
        """
        def used():
            return 1

        def orphan():
            return 2

        def entry():
            return used()
        """,
    )
    tests_dir = tree / "tests"
    tests_dir.mkdir()
    _write(
        tests_dir / "test_something.py",
        """
        def test_something():
            return 3
        """,
    )
    store = CodeStore(db_path=str(tmp_path / "analytics.duckdb"), root=str(tree))
    index_folder(store, tree)
    return store


def test_cycles_surface_a_mutual_dependency(tmp_path):
    store = _cyclic_tree(tmp_path)
    try:
        result = store.analytics("cycles")
    finally:
        store.close()
    assert result.kind == "cycles"
    # At least one cycle; every reported cycle has size > 1 (singletons are not
    # cycles) and its members materialize as real nodes.
    assert result.results, "the a<->b mutual call should form a cycle"
    for item in result.results:
        assert item.size > 1
        assert len(item.members) == item.size
    # The a.ping <-> b.pong mutual call is one of the cycles. Qualified names
    # are relative to the indexed root (the `pkg` folder), so modules are
    # `a`/`b`, not `pkg.a`/`pkg.b`.
    all_members = {m.qualified_name for item in result.results for m in item.members}
    assert {"a.ping", "b.pong"} <= all_members


def test_clusters_group_the_graph(tmp_path):
    store = _cyclic_tree(tmp_path)
    try:
        result = store.analytics("clusters")
    finally:
        store.close()
    assert result.kind == "clusters"
    assert result.results, "communities should be present"
    # Largest first, and each cluster is a non-empty member list.
    sizes = [item.size for item in result.results]
    assert sizes == sorted(sizes, reverse=True)
    assert all(item.members for item in result.results)


def test_dead_code_is_conservatively_scoped(tmp_path):
    store = _cyclic_tree(tmp_path)
    try:
        result = store.analytics("dead_code")
    finally:
        store.close()
    assert result.kind == "dead_code"
    names = {item.node.qualified_name for item in result.results}
    # orphan is referenced by nothing -> a candidate. (QNs are root-relative:
    # module `api`, not `pkg.api`.)
    assert "api.orphan" in names
    # used is referenced by entry -> not dead.
    assert "api.used" not in names
    # a test symbol is excluded even with zero inbound edges.
    assert "tests.test_something.test_something" not in names
    assert not any(item.node.path.startswith("tests/") for item in result.results)


def test_hotspots_rank_by_centrality_times_churn(tmp_path):
    store = _cyclic_tree(tmp_path)
    try:
        result = store.analytics("hotspots")
    finally:
        store.close()
    assert result.kind == "hotspots"
    # A fresh index of a non-git tree has zero churn, so hotspots is empty — a
    # hotspot needs both centrality and churn, and churn is zero here.
    for item in result.results:
        assert item.score >= 0
    # Ordering is non-increasing by score when any rows are present.
    scores = [item.score for item in result.results]
    assert scores == sorted(scores, reverse=True)


def test_unknown_kind_raises(tmp_path):
    store = _cyclic_tree(tmp_path)
    try:
        with pytest.raises(ValueError, match="unknown analytics kind"):
            store.analytics("bogus")
    finally:
        store.close()
