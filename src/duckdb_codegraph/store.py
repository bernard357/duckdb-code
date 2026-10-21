"""The code-graph store: one DuckDB file is one index.

This module owns the authoritative schema DDL (:data:`SCHEMA_DDL` and
:data:`INDEX_DDL`) so the indexer's table recreation and the store's
``_ensure_schema`` can never drift. It also exposes the three read-only agentic
tools over the graph: :meth:`CodeGraphStore.impact` (transitive reverse
dependencies), :meth:`CodeGraphStore.explain` (one node's structural
neighbourhood), and :meth:`CodeGraphStore.report` (pure aggregation).

The only DuckDB indexes are ordinary B-tree indexes on text/int columns plus
the two primary keys (``files.path``, ``meta.key``); there is no experimental
HNSW persistence, so a read-only open of a frozen file needs no special flag.
"""

import logging
from pathlib import Path, PurePosixPath

import duckdb

from duckdb_codegraph.config import Settings
from duckdb_codegraph.extractors.registry import EXTENSION_LANGUAGE
from duckdb_codegraph.models import (
    Explanation,
    ExplanationResult,
    ImpactNode,
    ImpactResult,
    Node,
    Report,
)

logger = logging.getLogger(__name__)

# The reverse-dependency edge kinds the impact BFS and explain fan-in/fan-out
# traverse. ``defines`` is deliberately absent: it is a container-structure
# edge, read forward once by ``explain`` and walked in reverse only to seed a
# symbol's container chain in ``impact`` — never a dependency hop.
_DEPENDENCY_KINDS = ("imports", "calls", "inherits", "references")

