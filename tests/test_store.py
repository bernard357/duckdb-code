"""The store's three read-only tools: impact, explain, report.

Indexes the committed fixture once per test via :func:`index_folder` and asserts
the end-to-end behaviour of the serving surface (AC3/AC5), fix 2 (best_effort
seed-origin propagation), the single-shape ``ExplanationResult``, report
per-kind/per-language aggregation and merged roots, the fix 6 empty-index
contract, and the read-only write guard.
"""

from pathlib import Path

import pytest

from duckdb_graph.indexer import index_folder
from duckdb_graph.store import GraphStore, ReadOnlyError

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


def _indexed(tmp_path) -> GraphStore:
    """Open a writable store, index the fixture, return it (root = fixture)."""
    store = GraphStore(db_path=str(tmp_path / "graph.duckdb"), root=str(FIXTURE))
    index_folder(store, FIXTURE)
    return store


def test_impact_on_leaf_symbol_is_empty(tmp_path):
    store = _indexed(tmp_path)
    result = store.impact("leaf.unused")
    assert [n.qualified_name for n in result.resolved] == ["leaf.unused"]
    assert result.affected == []
    assert result.affected_files == []
    assert result.ambiguous is False
    store.close()


def test_impact_on_nonexistent_target_is_empty(tmp_path):
    store = _indexed(tmp_path)
    result = store.impact("does.not.exist")
    assert result.resolved == []
    assert result.affected == []
    assert result.ambiguous is False
    store.close()


def test_impact_on_widely_used_symbol_has_transitive_dependents(tmp_path):
    store = _indexed(tmp_path)
    result = store.impact("payments.charge.Base")

    by_qn = {node.node.qualified_name: node for node in result.affected}
    # Charge inherits Base directly (distance 1); everything that depends on
    # Charge reaches Base transitively at distance 2.
    assert by_qn["payments.charge.Charge"].distance == 1
    assert by_qn["payments.premium.Premium"].distance == 2
    assert by_qn["payments.billing.bill"].distance == 2
    # A path/exact-qn seed origin propagates exact down exact edges.
    assert all(node.resolution == "exact" for node in result.affected)
    # Distances are always >= 1 (seeds at distance 0 are never emitted).
    assert all(node.distance >= 1 for node in result.affected)
    store.close()


def test_impact_never_traverses_defines_edges(tmp_path):
    store = _indexed(tmp_path)
    # The Charge class's own method is a defines child, not a dependent; it must
    # never appear in the affected set reached from Charge.
    result = store.impact("payments.charge.Charge")
    affected_qns = {node.node.qualified_name for node in result.affected}
    assert "payments.charge.Charge.run" not in affected_qns
    # Its real reverse dependents (subclass + caller) are present at distance 1.
    assert "payments.premium.Premium" in affected_qns
    assert "payments.billing.bill" in affected_qns
    store.close()


def test_impact_affected_ordering_is_deterministic(tmp_path):
    store = _indexed(tmp_path)
    keys = [
        (node.distance, node.node.path, node.node.start_line, node.node.node_id)
        for node in store.impact("payments.charge.Base").affected
    ]
    assert keys == sorted(keys)
    store.close()


def test_impact_path_target_seeds_whole_file(tmp_path):
    store = _indexed(tmp_path)
    result = store.impact("payments/charge.py")
    assert result.ambiguous is False
    # Every node stored under that path is a seed (file, module, imports, both
    # classes, both methods) and none of them is emitted in affected.
    resolved_paths = {node.path for node in result.resolved}
    assert resolved_paths == {"payments/charge.py"}
    affected_paths = set(result.affected_files)
    assert "payments/charge.py" not in affected_paths
    # The file's reverse dependents (billing, premium) are affected.
    assert affected_paths == {"payments/billing.py", "payments/premium.py"}
    store.close()


def test_impact_src_prefixed_path_resolves_via_root_strip(tmp_path):
    store = _indexed(tmp_path)
    # The index was built with root basename "sample"; a caller passing that
    # leading segment resolves to the stored folder-relative path (fix 5 strip).
    prefixed = store.impact("sample/payments/charge.py")
    direct = store.impact("payments/charge.py")
    assert {n.node_id for n in prefixed.resolved} == {
        n.node_id for n in direct.resolved
    }
    assert prefixed.resolved
    store.close()


def test_impact_depth_zero_returns_no_affected(tmp_path):
    store = _indexed(tmp_path)
    result = store.impact("payments.charge.Charge", depth=0)
    assert result.resolved
    assert result.affected == []
    store.close()


def test_impact_depth_caps_traversal(tmp_path):
    store = _indexed(tmp_path)
    full = store.impact("payments.charge.Base")
    capped = store.impact("payments.charge.Base", depth=1)
    assert all(node.distance == 1 for node in capped.affected)
    assert len(capped.affected) < len(full.affected)
    store.close()


def test_impact_suffix_seed_propagates_best_effort(tmp_path):
    store = _indexed(tmp_path)
    # "Charge" matches two distinct qualified names (payments + web) by suffix,
    # so the seed origin is best_effort and dependents inherit best_effort.
    result = store.impact("Charge")
    assert result.ambiguous is True
    assert result.affected
    assert all(node.resolution == "best_effort" for node in result.affected)
    store.close()


