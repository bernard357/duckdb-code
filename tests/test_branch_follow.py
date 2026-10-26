"""Branch-follow tests over a hermetic throwaway git repo (no network).

Builds a local git repo with two branches whose trees differ, indexes on one,
switches, re-indexes, and asserts the graph and the reported branch/HEAD follow
the switch. Uses plumbing via subprocess against a temp repo; skipped if git is
unavailable.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from duckdb_graph.indexer import _git_revision
from duckdb_graph.store import GraphStore

git = shutil.which("git")
pytestmark = pytest.mark.skipif(git is None, reason="git not available")


def _run(repo: Path, *args: str) -> None:
    subprocess.run(
        [git, *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-b", "main")
    (repo / "common.py").write_text("def common():\n    return 0\n")
    (repo / "only_main.py").write_text("def only_main():\n    return 1\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "main tree")
    _run(repo, "checkout", "-b", "feature")
    (repo / "only_main.py").unlink()
    (repo / "only_feature.py").write_text("def only_feature():\n    return 2\n")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", "feature tree")
    _run(repo, "checkout", "main")
    return repo


def _functions(db: str) -> set[str]:
    store = GraphStore(db_path=db)
    try:
        return {
            r[0]
            for r in store._con.execute(
                "SELECT name FROM nodes WHERE kind = 'function'"
            ).fetchall()
        }
    finally:
        store.close()


def test_index_records_branch_and_head(tmp_path):
    repo = _init_repo(tmp_path)
    branch, head = _git_revision(repo)
    assert branch == "main"
    assert head and len(head) >= 7

    db = str(tmp_path / "g.duckdb")
    store = GraphStore(db_path=db)
    try:
        store.index(repo)
        report = store.report()
    finally:
        store.close()
    assert report.branch == "main"
    assert report.head_commit == head
    assert "only_main" in _functions(db)
    assert "only_feature" not in _functions(db)


def test_reindex_follows_branch_switch(tmp_path):
    repo = _init_repo(tmp_path)
    db = str(tmp_path / "g.duckdb")

    store = GraphStore(db_path=db)
    try:
        store.index(repo)
    finally:
        store.close()
    assert "only_main" in _functions(db)

    _run(repo, "checkout", "feature")

    store = GraphStore(db_path=db)
    try:
        store.index(repo)  # incremental; etag diff reconciles the switch
        report = store.report()
    finally:
        store.close()

    funcs = _functions(db)
    assert "only_feature" in funcs  # file added on the branch appears
    assert "only_main" not in funcs  # file removed on the branch is gone
    assert report.branch == "feature"


def test_non_git_folder_is_a_noop(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "mod.py").write_text("def f():\n    return 1\n")
    assert _git_revision(plain) == (None, None)
    db = str(tmp_path / "g.duckdb")
    store = GraphStore(db_path=db)
    try:
        store.index(plain)
        report = store.report()
    finally:
        store.close()
    assert report.branch is None
    assert report.head_commit is None
