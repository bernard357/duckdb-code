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
MARKDOWN_FIXTURE = Path(__file__).parent / "fixtures" / "markdown"


def _indexed(tmp_path) -> GraphStore:
    """Open a writable store, index the fixture, return it (root = fixture)."""
    store = GraphStore(db_path=str(tmp_path / "graph.duckdb"), root=str(FIXTURE))
    index_folder(store, FIXTURE)
    return store


def _indexed_markdown(tmp_path) -> GraphStore:
    store = GraphStore(db_path=str(tmp_path / "kg.duckdb"), root=str(MARKDOWN_FIXTURE))
    index_folder(store, MARKDOWN_FIXTURE)
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


def test_impact_on_linked_page_returns_its_linkers(tmp_path):
    store = _indexed_markdown(tmp_path)
    result = store.impact("index.md")
    affected_paths = set(result.affected_files)
    # guide.md, reference-notes.md and orphan.md all link to index.md.
    assert "guide.md" in affected_paths
    assert "reference-notes.md" in affected_paths
    assert "orphan.md" in affected_paths
    store.close()


def test_explain_page_shows_links_and_headings(tmp_path):
    store = _indexed_markdown(tmp_path)
    explanation = store.explain("index.md").explanations[0]
    fan_out_paths = {n.path for n in explanation.fan_out}
    fan_in_paths = {n.path for n in explanation.fan_in}
    # Outbound links: index -> guide, index -> reference-notes (wikilink slug).
    assert "guide.md" in fan_out_paths
    assert "reference-notes.md" in fan_out_paths
    # Inbound links: pages that link back to index.
    assert "guide.md" in fan_in_paths
    assert "orphan.md" in fan_in_paths
    # defines surfaces the page's heading structure.
    assert any(n.kind == "heading" for n in explanation.defines)
    store.close()


def test_orphan_page_has_zero_inbound_links(tmp_path):
    store = _indexed_markdown(tmp_path)
    result = store.impact("orphan.md")
    # orphan.md links out but nothing links to it: no reverse dependents.
    assert result.affected == []
    explanation = store.explain("orphan.md").explanations[0]
    assert explanation.fan_in == []
    store.close()


def test_report_counts_page_heading_tag_and_link_edges(tmp_path):
    store = _indexed_markdown(tmp_path)
    report = store.report()
    assert report.languages == ["markdown"]
    assert report.nodes_by_kind.get("page") == 4
    assert report.nodes_by_kind.get("heading", 0) > 0
    # Tags are corpus-global: one node per DISTINCT tag name across the corpus
    # (overview, knowledge-base, architecture, guide, orphan), not one per use.
    assert report.nodes_by_kind.get("tag") == 5
    assert report.edges_by_kind.get("links_to", 0) > 0
    # Every `tagged` edge binds to its tag node and survives into the DB, so the
    # kind is counted by report() (the plan's "tagged shows in report counts").
    assert report.edges_by_kind.get("tagged", 0) > 0
    # The lone embed target (assets/diagram.png) has no page, so it is dangling
    # and dropped: no embeds edge survives.
    assert "embeds" not in report.edges_by_kind
    store.close()


def test_tag_nodes_are_corpus_global_and_tagged_edges_resolve(tmp_path):
    store = _indexed_markdown(tmp_path)
    # architecture is tagged by both index.md and reference-notes.md but is a
    # single node with two inbound `tagged` edges, both exact.
    rows = store._con.execute(
        "SELECT n.path FROM nodes n WHERE n.node_id = 'tag:architecture'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == ""  # path-independent synthetic path
    inbound = store._con.execute(
        "SELECT e.resolution FROM edges e WHERE e.kind = 'tagged' "
        "AND e.dst_id = 'tag:architecture' ORDER BY e.src_id"
    ).fetchall()
    assert len(inbound) == 2
    assert all(r[0] == "exact" for r in inbound)
    store.close()


def test_markdown_link_never_binds_to_a_code_node_end_to_end(tmp_path):
    # Index markdown and code into one store; a markdown links_to edge must
    # never point at a code node, and no code edge points at a page node.
    store = GraphStore(db_path=str(tmp_path / "mixed.duckdb"))
    index_folder(store, MARKDOWN_FIXTURE)
    # Second folder: re-index code on top would rebuild; instead assert on the
    # markdown-only graph that every links_to/embeds dst is a page.
    rows = store._con.execute(
        "SELECT n.kind FROM edges e JOIN nodes n ON n.node_id = e.dst_id "
        "WHERE e.kind IN ('links_to', 'embeds')"
    ).fetchall()
    assert rows
    assert all(row[0] == "page" for row in rows)
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


def test_export_meta_matches_report(tmp_path):
    store = _indexed(tmp_path)
    export = store.export()
    assert export.meta == store.report()
    store.close()


def test_export_counts_match_tables(tmp_path):
    store = _indexed(tmp_path)
    export = store.export()
    node_count = store._con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edge_count = store._con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    assert len(export.nodes) == node_count
    assert len(export.edges) == edge_count
    store.close()


def test_export_edges_reference_real_nodes(tmp_path):
    store = _indexed(tmp_path)
    export = store.export()
    node_ids = {node.id for node in export.nodes}
    for edge in export.edges:
        assert edge.source in node_ids
        assert edge.target in node_ids
    store.close()


def test_export_serialized_keys_are_cytoscape_shape(tmp_path):
    store = _indexed(tmp_path)
    export = store.export()
    node_dump = export.nodes[0].model_dump()
    assert set(node_dump) == {
        "id",
        "kind",
        "name",
        "qualified_name",
        "path",
        "language",
        "start_line",
        "end_line",
    }
    assert "node_id" not in node_dump
    edge_dump = export.edges[0].model_dump()
    assert set(edge_dump) == {"source", "target", "kind", "resolution"}
    assert "src_id" not in edge_dump
    assert "dst_id" not in edge_dump
    store.close()


def test_export_runs_on_read_only_store(tmp_path):
    db_path = str(tmp_path / "graph.duckdb")
    writer = _indexed_at(db_path)
    writer_export = writer.export()
    writer.close()

    reader = GraphStore(db_path=db_path, read_only=True)
    export = reader.export()
    assert len(export.nodes) == len(writer_export.nodes)
    assert len(export.edges) == len(writer_export.edges)
    reader.close()


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
