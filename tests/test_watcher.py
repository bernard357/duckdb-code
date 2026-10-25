"""Watcher tests driven through the change-batch injection seam.

These never start a real filesystem watcher or wait on wall-clock time: the
watch loop takes an injected iterator of change batches, so a create/modify/
delete is simulated by editing a temp workspace and yielding the changed path.
"""

import shutil
from pathlib import Path

from duckdb_graph.store import GraphStore
from duckdb_graph.watcher import _is_relevant_change, watch

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


def _copy_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURE, ws)
    return ws


def test_watch_reindexes_on_a_relevant_change(tmp_path):
    """A single relevant change batch triggers exactly one incremental run."""
    ws = _copy_workspace(tmp_path)
    db = str(tmp_path / "w.duckdb")
    store = GraphStore(db_path=db)
    reports = []

    # Build the initial index first, as `duckdb-graph watch` does, so the
    # watcher's triggered run is a genuine incremental re-index.
    from duckdb_graph.indexer import index_folder

    index_folder(store, ws, rebuild=False)

    # Add a new module, then yield its path as the one change batch.
    new_file = ws / "payments" / "added.py"
    new_file.write_text("def added():\n    return 1\n")
    changes = iter([{str(new_file)}])

    try:
        watch(
            store,
            ws,
            use_default_ignores=True,
            on_reindex=reports.append,
            _changes=changes,
        )
    finally:
        store.close()

    assert len(reports) == 1  # exactly one re-index for the one batch
    report = reports[0]
    assert report.mode == "incremental"
    assert report.rebuilt >= 1
    # The new function is now in the graph.
    store = GraphStore(db_path=db, read_only=True)
    try:
        names = {
            row[0]
            for row in store._con.execute(
                "SELECT name FROM nodes WHERE kind = 'function'"
            ).fetchall()
        }
    finally:
        store.close()
    assert "added" in names


def test_watch_skips_ignored_directory_churn(tmp_path):
    """Changes under a default-ignored dir do not trigger a re-index."""
    ws = _copy_workspace(tmp_path)
    db = str(tmp_path / "w.duckdb")
    store = GraphStore(db_path=db)
    reports = []

    node_modules = ws / "node_modules"
    node_modules.mkdir()
    churn = node_modules / "dep.js"
    churn.write_text("export const x = 1;\n")
    changes = iter([{str(churn)}])

    try:
        watch(
            store,
            ws,
            use_default_ignores=True,
            on_reindex=reports.append,
            _changes=changes,
        )
    finally:
        store.close()

    assert reports == []  # node_modules churn triggered no re-index


def test_is_relevant_change_filters_ignored_dirs(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _is_relevant_change(
        {str(ws / "pkg" / "app.py")}, ws, use_default_ignores=True
    )
    assert not _is_relevant_change(
        {str(ws / "node_modules" / "dep.js")}, ws, use_default_ignores=True
    )
    assert not _is_relevant_change(
        {str(ws / ".git" / "HEAD")}, ws, use_default_ignores=True
    )
    # With default ignores off, even node_modules churn is relevant.
    assert _is_relevant_change(
        {str(ws / "node_modules" / "dep.js")}, ws, use_default_ignores=False
    )
