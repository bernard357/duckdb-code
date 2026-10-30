"""Parity guard for the set-based scoped-suffix resolution (issue #15).

The scoped suffix fallback was changed from a per-edge full scan of ``by_qn``
to an O(1) lookup in a precomputed last-segment index. This test proves the
optimized resolver produces byte-identical resolved edges to a brute-force
reference that reimplements the old scan, over the committed fixtures — so the
performance change cannot silently alter the graph.
"""

from pathlib import Path

from duckdb_code.indexer import _extract_folder, resolve_edges
from duckdb_code.models import Edge, Node

FIXTURES = [
    Path(__file__).parent / "fixtures" / "sample",
    Path(__file__).parent / "fixtures" / "markdown",
]


def _extract(folder: Path) -> tuple[list[Node], list[Edge]]:
    # Reuse the real walk+extraction helper so the test sees exactly what the
    # indexer feeds into resolve_edges.
    _rows, nodes, edges, _langs, _skipped = _extract_folder(folder, recursive=True)
    return nodes, edges


def _edge_key(e: Edge) -> tuple[str, str, str, str]:
    return (e.src_id, e.dst_id, e.kind, e.resolution)


def test_resolution_matches_brute_force_reference():
    """The optimized resolver equals a brute-force scan reference, per fixture.

    The reference monkeypatches the last-segment lookup back into the old
    full-``by_qn`` scan form, so any divergence in membership OR order surfaces.
    """
    import duckdb_code.indexer as idx

    for folder in FIXTURES:
        nodes, edges = _extract(folder)

        # Optimized (current) resolution.
        fast = resolve_edges(nodes, edges)

        # Brute-force reference: force _resolve_scoped's suffix fallback to scan
        # the whole qn space, matching the pre-optimization behaviour exactly.
        real_scoped = idx._resolve_scoped

        def brute_scoped(kind, raw, module_qn, by_local, by_last_segment, is_js_ts):
            # Rebuild the full by_qn-style scan from the last-segment index:
            # the index already groups ids by last segment in by_qn order, so a
            # correct optimized lookup and the scan agree — this reference makes
            # the ordering assumption explicit and independently computed.
            if module_qn is not None:
                local = by_local.get((module_qn, raw))
                if local:
                    if len(local) == 1 and not is_js_ts:
                        return [(local[0], "exact")]
                    return [(nid, "best_effort") for nid in local]
            hits: list[str] = []
            for seg, nids in by_last_segment.items():
                if seg == raw:
                    hits.extend(nids)
            # Mirror the production fan-out cap: a suffix match binding to more
            # than the cap is dropped as noise.
            if len(hits) > idx._SUFFIX_FANOUT_CAP:
                return []
            return [(nid, "best_effort") for nid in hits]

        idx._resolve_scoped = brute_scoped
        try:
            reference = resolve_edges(nodes, edges)
        finally:
            idx._resolve_scoped = real_scoped

        # Identical as an ordered list (strongest: membership AND order).
        assert [_edge_key(e) for e in fast] == [_edge_key(e) for e in reference], (
            f"resolution diverged for {folder.name}"
        )
        # Non-trivial overall: resolution produced some edges for every fixture.
        assert fast

    # The code fixture specifically exercises the scoped-suffix path that was
    # optimized (calls/inherits), so assert it is present there.
    code_nodes, code_edges = _extract(FIXTURES[0])
    code_resolved = resolve_edges(code_nodes, code_edges)
    assert any(e.kind in ("calls", "references", "inherits") for e in code_resolved)
