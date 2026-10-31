"""Tests for the ``index_status`` freshness tool.

Builds a small git repository, indexes it, and asserts ``index_status`` reports
the indexed commit, branch, counts, an ``indexed_at`` stamp, and a ``dirty``
flag that tracks the real working-tree state (clean right after commit, dirty
after an edit). A non-git tree reports ``dirty=None`` (unknown).
"""

import subprocess
import textwrap

from duckdb_code.indexer import index_folder
from duckdb_code.store import CodeStore


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _git(tree, *args):
    subprocess.run(
        ["git", "-C", str(tree), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _git_repo(tmp_path):
    """Create a committed git repo with one Python file and return its path."""
    tree = tmp_path / "repo"
    tree.mkdir()
    _write(
        tree / "mod.py",
        """
        def thing():
            return 1
        """,
    )
    _git(tree, "init", "-q", "-b", "main")
    _git(tree, "config", "user.email", "t@example.com")
    _git(tree, "config", "user.name", "Test")
    _git(tree, "add", "mod.py")
    _git(tree, "commit", "-q", "-m", "initial")
    return tree


def test_index_status_reports_commit_counts_and_clean_tree(tmp_path):
    tree = _git_repo(tmp_path)
    store = CodeStore(db_path=str(tmp_path / "status.duckdb"), root=str(tree))
    index_folder(store, tree)
    try:
        status = store.index_status()
    finally:
        store.close()
    assert status.branch == "main"
    assert status.indexed_commit  # a short sha was recorded
    assert status.node_count > 0
    assert status.edge_count >= 0
    assert status.indexed_at is not None
    # Nothing changed since the commit that was just indexed.
    assert status.dirty is False


def test_index_status_detects_a_dirty_working_tree(tmp_path):
    tree = _git_repo(tmp_path)
    store = CodeStore(db_path=str(tmp_path / "status.duckdb"), root=str(tree))
    index_folder(store, tree)
    # Edit a tracked file after indexing — the working tree is now dirty.
    _write(tree / "mod.py", "def thing():\n    return 2\n")
    try:
        status = store.index_status()
    finally:
        store.close()
    assert status.dirty is True


def test_index_status_unknown_dirty_outside_git(tmp_path):
    tree = tmp_path / "plain"
    tree.mkdir()
    _write(tree / "mod.py", "def thing():\n    return 1\n")
    store = CodeStore(db_path=str(tmp_path / "status.duckdb"), root=str(tree))
    index_folder(store, tree)
    try:
        status = store.index_status()
    finally:
        store.close()
    # Not a git repo -> dirty is unknown, never reported as clean.
    assert status.dirty is None
    assert status.indexed_commit is None
    assert status.node_count > 0
