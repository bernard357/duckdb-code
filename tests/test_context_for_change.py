"""Tests for the ``context_for_change`` review/PR primitive.

Builds a small git repository, indexes it, makes a change, and asserts
``context_for_change`` maps the diff's line ranges onto the touched symbols and
returns their one-hop dependency neighbourhood — both for an uncommitted
working-tree diff and for a branch diff. A non-git tree degrades to empty.
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


def _repo(tmp_path):
    """A committed repo: helper <- feature <- caller (a call chain)."""
    tree = tmp_path / "repo"
    tree.mkdir()
    _write(
        tree / "mod.py",
        """
        def helper():
            return 1

        def feature():
            return helper()

        def caller():
            return feature()
        """,
    )
    _git(tree, "init", "-q", "-b", "main")
    _git(tree, "config", "user.email", "t@example.com")
    _git(tree, "config", "user.name", "Test")
    _git(tree, "add", "mod.py")
    _git(tree, "commit", "-q", "-m", "initial")
    return tree


def _index(tmp_path, tree):
    store = CodeStore(db_path=str(tmp_path / "ctx.duckdb"), root=str(tree))
    index_folder(store, tree)
    return store


def test_working_tree_change_maps_to_symbol_and_neighbourhood(tmp_path):
    tree = _repo(tmp_path)
    store = _index(tmp_path, tree)
    # Edit the body of `feature` after indexing (uncommitted).
    _write(
        tree / "mod.py",
        """
        def helper():
            return 1

        def feature():
            return helper() + 1

        def caller():
            return feature()
        """,
    )
    try:
        result = store.context_for_change()
    finally:
        store.close()
    assert result.base == "working-tree"
    # feature is the touched symbol.
    changed = {c.node.qualified_name for c in result.changed}
    assert "mod.feature" in changed
    # Its one-hop neighbours: helper (out, feature calls it) and caller (in).
    relations = {n.node.qualified_name: n.relation for n in result.neighbourhood}
    assert relations.get("mod.helper") == "calls:out"
    assert relations.get("mod.caller") == "calls:in"
    # A changed symbol is never also listed as its own neighbour.
    assert "mod.feature" not in relations


def test_branch_diff_uses_the_named_base(tmp_path):
    tree = _repo(tmp_path)
    store = _index(tmp_path, tree)
    # Commit an edit on a feature branch; diff HEAD against main.
    _git(tree, "checkout", "-q", "-b", "feat")
    _write(
        tree / "mod.py",
        """
        def helper():
            return 1

        def feature():
            return helper() + 2

        def caller():
            return feature()
        """,
    )
    _git(tree, "commit", "-q", "-am", "tweak feature")
    try:
        result = store.context_for_change("main")
    finally:
        store.close()
    assert result.base == "main"
    assert "mod.feature" in {c.node.qualified_name for c in result.changed}


def test_non_git_tree_degrades_to_empty(tmp_path):
    tree = tmp_path / "plain"
    tree.mkdir()
    _write(tree / "mod.py", "def helper():\n    return 1\n")
    store = _index(tmp_path, tree)
    try:
        result = store.context_for_change()
    finally:
        store.close()
    # Not a git repo -> nothing to diff, but a well-formed empty result.
    assert result.base == "working-tree"
    assert result.changed == []
    assert result.neighbourhood == []