# A target string is classified as a path (not a symbol) when it carries one of
# these registered source extensions. Kept in sync with the extractor registry
# so adding a language's extension there also teaches the resolver.
_SOURCE_EXTENSIONS = frozenset(EXTENSION_LANGUAGE)

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

    _NODE_COLUMNS = (
        "node_id, kind, name, qualified_name, path, start_line, end_line, language"
    )

    def _row_to_node(self, row: tuple) -> Node:
        """Build a :class:`Node` from a ``_NODE_COLUMNS`` row."""
        return Node(
            node_id=row[0],
            kind=row[1],
            name=row[2],
            qualified_name=row[3],
            path=row[4],
            start_line=row[5],
            end_line=row[6],
            language=row[7],
        )

    def _is_path_target(self, target: str) -> bool:
        """Classify ``target`` as a path rather than a symbol.

        A target is a path when it contains a path separator, ends in a
        registered source extension, or names an existing file on disk; else it
        is a symbol (a bare or dotted qualified name).
        """
        if "/" in target or "\\" in target:
            return True
        if PurePosixPath(target).suffix.lower() in _SOURCE_EXTENSIONS:
            return True
        return Path(target).is_file()

    def _normalize_path(self, target: str) -> str:
        """Normalize a path target to the stored folder-relative POSIX form.

        An absolute path is made relative to ``settings.root`` when a root is
        configured; separators are converted to POSIX. No stripping or ``LIKE``
        fallback happens here — those are applied by :meth:`_resolve_path` only
        when the unstripped exact match yields zero rows.
        """
        normalized = target.replace("\\", "/")
        root = self.settings.root
        if root and PurePosixPath(normalized).is_absolute():
            root_posix = root.replace("\\", "/")
            try:
                normalized = str(
                    PurePosixPath(normalized).relative_to(PurePosixPath(root_posix))
                )
            except ValueError:
                pass
        return normalized

    def _resolve_path(self, target: str) -> list[Node]:
        """Resolve a path target to every node stored under that path.

        Exact unstripped match first; then, only on zero rows and a first
        segment equal to ``root``'s basename, strip that leading segment and
        retry exact; then a trailing-path ``LIKE`` fallback. A path seeds every
        node with that stored path (file, module, and all symbols in it).
        """
        normalized = self._normalize_path(target)
        nodes = self._nodes_for_path(normalized)
        if nodes:
            return nodes
        root = self.settings.root
        if root:
            root_base = PurePosixPath(root.replace("\\", "/")).name
            segments = normalized.split("/")
            if len(segments) > 1 and segments[0] == root_base:
                stripped = "/".join(segments[1:])
                nodes = self._nodes_for_path(stripped)
                if nodes:
                    return nodes
        rows = self._con.execute(
            f"SELECT {self._NODE_COLUMNS} FROM nodes "
            "WHERE path LIKE '%' || ? ORDER BY path, start_line, node_id",
            [normalized],
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def _nodes_for_path(self, path: str) -> list[Node]:
        rows = self._con.execute(
            f"SELECT {self._NODE_COLUMNS} FROM nodes "
            "WHERE path = ? ORDER BY path, start_line, node_id",
            [path],
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def _resolve_symbol(self, target: str) -> tuple[list[Node], bool, str]:
        """Resolve a symbol target to node(s), with an ambiguity flag and origin.

        Exact ``qualified_name = target`` first (origin ``exact``); else a
        suffix match ``qualified_name LIKE '%.' || target`` (origin
        ``best_effort``). ``ambiguous`` is ``True`` only when a suffix match
        spans more than one distinct qualified name.
        """
        rows = self._con.execute(
            f"SELECT {self._NODE_COLUMNS} FROM nodes "
            "WHERE qualified_name = ? ORDER BY path, start_line, node_id",
            [target],
        ).fetchall()
        if rows:
            return [self._row_to_node(row) for row in rows], False, "exact"
        rows = self._con.execute(
            f"SELECT {self._NODE_COLUMNS} FROM nodes "
            "WHERE qualified_name LIKE '%.' || ? ORDER BY path, start_line, node_id",
            [target],
        ).fetchall()
        nodes = [self._row_to_node(row) for row in rows]
        ambiguous = len({node.qualified_name for node in nodes}) > 1
        return nodes, ambiguous, "best_effort"

    def _resolve_target(self, target: str) -> tuple[list[Node], bool, str]:
        """Resolve ``target`` to seed node(s), ambiguity, and seed origin.

        Returns ``(nodes, ambiguous, origin)`` where ``origin`` is ``"exact"``
        for a path or exact-qualified-name match and ``"best_effort"`` for a
        symbol suffix match (fix 2). A path match is never ambiguous.
        """
        target = target.strip()
        if self._is_path_target(target):
            return self._resolve_path(target), False, "exact"
        return self._resolve_symbol(target)

    def _order_key(self, node: Node) -> tuple[str, int, str]:
        return (node.path, node.start_line, node.node_id)

    def impact(self, target: str, *, depth: int | None = None) -> ImpactResult:
        """Return the transitive reverse-dependency set of ``target``.

        Resolves ``target`` (path or symbol), seeds its container chain once
        over ``defines`` in reverse, then runs a reverse BFS over
        ``imports|calls|inherits|references`` only — never ``defines``. Each
        affected node carries a propagated ``resolution``: a seed resolved by
        path or exact-qualified-name match is an exact origin; a seed resolved
        only by symbol suffix match is a best_effort origin, and nodes reachable
        only through it inherit best_effort (fix 2). Seeds are distance 0 and
        are never emitted, so every :class:`ImpactNode` has ``distance >= 1``;
        ``depth=0`` yields ``affected=[]``.
        """
        resolved, ambiguous, origin = self._resolve_target(target)
        if not resolved:
            return ImpactResult(
                target=target,
                resolved=[],
                ambiguous=False,
                affected=[],
                affected_files=[],
            )

        node_by_id = {node.node_id: node for node in resolved}
        # Seed origin per node id: a path/exact-qn seed is exact, a suffix seed
        # best_effort (fix 2). All resolved seeds share one origin.
        seed_exact = origin == "exact"
        resolution_of: dict[str, bool] = {node.node_id: seed_exact for node in resolved}

        # Step 2: container expansion over defines in reverse, exactly once.
        # Symbol seeds pull in their class -> module -> file chain at distance 0;
        # a path seed already covers the file, so this is a no-op for it.
        seed_ids = set(node_by_id)
        frontier = set(seed_ids)
        while frontier:
            containers = self._reverse_defines(frontier)
            new_ids = set()
            for node in containers:
                if node.node_id not in node_by_id:
                    node_by_id[node.node_id] = node
                    resolution_of[node.node_id] = seed_exact
                    seed_ids.add(node.node_id)
                    new_ids.add(node.node_id)
            frontier = new_ids

        if depth is not None and depth < 0:
            logger.info("negative depth %s clamped to 0", depth)
            depth = 0

        # Step 3: reverse BFS over dependency edges only.
        visited = set(seed_ids)
        distance_of: dict[str, int] = {nid: 0 for nid in seed_ids}
        frontier = set(seed_ids)
        current_distance = 0
        while frontier and (depth is None or current_distance < depth):
            predecessors = self._reverse_dependencies(frontier)
            # Collect per newly-seen node: did ANY incoming triple arrive via an
            # exact edge from an exact predecessor? (order-independent tie-break)
            newly: dict[str, bool] = {}
            for src_id, src_node, edge_resolution, dst_id in predecessors:
                if src_id in visited:
                    continue
                node_by_id.setdefault(src_id, src_node)
                all_exact = edge_resolution == "exact" and resolution_of.get(
                    dst_id, False
                )
                newly[src_id] = newly.get(src_id, False) or all_exact
            if not newly:
                break
            next_distance = current_distance + 1
            for src_id, is_exact in newly.items():
                visited.add(src_id)
                distance_of[src_id] = next_distance
                resolution_of[src_id] = is_exact
            frontier = set(newly)
            current_distance = next_distance

        affected_ids = visited - seed_ids
        affected_nodes = [node_by_id[nid] for nid in affected_ids]
        affected_nodes.sort(key=lambda n: (distance_of[n.node_id], *self._order_key(n)))
        affected = [
            ImpactNode(
                node=node,
                distance=distance_of[node.node_id],
                resolution="exact" if resolution_of[node.node_id] else "best_effort",
            )
            for node in affected_nodes
        ]
        affected_files = sorted({node.path for node in affected_nodes})
        resolved_sorted = sorted(resolved, key=self._order_key)
        return ImpactResult(
            target=target,
            resolved=resolved_sorted,
            ambiguous=ambiguous,
            affected=affected,
            affected_files=affected_files,
        )

    def _reverse_defines(self, dst_ids: set[str]) -> list[Node]:
        """Return the nodes that ``defines``-point at any id in ``dst_ids``."""
        if not dst_ids:
            return []
        placeholders = ", ".join(["?"] * len(dst_ids))
        rows = self._con.execute(
            f"""
            SELECT DISTINCT {self._node_select("n")}
            FROM edges e JOIN nodes n ON n.node_id = e.src_id
            WHERE e.kind = 'defines' AND e.dst_id IN ({placeholders})
            """,
            list(dst_ids),
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def _reverse_dependencies(
        self, dst_ids: set[str]
    ) -> list[tuple[str, Node, str, str]]:
        """Return ``(src_id, src_node, edge_resolution, dst_id)`` incoming edges.

        One row per dependency edge (``imports|calls|inherits|references``)
        whose ``dst_id`` is in ``dst_ids``, used by the reverse BFS hop.
        """
        if not dst_ids:
            return []
        placeholders = ", ".join(["?"] * len(dst_ids))
        kinds = ", ".join(["?"] * len(_DEPENDENCY_KINDS))
        rows = self._con.execute(
            f"""
            SELECT {self._node_select("n")}, e.resolution, e.dst_id
            FROM edges e JOIN nodes n ON n.node_id = e.src_id
            WHERE e.kind IN ({kinds}) AND e.dst_id IN ({placeholders})
            """,
            [*_DEPENDENCY_KINDS, *dst_ids],
        ).fetchall()
        result = []
        for row in rows:
            node = self._row_to_node(row[:8])
            result.append((node.node_id, node, row[8], row[9]))
        return result

    @staticmethod
    def _node_select(alias: str) -> str:
        cols = (
            "node_id",
            "kind",
            "name",
            "qualified_name",
            "path",
            "start_line",
            "end_line",
            "language",
        )
        return ", ".join(f"{alias}.{col}" for col in cols)

    def explain(self, target: str) -> ExplanationResult:
        """Return the structural neighbourhood of each resolved node.

        Always a single :class:`ExplanationResult` (never a bare Explanation,
        never a list); a zero-match target returns ``resolved=[]`` and
        ``explanations=[]`` without raising. For each resolved node: ``defines``
        are the symbols it contains (the one forward read of ``defines``);
        ``fan_out``/``fan_in`` are distinct dependency neighbours out/in; and
        ``bases``/``subclasses`` come from ``inherits`` in each direction.
        """
        resolved, ambiguous, _origin = self._resolve_target(target)
        resolved = sorted(resolved, key=self._order_key)
        explanations = [self._explain_node(node) for node in resolved]
        return ExplanationResult(
            target=target,
            resolved=resolved,
            ambiguous=ambiguous,
            explanations=explanations,
        )

    def _explain_node(self, node: Node) -> Explanation:
        defines = self._forward_nodes(node.node_id, ("defines",))
        fan_out = self._forward_nodes(node.node_id, _DEPENDENCY_KINDS)
        fan_in = self._reverse_nodes(node.node_id, _DEPENDENCY_KINDS)
        bases = (
            self._forward_nodes(node.node_id, ("inherits",))
            if (node.kind == "class")
            else []
        )
        subclasses = (
            self._reverse_nodes(node.node_id, ("inherits",))
            if (node.kind == "class")
            else []
        )
        return Explanation(
            node=node,
            defines=defines,
            fan_out=fan_out,
            fan_in=fan_in,
            bases=bases,
            subclasses=subclasses,
            fan_in_count=len(fan_in),
            fan_out_count=len(fan_out),
        )

    def _forward_nodes(self, src_id: str, kinds: tuple[str, ...]) -> list[Node]:
        """Distinct ``dst`` nodes of edges of ``kinds`` with this ``src_id``."""
        kind_ph = ", ".join(["?"] * len(kinds))
        rows = self._con.execute(
            f"""
            SELECT DISTINCT {self._node_select("n")}
            FROM edges e JOIN nodes n ON n.node_id = e.dst_id
            WHERE e.src_id = ? AND e.kind IN ({kind_ph})
            ORDER BY n.path, n.start_line, n.node_id
            """,
            [src_id, *kinds],
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def _reverse_nodes(self, dst_id: str, kinds: tuple[str, ...]) -> list[Node]:
        """Distinct ``src`` nodes of edges of ``kinds`` with this ``dst_id``."""
        kind_ph = ", ".join(["?"] * len(kinds))
        rows = self._con.execute(
            f"""
            SELECT DISTINCT {self._node_select("n")}
            FROM edges e JOIN nodes n ON n.node_id = e.src_id
            WHERE e.dst_id = ? AND e.kind IN ({kind_ph})
            ORDER BY n.path, n.start_line, n.node_id
            """,
            [dst_id, *kinds],
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def report(self) -> Report:
        """Introspect the index by pure aggregation on the read-only connection.

        An index built over a folder with zero indexable files returns
        ``nodes_by_kind={}``, ``edges_by_kind={}``, ``files_by_language={}``,
        ``languages=[]``, and ``roots=[meta.root]`` — never raises (fix 6).
        """
        meta = dict(self._con.execute("SELECT key, value FROM meta").fetchall())
        schema_version = int(meta.get("schema_version", SCHEMA_VERSION))

        files_by_language: dict[str, int] = dict(
            self._con.execute(
                "SELECT language, COUNT(*) FROM files GROUP BY language "
                "ORDER BY language"
            ).fetchall()
        )
        languages = sorted(files_by_language)
        nodes_by_kind: dict[str, int] = dict(
            self._con.execute(
                "SELECT kind, COUNT(*) FROM nodes GROUP BY kind ORDER BY kind"
            ).fetchall()
        )
        edges_by_kind: dict[str, int] = dict(
            self._con.execute(
                "SELECT kind, COUNT(*) FROM edges GROUP BY kind ORDER BY kind"
            ).fetchall()
        )

        root = meta.get("root")
        if root is not None:
            roots = [root]
        else:
            rows = self._con.execute("SELECT DISTINCT path FROM files").fetchall()
            roots = sorted({row[0].split("/")[0] for row in rows})
        return Report(
            db_path=self.settings.db_path,
            schema_version=schema_version,
            languages=languages,
            files_by_language=files_by_language,
            nodes_by_kind=nodes_by_kind,
            edges_by_kind=edges_by_kind,
            roots=roots,
        )
