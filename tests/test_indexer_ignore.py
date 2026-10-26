"""The indexer ignore rules: default dir/size exclusions and .gitignore.

Every tree is built under ``tmp_path`` (never a committed fixture), because a
committed ``.git`` directory is unstageable and a committed fixture
``.gitignore`` would make git itself drop the very files these tests need. The
Markdown-corpus case reuses the committed ``ignore-markdown`` pages and adds a
sibling ``.git`` at test time for the same reason.
"""

import shutil
from pathlib import Path

from duckdb_graph.indexer import _DEFAULT_MAX_FILE_BYTES, index_folder
from duckdb_graph.store import GraphStore

MARKDOWN_CORPUS = Path(__file__).parent / "fixtures" / "ignore-markdown"


def _store(tmp_path) -> GraphStore:
    return GraphStore(db_path=str(tmp_path / "graph.duckdb"))


def _node_paths(store: GraphStore) -> set[str]:
    rows = store._con.execute("SELECT DISTINCT path FROM nodes").fetchall()
    return {row[0] for row in rows}


def _build_tree(root: Path, *, oversized: bool = True) -> None:
    """Write an exclusion fixture tree: a normal source file plus every kind
    of excluded entry (dependency/build/vcs dirs, an optional oversized file,
    and ``.gitignore``-matched paths).

    ``oversized`` adds a >1 MB ``huge.py``. It is only meaningful when the
    default size cap is active: with ``use_default_ignores=False`` the cap is
    off, so the oversized file would be fully parsed — slow and irrelevant to
    that path — hence the no-default test omits it.
    """
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "class App:\n"
        "    def run(self) -> int:\n"
        "        return self.helper()\n\n"
        "    def helper(self) -> int:\n"
        "        return 1\n\n\n"
        "def main() -> int:\n"
        "    return App().run()\n",
        encoding="utf-8",
    )
    # Dependency / build / cache / vcs directories pruned by default.
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "vendored.js").write_text(
        "export function vendored() { return 42; }\n", encoding="utf-8"
    )
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "dep.py").write_text(
        "def dependency() -> int:\n    return 7\n", encoding="utf-8"
    )
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    # An oversized file (strictly over the 1 MB cap) dropped by default.
    if oversized:
        (root / "huge.py").write_text(
            "x = 1\n" + "# pad\n" * (_DEFAULT_MAX_FILE_BYTES // 6 + 1),
            encoding="utf-8",
        )
    # .gitignore-matched paths, honored even with --no-default-ignores.
    (root / "secret.py").write_text(
        'def secret() -> str:\n    return "token"\n', encoding="utf-8"
    )
    (root / "ignored_dir").mkdir()
    (root / "ignored_dir" / "inside.py").write_text(
        "def inside() -> int:\n    return 11\n", encoding="utf-8"
    )
    (root / ".gitignore").write_text("secret.py\nignored_dir/\n", encoding="utf-8")


def test_default_ignores_exclude_dep_trees_oversized_and_gitignored(tmp_path):
    tree = tmp_path / "tree"
    _build_tree(tree)
    store = _store(tmp_path)
    index_folder(store, tree)

    paths = _node_paths(store)
    # The normal project source IS indexed.
    assert "src/app.py" in paths
    # No node comes from a pruned dependency/build/vcs tree, the oversized
    # file, or a .gitignore-matched path.
    assert not any("node_modules/" in p for p in paths)
    assert not any(p.startswith(".venv/") for p in paths)
    assert not any(p.startswith(".git/") for p in paths)
    assert "huge.py" not in paths
    assert "secret.py" not in paths
    assert not any(p.startswith("ignored_dir/") for p in paths)
    store.close()


def test_no_default_ignores_reincludes_deps_but_still_honors_gitignore(tmp_path):
    tree = tmp_path / "tree"
    _build_tree(tree, oversized=False)
    store = _store(tmp_path)
    index_folder(store, tree, use_default_ignores=False)

    paths = _node_paths(store)
    # The dependency trees are re-included now that defaults are off.
    assert any("node_modules/" in p for p in paths)
    assert any(p.startswith(".venv/") for p in paths)
    # The normal source is still there.
    assert "src/app.py" in paths
    # .gitignore is STILL honored regardless of the flag.
    assert "secret.py" not in paths
    assert not any(p.startswith("ignored_dir/") for p in paths)
    store.close()


def test_markdown_corpus_indexed_while_sibling_git_is_excluded(tmp_path):
    tree = tmp_path / "corpus"
    shutil.copytree(MARKDOWN_CORPUS, tree)
    # A sibling .git with a plausible text file; must contribute zero nodes.
    (tree / ".git").mkdir()
    (tree / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

    store = _store(tmp_path)
    report = index_folder(store, tree)

    assert report.languages.get("markdown", 0) == 2
    paths = _node_paths(store)
    assert "index.md" in paths
    assert "guide.md" in paths
    assert not any(p.startswith(".git/") for p in paths)
    store.close()


def test_nested_gitignore_is_honored(tmp_path):
    """A .gitignore in a SUBDIRECTORY excludes its matches, not only a root one.

    This is the ~/Public leak: indexing a parent of a repo whose own .gitignore
    ignores a build-cache dir. The parent has no rule for it, so only a nested
    .gitignore can exclude it. The old single-root spec missed this entirely.
    """
    root = tmp_path / "root"
    # Sub-repo A: its own .gitignore excludes cache/, which root does not know.
    repo_a = root / "repo_a"
    (repo_a / "cache").mkdir(parents=True)
    (repo_a / ".gitignore").write_text("cache/\n")
    (repo_a / "app.py").write_text("def app():\n    return 1\n")
    (repo_a / "cache" / "junk.py").write_text("def junk():\n    return 0\n")
    # Sub-repo B: a different nested ignore (a specific file by name).
    repo_b = root / "repo_b"
    repo_b.mkdir()
    (repo_b / ".gitignore").write_text("secret.py\n")
    (repo_b / "lib.py").write_text("def lib():\n    return 2\n")
    (repo_b / "secret.py").write_text("TOKEN = 1\n")
    # Root has no .gitignore at all.

    store = _store(tmp_path)
    try:
        index_folder(store, root)
        paths = _node_paths(store)
    finally:
        store.close()

    # Nested-ignored files are excluded...
    assert not any("cache/junk.py" in p for p in paths), paths
    assert not any("secret.py" in p for p in paths), paths
    # ...while the real source in each sub-repo is kept.
    assert any(p.endswith("repo_a/app.py") for p in paths), paths
    assert any(p.endswith("repo_b/lib.py") for p in paths), paths


def test_nested_duckdbgraphignore_is_honored(tmp_path):
    """A nested .duckdbgraphignore is honored like a nested .gitignore."""
    root = tmp_path / "root"
    sub = root / "pkg"
    (sub / "generated").mkdir(parents=True)
    (sub / ".duckdbgraphignore").write_text("generated/\n")
    (sub / "real.py").write_text("def real():\n    return 1\n")
    (sub / "generated" / "gen.py").write_text("def gen():\n    return 0\n")

    store = _store(tmp_path)
    try:
        index_folder(store, root)
        paths = _node_paths(store)
    finally:
        store.close()

    assert not any("generated/gen.py" in p for p in paths), paths
    assert any(p.endswith("pkg/real.py") for p in paths), paths
