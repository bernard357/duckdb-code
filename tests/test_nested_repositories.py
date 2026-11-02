"""Indexing a folder that holds several git repositories but is not one.

Builds a root with two real git repositories (`alpha`, a `src/` layout, and
`beta`, a flat layout) next to a plain folder, then checks that the git-backed
signals are gathered per repository (co-change, churn, `history`,
`index_status`, `context_for_change`) and that Python imports bind by their
import name within each repository. Skips when git is unavailable.
"""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, env=_GIT_ENV
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _commit_together(repo: Path, files: dict[str, str], message: str) -> None:
    for rel, text in files.items():
        _write(repo / rel, text)
    _git(repo, "add", *files)
    _git(repo, "commit", "-q", "-m", message)


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    alpha = root / "alpha"
    beta = root / "beta"
    for repo in (alpha, beta):
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", "-b", "main")
    # alpha: store.py and models.py change together twice.
    for n in (1, 2):
        _commit_together(
            alpha,
            {
                "src/alpha/__init__.py": "",
                "src/alpha/models.py": f"class Item:\n    size = {n}\n",
                "src/alpha/store.py": (
                    "from alpha.models import Item\n\n\n"
                    f"class Store:\n    def get(self):\n        return Item()  # {n}\n"
                ),
            },
            f"alpha {n}",
        )
    _git(alpha, "branch", "feature")
    # beta: app.py and config.py change together twice.
    for n in (1, 2):
        _commit_together(
            beta,
            {
                "beta/__init__.py": "",
                "beta/config.py": f"class Settings:\n    level = {n}\n",
                "beta/app.py": (
                    "from beta.config import Settings\n\n\n"
                    f"def main():\n    return Settings()  # {n}\n"
                ),
            },
            f"beta {n}",
        )
    _write(root / "notes" / "scratch.py", "def scratch():\n    return 0\n")
    return root


def _indexed(tmp_path: Path) -> tuple[Path, CodeStore]:
    root = _workspace(tmp_path)
    store = CodeStore(db_path=str(tmp_path / "ws.duckdb"), root=str(root))
    index_folder(store, root)
    return root, store


def test_history_and_churn_per_repository(tmp_path):
    _root, store = _indexed(tmp_path)
    try:
        alpha = store.history("alpha/src/alpha/store.py")
        beta = store.history("beta/beta/app.py")
        plain = store.history("notes/scratch.py")
    finally:
        store.close()
    assert [c.path for c in alpha.co_change] == ["alpha/src/alpha/models.py"]
    assert alpha.churn == 2 and len(alpha.commits) == 2
    assert [c.path for c in beta.co_change] == ["beta/beta/config.py"]
    assert beta.churn == 2
    # A file outside every repository has no git signals, and no error.
    assert plain.commits == [] and plain.churn == 0


def test_index_status_lists_every_repository(tmp_path):
    root, store = _indexed(tmp_path)
    try:
        clean = store.index_status()
        _write(root / "beta" / "beta" / "config.py", "class Settings:\n    level = 9\n")
        dirty = store.index_status()
    finally:
        store.close()
    assert [r.path for r in clean.repositories] == ["alpha", "beta"]
    assert all(r.branch == "main" and r.commit for r in clean.repositories)
    assert [r.dirty for r in clean.repositories] == [False, False]
    assert clean.dirty is False
    assert [r.dirty for r in dirty.repositories] == [False, True]
    assert dirty.dirty is True


def test_context_for_change_diffs_each_repository(tmp_path):
    root, store = _indexed(tmp_path)
    try:
        _write(
            root / "alpha" / "src" / "alpha" / "models.py",
            "class Item:\n    size = 7\n",
        )
        _write(root / "beta" / "beta" / "config.py", "class Settings:\n    level = 7\n")
        working = store.context_for_change()
        # `feature` exists only in alpha: beta is skipped, not an error.
        branch = store.context_for_change("feature")
    finally:
        store.close()
    changed = {c.node.qualified_name for c in working.changed}
    assert "alpha.src.alpha.models.Item" in changed
    assert "beta.beta.config.Settings" in changed
    branch_paths = {c.node.path for c in branch.changed}
    assert branch_paths and all(p.startswith("alpha/") for p in branch_paths)


def test_imports_bind_by_import_name_in_each_repository(tmp_path):
    _root, store = _indexed(tmp_path)
    try:
        alpha = store.impact("alpha.src.alpha.models.Item", depth=1)
        beta = store.impact("beta.beta.config.Settings", depth=1)
    finally:
        store.close()
    alpha_direct = {
        n.node.qualified_name for n in alpha.affected if not n.via_container
    }
    beta_direct = {n.node.qualified_name for n in beta.affected if not n.via_container}
    assert "alpha.src.alpha.store.Store.get" in alpha_direct
    assert "beta.beta.app.main" in beta_direct
    # Bound through the import, not guessed by name.
    assert all(n.resolution == "exact" for n in alpha.affected if not n.via_container)