def test_impact_attribute_tail_run_edges_are_best_effort(tmp_path):
    store = _indexed(tmp_path)
    # billing.bill does `c.run(amount)` — an attribute-tail call whose tail
    # `run` suffix-matches every `run` method in the index (Base.run,
    # Charge.run, web Charge.run). Those edges must be best_effort, never exact
    # (fix 5). So impacting a `run` method reaches billing.bill best_effort.
    result = store.impact("payments.charge.Base.run")
    affected = {item.node.qualified_name: item for item in result.affected}
    assert "payments.billing.bill" in affected
    assert affected["payments.billing.bill"].resolution == "best_effort"
    store.close()


def test_explain_always_single_result_shape(tmp_path):
    store = _indexed(tmp_path)
    result = store.explain("payments.charge.Charge")
    assert len(result.resolved) == 1
    assert len(result.explanations) == 1
    assert result.ambiguous is False

    explanation = result.explanations[0]
    assert [n.name for n in explanation.defines] == ["run"]
    assert explanation.fan_out_count == len(explanation.fan_out)
    assert explanation.fan_in_count == len(explanation.fan_in)
    # Charge inherits Base and is subclassed by Premium.
    assert [n.qualified_name for n in explanation.bases] == ["payments.charge.Base"]
    assert [n.qualified_name for n in explanation.subclasses] == [
        "payments.premium.Premium"
    ]
    fan_in_qns = {n.qualified_name for n in explanation.fan_in}
    assert "payments.billing.bill" in fan_in_qns
    store.close()


def test_explain_zero_match_returns_empty_shape(tmp_path):
    store = _indexed(tmp_path)
    result = store.explain("nope.nothing")
    assert result.resolved == []
    assert result.explanations == []
    store.close()


def test_explain_lists_are_ordered(tmp_path):
    store = _indexed(tmp_path)
    explanation = store.explain("payments.charge.Charge").explanations[0]
    keys = [(n.path, n.start_line, n.node_id) for n in explanation.fan_in]
    assert keys == sorted(keys)
    store.close()


def test_report_aggregates_kinds_languages_and_roots(tmp_path):
    store = _indexed(tmp_path)
    report = store.report()
    assert report.schema_version == 1
    assert report.languages == ["python", "typescript"]
    assert report.files_by_language == {"python": 5, "typescript": 2}
    assert report.nodes_by_kind["class"] == 4
    assert report.nodes_by_kind["module"] == 7
    assert report.edges_by_kind["defines"] > 0
    assert report.edges_by_kind["inherits"] == 2
    # A root meta row is present -> roots is exactly [meta.root].
    assert report.roots == [str(FIXTURE)]
    store.close()


def test_report_empty_index_returns_empty_shape(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    store = GraphStore(db_path=str(tmp_path / "empty.duckdb"), root=str(empty_dir))
    index_folder(store, empty_dir)
    report = store.report()
    assert report.nodes_by_kind == {}
    assert report.edges_by_kind == {}
    assert report.files_by_language == {}
    assert report.languages == []
    assert report.roots == [str(empty_dir)]
    store.close()


def test_read_only_store_rejects_writes(tmp_path):
    db_path = str(tmp_path / "graph.duckdb")
    writer = _indexed_at(db_path)
    writer.close()

    reader = GraphStore(db_path=db_path, read_only=True)
    with pytest.raises(ReadOnlyError):
        reader._guard_writable()
    with pytest.raises(ReadOnlyError):
        index_folder(reader, FIXTURE)
    reader.close()


def _indexed_at(db_path: str) -> GraphStore:
    store = GraphStore(db_path=db_path, root=str(FIXTURE))
    index_folder(store, FIXTURE)
    return store


def test_store_index_method_delegates_full_then_incremental(tmp_path):
    import shutil

    work = tmp_path / "tree"
    shutil.copytree(FIXTURE, work)
    store = GraphStore(db_path=str(tmp_path / "graph.duckdb"), root=str(work))

    first = store.index(work)
    assert first.mode == "full"
    second = store.index(work)
    assert second.mode == "incremental"
    assert second.rebuilt == 0
    store.close()


def test_store_incremental_keeps_impact_correct_after_edit(tmp_path):
    import shutil

    work = tmp_path / "tree"
    shutil.copytree(FIXTURE, work)
    store = GraphStore(db_path=str(tmp_path / "graph.duckdb"), root=str(work))
    store.index(work)

    # Edit charge.py (shifts the Base/Charge node ids); the incremental run must
    # re-resolve so impact over Base still reaches Charge and its dependents.
    charge = work / "payments" / "charge.py"
    charge.write_text(
        "# edited\n" + charge.read_text(encoding="utf-8"), encoding="utf-8"
    )
    report = store.index(work)
    assert report.mode == "incremental"

    result = store.impact("payments.charge.Base")
    affected = {item.node.qualified_name for item in result.affected}
    assert "payments.charge.Charge" in affected
    assert "payments.premium.Premium" in affected
    assert "payments.billing.bill" in affected
    store.close()
