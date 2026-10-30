"""Unit tests for TESTS-edge enrichment (issue #26 / #32 data source).

Exercises the pure `enrich_test_edges` and `_is_test_path` helpers directly
with synthetic nodes/edges, so the test-reachability derivation is verified in
isolation without needing a dedicated on-disk fixture.
"""

from duckdb_code.indexer import _is_test_path, enrich_test_edges
from duckdb_code.models import Edge, Node


def _node(node_id: str, path: str) -> Node:
    return Node(
        node_id=node_id,
        kind="function",
        name=node_id,
        qualified_name=node_id,
        path=path,
        start_line=1,
        end_line=2,
        language="python",
    )


def test_is_test_path_conventions():
    assert _is_test_path("tests/test_store.py")
    assert _is_test_path("pkg/foo_test.py")
    assert _is_test_path("web/charge.test.ts")
    assert _is_test_path("web/charge.spec.ts")
    assert _is_test_path("src/__tests__/a.js")
    assert not _is_test_path("src/duckdb_code/store.py")
    assert not _is_test_path("pkg/contest.py")  # not test_/_test, no test dir


def test_enrich_emits_tests_edges_from_test_to_non_test():
    nodes = [
        _node("t_fn", "tests/test_store.py"),
        _node("prod_fn", "src/store.py"),
    ]
    resolved = [
        Edge(
            src_id="t_fn",
            dst_id="prod_fn",
            kind="calls",
            path="tests/test_store.py",
            resolution="exact",
            confidence=0.9,
        )
    ]
    tests_edges = enrich_test_edges(nodes, resolved)
    assert len(tests_edges) == 1
    edge = tests_edges[0]
    assert edge.kind == "tests"
    assert (edge.src_id, edge.dst_id) == ("t_fn", "prod_fn")
    # The tests edge inherits the reach edge's confidence/resolution.
    assert edge.confidence == 0.9
    assert edge.resolution == "exact"


def test_enrich_ignores_non_test_sources_and_inherits_targets():
    nodes = [
        _node("prod_a", "src/a.py"),
        _node("prod_b", "src/b.py"),
        _node("t_fn", "tests/test_a.py"),
        _node("t_other", "tests/test_b.py"),
    ]
    resolved = [
        # non-test -> non-test: not a tests edge
        Edge(
            src_id="prod_a",
            dst_id="prod_b",
            kind="calls",
            path="src/a.py",
            resolution="exact",
            confidence=1.0,
        ),
        # test -> test: excluded (target is also a test file)
        Edge(
            src_id="t_fn",
            dst_id="t_other",
            kind="references",
            path="tests/test_a.py",
            resolution="best_effort",
            confidence=0.55,
        ),
        # inherits is NOT a reach kind -> excluded
        Edge(
            src_id="t_fn",
            dst_id="prod_a",
            kind="inherits",
            path="tests/test_a.py",
            resolution="exact",
            confidence=0.9,
        ),
    ]
    assert enrich_test_edges(nodes, resolved) == []


def test_enrich_dedups_repeated_reach():
    nodes = [_node("t_fn", "tests/test_a.py"), _node("prod", "src/a.py")]
    resolved = [
        Edge(
            src_id="t_fn",
            dst_id="prod",
            kind="calls",
            path="tests/test_a.py",
            resolution="exact",
            confidence=0.9,
        ),
        Edge(
            src_id="t_fn",
            dst_id="prod",
            kind="references",
            path="tests/test_a.py",
            resolution="best_effort",
            confidence=0.55,
        ),
    ]
    tests_edges = enrich_test_edges(nodes, resolved)
    assert len(tests_edges) == 1  # one (src, dst) pair, deduped
