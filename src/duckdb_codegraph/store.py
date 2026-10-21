"""The code-graph store: one DuckDB file is one index.

This module owns the authoritative schema DDL (:data:`SCHEMA_DDL` and
:data:`INDEX_DDL`) so the indexer's table recreation and the store's
``_ensure_schema`` can never drift. FEAT-002 ships the construction, lifecycle,
and schema; the ``impact``/``explain``/``report`` query methods land in a later
feature.

The only DuckDB indexes are ordinary B-tree indexes on text/int columns plus
the two primary keys (``files.path``, ``meta.key``); there is no experimental
HNSW persistence, so a read-only open of a frozen file needs no special flag.
"""

import duckdb

from duckdb_codegraph.config import Settings

# Authoritative schema. Every graph row carries its originating file ``path``
# (the incremental seam): deleting ``WHERE path = ?`` across the three graph
# tables removes exactly one file's contribution. ``files.path`` and ``meta.key``
# carry primary keys because one-row-per-path / one-row-per-key are real
# invariants, enforced by the engine rather than by convention.
SCHEMA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS files (
        path        TEXT NOT NULL PRIMARY KEY,
        language    TEXT NOT NULL,
        etag        TEXT NOT NULL,
        stamped_at  TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS nodes (
        node_id        TEXT NOT NULL,
        kind           TEXT NOT NULL,
        name           TEXT NOT NULL,
        qualified_name TEXT NOT NULL,
        path           TEXT NOT NULL,
        start_line     INTEGER NOT NULL,
        end_line       INTEGER NOT NULL,
        language       TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edges (
        src_id     TEXT NOT NULL,
        dst_id     TEXT NOT NULL,
        kind       TEXT NOT NULL,
        path       TEXT NOT NULL,
        resolution TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT NOT NULL PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)

# Read-write-only indexes. No ``idx_files_path`` — the ``files.path`` primary
# key already provides the file-key lookup index.
INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_nodes_id   ON nodes (node_id)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_path ON nodes (path)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_qn   ON nodes (qualified_name)",
    "CREATE INDEX IF NOT EXISTS idx_edges_src  ON edges (src_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges (dst_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_path ON edges (path)",
)

SCHEMA_VERSION = 1


class ReadOnlyError(RuntimeError):
    """Raised when a write is attempted on a read-only store."""


class CodeGraphStore:
    """A DuckDB-backed code graph.

    ``read_only`` opens the file without allowing writes — the mode used when a
    frozen index is baked into an image. The schema and indexes are created only
    on a writable open.
    """

    def __init__(
        self,
        db_path: str | None = None,
        root: str | None = None,
        read_only: bool | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or Settings(
            db_path=db_path, root=root, read_only=read_only
        )
        self._read_only = self.settings.read_only
        self._con = duckdb.connect(self.settings.db_path, read_only=self._read_only)
        if not self._read_only:
            self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create the graph tables and the read-write indexes if absent."""
        for ddl in SCHEMA_DDL:
            self._con.execute(ddl)
        for ddl in INDEX_DDL:
            self._con.execute(ddl)

    def _guard_writable(self) -> None:
        if self._read_only:
            raise ReadOnlyError(
                "This store was opened read-only; writes are not allowed."
            )

    def close(self) -> None:
        """Close the underlying connection."""
        self._con.close()

    def __enter__(self) -> "CodeGraphStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
