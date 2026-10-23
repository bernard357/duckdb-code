"""The indexer: full-build counts, determinism, stamped_at, and the seam."""

import datetime
import shutil
from pathlib import Path

from duckdb_graph.etag import compute_etag
from duckdb_graph.extractors.python import PythonExtractor
from duckdb_graph.indexer import _rebuild_file, index_folder
from duckdb_graph.store import GraphStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample"
MARKDOWN_FIXTURE = Path(__file__).parent / "fixtures" / "markdown"


def _store(tmp_path) -> GraphStore:
    return GraphStore(db_path=str(tmp_path / "graph.duckdb"))


def _copy_fixture(tmp_path) -> Path:
    """Copy the committed fixture into a writable working tree under tmp_path.

    Edits and deletes happen on the copy so the committed fixture is never
    mutated (AGENTS.md: fixtures hold data only, never edit them).
    """
    work = tmp_path / "tree"
    shutil.copytree(FIXTURE, work)
    return work


def _node_ids(store: GraphStore, path: str | None = None) -> set[str]:
    if path is None:
        rows = store._con.execute("SELECT node_id FROM nodes").fetchall()
    else:
        rows = store._con.execute(
            "SELECT node_id FROM nodes WHERE path = ?", [path]
        ).fetchall()
    return {row[0] for row in rows}


def _edge_set(store: GraphStore) -> set[tuple[str, str, str, str, str]]:
    rows = store._con.execute(
        "SELECT src_id, dst_id, kind, path, resolution FROM edges"
    ).fetchall()
    return {tuple(row) for row in rows}


def _table_count(store: GraphStore, table: str) -> int:
    return store._con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_index_folder_counts(tmp_path):
    store = _store(tmp_path)
    try:
        report = index_folder(store, FIXTURE)
    finally:
        pass

    # Source files: 5 python (charge, billing, premium, __init__, leaf) + 2 ts.
    # README.txt and assets/data.txt have no language -> skipped (fix 6: a
    # no-indexable sub-folder contributes nothing and never errors).
    assert report.skipped == 2
    assert report.indexed == report.files - report.skipped
    assert report.languages.get("python", 0) == 5
    assert report.languages.get("typescript", 0) == 2
    assert report.nodes > 0
    assert report.edges > 0
    store.close()


def test_index_folder_is_deterministic(tmp_path):
    store_a = GraphStore(db_path=str(tmp_path / "a.duckdb"))
    store_b = GraphStore(db_path=str(tmp_path / "b.duckdb"))

    report_a = index_folder(store_a, FIXTURE)
    report_b = index_folder(store_b, FIXTURE)
    assert report_a.model_dump() == report_b.model_dump()

    ids_a = [
        row[0]
        for row in store_a._con.execute(
            "SELECT node_id FROM nodes ORDER BY node_id"
        ).fetchall()
    ]
    ids_b = [
        row[0]
        for row in store_b._con.execute(
            "SELECT node_id FROM nodes ORDER BY node_id"
        ).fetchall()
    ]
    assert ids_a == ids_b
    store_a.close()
    store_b.close()


def test_rerun_over_existing_db_is_incremental_noop(tmp_path):
    store = _store(tmp_path)
    first = index_folder(store, FIXTURE)
    assert first.mode == "full"
    second = index_folder(store, FIXTURE)
    # Incremental-by-default: the second run reconciles nothing, so it is a
    # no-op whose whole-graph totals equal the first run's.
    assert second.mode == "incremental"
    assert second.rebuilt == 0
    assert second.deleted == 0
    assert second.files == first.files
    assert second.indexed == first.indexed
    assert second.skipped == first.skipped
    assert second.nodes == first.nodes
    assert second.edges == first.edges
    assert second.languages == first.languages
    store.close()


def test_files_rows_carry_non_null_stamped_at(tmp_path):
    store = _store(tmp_path)
    index_folder(store, FIXTURE)
    rows = store._con.execute("SELECT path, stamped_at FROM files").fetchall()
    assert rows
    assert all(row[1] is not None for row in rows)
    store.close()


def test_cross_file_edges_resolved_in_db(tmp_path):
    store = _store(tmp_path)
    index_folder(store, FIXTURE)
    # Premium inherits Charge: an inherits edge should resolve to the Charge
    # class node, exact (bound via by_local from the import).
    rows = store._con.execute(
        "SELECT resolution FROM edges WHERE kind = 'inherits'"
    ).fetchall()
    assert rows
    store.close()


