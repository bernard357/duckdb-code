"""Watch a folder and re-index incrementally when its files change.

A thin trigger loop around the existing incremental indexer: it does not add
any extraction or resolution logic. ``watchfiles`` coalesces a burst of
filesystem events into one batch (debounced), the loop drops events under the
same directories the indexer ignores (so a ``node_modules`` install storm never
triggers a rebuild), and any surviving change runs
``index_folder(..., rebuild=False)`` — the etag diff reconciles only the files
that actually changed. Runs are serialized by construction: the loop processes
one batch at a time, so events arriving mid-run are picked up on the next batch.

``watchfiles`` is imported lazily inside :func:`watch` so the dependency is only
loaded when watching, keeping ``import duckdb_graph`` light.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from duckdb_graph.indexer import _DEFAULT_IGNORED_DIRS, IndexReport, index_folder
from duckdb_graph.store import GraphStore


def _is_relevant_change(
    changed_paths: set[str], folder: Path, *, use_default_ignores: bool
) -> bool:
    """Return ``True`` when at least one changed path is worth re-indexing.

    A change is ignored when it falls under one of the default-excluded
    directories (``.git``, ``node_modules``, ``.venv``, caches, …) — the same
    set the indexer prunes — so churn in those trees does not trigger a rebuild.
    When ``use_default_ignores`` is ``False`` every change is relevant, matching
    the indexer's own flag. ``.gitignore`` patterns are not re-checked here: the
    re-index itself honors them, so a gitignored file that slips through merely
    triggers a no-op reconcile, never a wrong edge.
    """
    if not use_default_ignores:
        return bool(changed_paths)
    for raw in changed_paths:
        try:
            rel_parts = Path(raw).resolve().relative_to(folder.resolve()).parts
        except ValueError:
            # Outside the watched folder (rare); treat as relevant.
            return True
        if not any(part in _DEFAULT_IGNORED_DIRS for part in rel_parts):
            return True
    return False


def watch(
    store: GraphStore,
    folder: str | Path,
    *,
    recursive: bool = True,
    use_default_ignores: bool = True,
    on_reindex: Callable[[IndexReport], None] | None = None,
    _changes: Iterator[set[str]] | None = None,
) -> None:
    """Watch ``folder`` and incrementally re-index on each relevant change batch.

    Blocks until interrupted (the underlying watcher stops on ``KeyboardInterrupt``
    / a stop signal). ``on_reindex`` is called with the :class:`IndexReport` after
    each re-index, for progress output. ``_changes`` injects a change-batch
    iterator in tests so the loop can be driven without a real filesystem
    watcher or any wall-clock wait; in production it defaults to
    ``watchfiles.watch``.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder}")

    if _changes is None:
        from watchfiles import watch as _wf_watch

        # watchfiles yields a set of (Change, path) tuples per debounced batch;
        # reduce to the set of paths. recursive is always on for watchfiles;
        # the relevance filter + indexer handle scoping.
        def _iter() -> Iterator[set[str]]:
            for batch in _wf_watch(folder):
                yield {path for _change, path in batch}

        changes: Iterator[set[str]] = _iter()
    else:
        changes = _changes

    for changed_paths in changes:
        if not _is_relevant_change(
            changed_paths, folder, use_default_ignores=use_default_ignores
        ):
            continue
        report = index_folder(
            store,
            folder,
            recursive=recursive,
            rebuild=False,
            use_default_ignores=use_default_ignores,
        )
        if on_reindex is not None:
            on_reindex(report)
