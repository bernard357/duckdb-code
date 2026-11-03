"""Git co-change enrichment (issue #26 / #31 data source).

Builds a real tiny git repo so the `git log` path is genuinely exercised, and
asserts `file_changes_with` edges and per-file churn appear. Skips when git is
unavailable so the suite still runs offline.
"""

import os
import shutil
import subprocess
import textwrap

import pytest

from duckdb_code.indexer import index_folder
from duckdb_code.store import CodeStore

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(args, cwd):
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=_GIT_ENV,
    )


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def test_co_change_edges_and_churn(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init"], repo)
    a = repo / "a.py"
    b = repo / "b.py"
    c = repo / "c.py"
    _write(a, "def fa():\n    return 1\n")
    _write(b, "def fb():\n    return 2\n")
    _write(c, "def fc():\n    return 3\n")

    # Two commits that change a.py and b.py together (coupling), plus one that
    # changes c.py alone. a<->b should earn a co-change edge; c should not.
    _git(["add", "a.py", "b.py"], repo)
    _git(["commit", "-m", "ab one"], repo)
    _write(a, "def fa():\n    return 10\n")
    _write(b, "def fb():\n    return 20\n")
    _git(["add", "a.py", "b.py"], repo)
    _git(["commit", "-m", "ab two"], repo)
    _write(c, "def fc():\n    return 30\n")
    _git(["add", "c.py"], repo)
    _git(["commit", "-m", "c alone"], repo)

    store = CodeStore(db_path=str(tmp_path / "cc.duckdb"), root=str(repo))
    index_folder(store, repo)
    con = store._con

    cc = con.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'file_changes_with'"
    ).fetchone()[0]
    assert cc >= 1  # a.py <-> b.py co-changed twice

    # Churn: a.py and b.py touched by two commits, c.py by one.
    churn = dict(con.execute("SELECT path, churn FROM files").fetchall())
    assert churn.get("a.py") == 2
    assert churn.get("b.py") == 2
    assert churn.get("c.py") == 1
    store.close()


def test_co_change_degrades_without_git_repo(tmp_path):
    # A plain (non-git) folder adds no co-change edges and leaves churn zero,
    # and the index still builds.
    src = tmp_path / "plain"
    src.mkdir()
    _write(src / "m.py", "def m():\n    return 1\n")
    store = CodeStore(db_path=str(tmp_path / "plain.duckdb"), root=str(src))
    report = index_folder(store, src)
    assert report.nodes > 0  # index built fine
    con = store._con
    cc = con.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'file_changes_with'"
    ).fetchone()[0]
    assert cc == 0
    assert con.execute("SELECT MAX(churn) FROM files").fetchone()[0] == 0
    store.close()


def test_history_returns_commits_co_change_and_churn(tmp_path):
    repo = tmp_path / "hist"
    repo.mkdir()
    _git(["init"], repo)
    a = repo / "a.py"
    b = repo / "b.py"
    _write(a, "def fa():\n    return 1\n")
    _write(b, "def fb():\n    return 2\n")
    _git(["add", "a.py", "b.py"], repo)
    _git(["commit", "-m", "first ab"], repo)
    _write(a, "def fa():\n    return 10\n")
    _write(b, "def fb():\n    return 20\n")
    _git(["add", "a.py", "b.py"], repo)
    _git(["commit", "-m", "second ab"], repo)

    store = CodeStore(db_path=str(tmp_path / "hist.duckdb"), root=str(repo))
    index_folder(store, repo)
    result = store.history("a.fa")
    assert result.path == "a.py"
    assert result.churn == 2
    # Two commits touched a.py, newest first.
    assert len(result.commits) == 2
    assert result.commits[0].summary == "second ab"
    assert all(c.sha for c in result.commits)
    # b.py is a co-change partner of a.py.
    partners = {p.path for p in result.co_change}
    assert "b.py" in partners
    store.close()


def test_history_empty_without_git(tmp_path):
    src = tmp_path / "plain2"
    src.mkdir()
    _write(src / "m.py", "def m():\n    return 1\n")
    store = CodeStore(db_path=str(tmp_path / "plain2.duckdb"), root=str(src))
    index_folder(store, src)
    result = store.history("m.m")
    assert result.resolved  # the symbol resolves
    assert result.commits == []
    assert result.co_change == []
    assert result.churn == 0
    store.close()