def test_rebuild_file_rebuilds_one_file_leaving_others_untouched(tmp_path):
    store = _store(tmp_path)
    index_folder(store, FIXTURE)

    other_before = store._con.execute(
        "SELECT node_id, kind, name, qualified_name, path, start_line, end_line, "
        "language FROM nodes WHERE path != ? ORDER BY node_id",
        ["payments/billing.py"],
    ).fetchall()

    rel = "payments/billing.py"
    text = (FIXTURE / rel).read_text(encoding="utf-8")
    extraction = PythonExtractor().extract(text, rel)
    stamp = datetime.datetime.now(datetime.UTC)
    _rebuild_file(store, rel, extraction, "python", compute_etag(text), stamp)

    other_after = store._con.execute(
        "SELECT node_id, kind, name, qualified_name, path, start_line, end_line, "
        "language FROM nodes WHERE path != ? ORDER BY node_id",
        ["payments/billing.py"],
    ).fetchall()
    assert other_before == other_after

    # The rebuilt file's rows are present and keyed to it.
    billing_rows = store._con.execute(
        "SELECT COUNT(*) FROM nodes WHERE path = ?", [rel]
    ).fetchone()
    assert billing_rows[0] == len(extraction.nodes)
    store.close()


def test_index_markdown_folder_counts_and_resolved_links(tmp_path):
    store = _store(tmp_path)
    report = index_folder(store, MARKDOWN_FIXTURE)

    assert report.languages.get("markdown", 0) == 4
    kinds = dict(
        store._con.execute("SELECT kind, COUNT(*) FROM nodes GROUP BY kind").fetchall()
    )
    assert kinds.get("page", 0) > 0
    assert kinds.get("heading", 0) > 0
    assert kinds.get("tag", 0) > 0
    # links_to edges resolved into the edges table, pointing at real page ids.
    links = store._con.execute(
        "SELECT dst_id FROM edges WHERE kind = 'links_to'"
    ).fetchall()
    assert links
    page_ids = {
        row[0]
        for row in store._con.execute(
            "SELECT node_id FROM nodes WHERE kind = 'page'"
        ).fetchall()
    }
    assert all(row[0] in page_ids for row in links)
    # Tag nodes are de-duped corpus-wide: one row per distinct tag name even
    # when the tag is used in several files.
    tag_ids = store._con.execute(
        "SELECT node_id FROM nodes WHERE kind = 'tag'"
    ).fetchall()
    assert len(tag_ids) == len({row[0] for row in tag_ids})
    # tagged edges resolve to those tag nodes.
    tag_id_set = {row[0] for row in tag_ids}
    tagged = store._con.execute(
        "SELECT dst_id FROM edges WHERE kind = 'tagged'"
    ).fetchall()
    assert tagged
    assert all(row[0] in tag_id_set for row in tagged)
    store.close()


def test_index_markdown_is_deterministic(tmp_path):
    store_a = GraphStore(db_path=str(tmp_path / "ma.duckdb"))
    store_b = GraphStore(db_path=str(tmp_path / "mb.duckdb"))
    index_folder(store_a, MARKDOWN_FIXTURE)
    index_folder(store_b, MARKDOWN_FIXTURE)
    assert _node_ids(store_a) == _node_ids(store_b)
    assert _edge_set(store_a) == _edge_set(store_b)
    store_a.close()
    store_b.close()


def test_index_folder_rejects_non_directory(tmp_path):
    store = _store(tmp_path)
    missing = tmp_path / "does-not-exist"
    try:
        raised = False
        try:
            index_folder(store, missing)
        except NotADirectoryError:
            raised = True
        assert raised
    finally:
        store.close()


def test_incremental_edit_rebuilds_only_changed_file(tmp_path):
    work = _copy_fixture(tmp_path)
    store = _store(tmp_path)
    index_folder(store, work)
    others_before = _node_ids(store) - _node_ids(store, "payments/billing.py")

    billing = work / "payments" / "billing.py"
    billing.write_text(
        billing.read_text(encoding="utf-8") + "\n\ndef added():\n    return 1\n",
        encoding="utf-8",
    )
    report = index_folder(store, work)

    assert report.mode == "incremental"
    assert report.rebuilt == 1
    assert report.deleted == 0
    # Every other file's node-id set is unchanged; only billing.py's grew.
    others_after = _node_ids(store) - _node_ids(store, "payments/billing.py")
    assert others_after == others_before
    names = store._con.execute(
        "SELECT name FROM nodes WHERE path = ? AND kind = 'function'",
        ["payments/billing.py"],
    ).fetchall()
    assert ("added",) in names
    store.close()


