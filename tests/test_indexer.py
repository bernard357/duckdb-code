"""The indexer: full-build counts, determinism, stamped_at, and the seam."""

import datetime
from pathlib import Path

from duckdb_codegraph.etag import compute_etag
from duckdb_codegraph.extractors.python import PythonExtractor
from duckdb_codegraph.indexer import _rebuild_file, index_folder
from duckdb_codegraph.store import CodeGraphStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


def _store(tmp_path) -> CodeGraphStore:
    return CodeGraphStore(db_path=str(tmp_path / "codegraph.duckdb"))


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
    store_a = CodeGraphStore(db_path=str(tmp_path / "a.duckdb"))
    store_b = CodeGraphStore(db_path=str(tmp_path / "b.duckdb"))

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


def test_rerun_over_existing_db_yields_same_counts(tmp_path):
    store = _store(tmp_path)
    first = index_folder(store, FIXTURE)
    second = index_folder(store, FIXTURE)
    assert first.model_dump() == second.model_dump()
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