def test_co_change_uses_meta_root_not_just_settings(tmp_path):
    # Regression: the CLI opens the store WITHOUT a settings.root, so the
    # co-change pass must read the indexed folder from meta.root (written by the
    # index) rather than settings.root — otherwise CLI-built indexes get no
    # co-change or history.
    repo = tmp_path / "metaroot"
    repo.mkdir()
    _git(["init"], repo)
    a = repo / "a.py"
    b = repo / "b.py"
    _write(a, "def fa():\n    return 1\n")
    _write(b, "def fb():\n    return 2\n")
    _git(["add", "a.py", "b.py"], repo)
    _git(["commit", "-m", "ab"], repo)
    _write(a, "def fa():\n    return 9\n")
    _write(b, "def fb():\n    return 9\n")
    _git(["add", "a.py", "b.py"], repo)
    _git(["commit", "-m", "ab2"], repo)

    # No root= on the constructor, mirroring the CLI; pass the folder to index.
    store = CodeStore(db_path=str(tmp_path / "metaroot.duckdb"))
    index_folder(store, repo)
    con = store._con
    assert con.execute("SELECT COUNT(*) FROM commits").fetchone()[0] > 0
    assert con.execute("SELECT MAX(churn) FROM files").fetchone()[0] == 2
    store.close()


def _commit(repo, files, message):
    """Rewrite each file with a fresh body, then commit them together."""
    for name in files:
        path = repo / name
        body = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(f"{body}# {message}\n", encoding="utf-8")
    _git(["add", *files], repo)
    _git(["commit", "-q", "-m", message], repo)


def _workspace(tmp_path):
    """Repo `alpha`: a.py with b.py twice, with c.py once, alone once.

    Repo `beta` holds a stronger pair (x.py with y.py three times), which
    used to scale alpha's counts because coupling was normalized across the
    whole index.
    """
    root = tmp_path / "ws"
    alpha = root / "alpha"
    beta = root / "beta"
    for repo in (alpha, beta):
        repo.mkdir(parents=True)
        _git(["init", "-q"], repo)
    _commit(alpha, ["a.py", "b.py"], "ab one")
    _commit(alpha, ["a.py", "b.py"], "ab two")
    _commit(alpha, ["a.py", "c.py"], "ac")
    _commit(alpha, ["a.py"], "a alone")
    for n in range(3):
        _commit(beta, ["x.py", "y.py"], f"xy {n}")
    store = CodeStore(db_path=str(tmp_path / "ws.duckdb"), root=str(root))
    index_folder(store, root)
    return store


def test_history_reports_the_real_number_of_shared_commits(tmp_path):
    store = _workspace(tmp_path)
    try:
        result = store.history("alpha/a.py")
        beta = store.history("beta/x.py")
    finally:
        store.close()
    assert result.churn == 4
    # b.py shares 2 of a.py's 4 commits; c.py only 1, under the 2-commit floor.
    # The old proxy, round(2 / 3 * 4), would have said 3.
    assert [(p.path, p.together_count) for p in result.co_change] == [("alpha/b.py", 2)]
    assert [(p.path, p.together_count) for p in beta.co_change] == [("beta/y.py", 3)]


def test_coupling_is_normalized_per_repository(tmp_path):
    store = _workspace(tmp_path)
    try:
        rows = store._con.execute(
            """
            SELECT s.path, d.path, e.confidence
            FROM edges e
            JOIN nodes s ON s.node_id = e.src_id
            JOIN nodes d ON d.node_id = e.dst_id
            WHERE e.kind = 'file_changes_with'
            ORDER BY s.path
            """
        ).fetchall()
    finally:
        store.close()
    # Each repository's strongest pair is 1.0, whatever the other repository
    # holds (alpha's pair used to read 2/3 next to beta's).
    assert rows == [("alpha/a.py", "alpha/b.py", 1.0), ("beta/x.py", "beta/y.py", 1.0)]


def test_markdown_pages_take_part_in_co_change(tmp_path):
    # A Markdown file has a `page` node, not a `file` node; it must still get
    # commits, churn, and co-change partners, in both directions.
    repo = tmp_path / "docs"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _commit(repo, ["store.py", "CHANGES.md"], "one")
    _commit(repo, ["store.py", "CHANGES.md"], "two")
    _commit(repo, ["store.py"], "three")
    store = CodeStore(db_path=str(tmp_path / "docs.duckdb"), root=str(repo))
    index_folder(store, repo)
    try:
        code = store.history("store.py")
        page = store.history("CHANGES.md")
    finally:
        store.close()
    assert [(p.path, p.together_count) for p in code.co_change] == [("CHANGES.md", 2)]
    assert page.path == "CHANGES.md"
    assert page.churn == 2
    assert [c.summary for c in page.commits] == ["two", "one"]
    assert [(p.path, p.together_count) for p in page.co_change] == [("store.py", 2)]