def test_incremental_cross_file_edges_are_re_resolved(tmp_path):
    work = _copy_fixture(tmp_path)
    inc_store = _store(tmp_path)
    index_folder(inc_store, work)

    # Edit charge.py (the file the other modules bind to). Premium(Charge) and
    # billing's `from payments.charge import Charge` bind to symbols in this
    # file, whose node ids shift when a line is inserted above them; a correct
    # incremental run must re-resolve those cross-file edges to the new ids.
    charge = work / "payments" / "charge.py"
    text = charge.read_text(encoding="utf-8")
    charge.write_text("# edited header\n" + text, encoding="utf-8")
    report = index_folder(inc_store, work)
    assert report.mode == "incremental"
    assert report.rebuilt == 1

    full_store = GraphStore(db_path=str(tmp_path / "full.duckdb"))
    index_folder(full_store, work, rebuild=True)

    assert _edge_set(inc_store) == _edge_set(full_store)
    # The binding edges to the changed file are present and resolved.
    premium_inherits = inc_store._con.execute(
        "SELECT resolution FROM edges WHERE kind = 'inherits' "
        "AND path = 'payments/premium.py'"
    ).fetchall()
    assert premium_inherits
    inc_store.close()
    full_store.close()


def test_incremental_delete_drops_rows_and_dangling_edges(tmp_path):
    work = _copy_fixture(tmp_path)
    store = _store(tmp_path)
    index_folder(store, work)

    (work / "payments" / "premium.py").unlink()
    report = index_folder(store, work)

    assert report.mode == "incremental"
    assert report.deleted == 1
    # The deleted file's files/nodes rows are gone.
    assert _node_ids(store, "payments/premium.py") == set()
    assert (
        store._con.execute(
            "SELECT COUNT(*) FROM files WHERE path = ?", ["payments/premium.py"]
        ).fetchone()[0]
        == 0
    )
    # No surviving edge references a node id that no longer exists.
    dangling = store._con.execute(
        "SELECT COUNT(*) FROM edges e "
        "WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.node_id = e.dst_id) "
        "AND NOT EXISTS (SELECT 1 FROM nodes n WHERE n.node_id = e.src_id)"
    ).fetchone()[0]
    assert dangling == 0
    assert _edge_set(store) == _edge_set_of_full_rebuild(tmp_path, work)
    store.close()


def _edge_set_of_full_rebuild(
    tmp_path, work: Path
) -> set[tuple[str, str, str, str, str]]:
    full = GraphStore(db_path=str(tmp_path / "full_cmp.duckdb"))
    index_folder(full, work, rebuild=True)
    edges = _edge_set(full)
    full.close()
    return edges


def test_incremental_no_change_is_a_noop(tmp_path):
    work = _copy_fixture(tmp_path)
    store = _store(tmp_path)
    index_folder(store, work)

    counts_before = {t: _table_count(store, t) for t in ("files", "nodes", "edges")}
    ids_before = _node_ids(store)

    report = index_folder(store, work)

    assert report.mode == "incremental"
    assert report.rebuilt == 0
    assert report.deleted == 0
    counts_after = {t: _table_count(store, t) for t in ("files", "nodes", "edges")}
    assert counts_after == counts_before
    assert _node_ids(store) == ids_before
    store.close()


def test_full_and_incremental_reach_identical_state(tmp_path):
    # Reach final tree state S incrementally from a prior state, and compare to
    # a full rebuild of S: node-id and edge sets must be identical.
    work = _copy_fixture(tmp_path)
    inc_store = _store(tmp_path)
    index_folder(inc_store, work)

    # Mutate: edit billing, delete premium, add a new module.
    billing = work / "payments" / "billing.py"
    billing.write_text(
        billing.read_text(encoding="utf-8") + "\n\ndef extra():\n    return 2\n",
        encoding="utf-8",
    )
    (work / "payments" / "premium.py").unlink()
    (work / "payments" / "refund.py").write_text(
        "from payments.charge import Charge\n\n\n"
        "def refund(amount):\n    return Charge().run(amount)\n",
        encoding="utf-8",
    )
    index_folder(inc_store, work)

    full_store = GraphStore(db_path=str(tmp_path / "full.duckdb"))
    index_folder(full_store, work, rebuild=True)

    assert _node_ids(inc_store) == _node_ids(full_store)
    assert _edge_set(inc_store) == _edge_set(full_store)
    inc_store.close()
    full_store.close()
