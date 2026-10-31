"""The code-graph store: one DuckDB file is one index.

This module owns the authoritative schema DDL (:data:`SCHEMA_DDL` and
:data:`INDEX_DDL`) so the indexer's table recreation and the store's
``_ensure_schema`` can never drift. It also exposes the three query agentic
tools over the graph: :meth:`CodeStore.impact` (transitive reverse
dependencies), :meth:`CodeStore.explain` (one node's structural
neighbourhood), and :meth:`CodeStore.report` (pure aggregation).

The store always opens read-write: incremental updates are the rule for this
package, so the file is opened writable and the schema is ensured on every
open. The only DuckDB indexes are ordinary B-tree indexes on text/int columns
plus the two primary keys (``files.path``, ``meta.key``).
"""

import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import duckdb

from duckdb_code.config import Settings
from duckdb_code.extractors.registry import EXTENSION_LANGUAGE
from duckdb_code.models import (
    CoChangePartner,
    CodeExport,
    CodeExportEdge,
    CodeExportNode,
    CommitInfo,
    Explanation,
    ExplanationResult,
    HistoryResult,
    ImpactNode,
    ImpactResult,
    LinkEdge,
    LinksResult,
    ModuleSummary,
    Node,
    Overview,
    RankedSymbol,
    RelatedTest,
    RelatedTestsResult,
    Report,
    SearchHit,
    SearchResult,
)

if TYPE_CHECKING:
    from duckdb_code.indexer import IndexReport

logger = logging.getLogger(__name__)


def _iso_or_none(value: object) -> str | None:
    """Render a stored timestamp as an ISO string, or ``None``.

    DuckDB returns a ``TIMESTAMP`` column as a ``datetime``; coerce it to an ISO
    string for the JSON-friendly ``CommitInfo``. A ``None`` stays ``None``, and
    any already-string value passes through.
    """
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)


# The reverse-dependency edge kinds the impact BFS and explain fan-in/fan-out
# traverse. ``defines`` is deliberately absent: it is a container-structure
# edge, read forward once by ``explain`` and walked in reverse only to seed a
# symbol's container chain in ``impact`` — never a dependency hop.
_DEPENDENCY_KINDS = (
    "imports",
    "calls",
    "inherits",
    "references",
    "links_to",
    "embeds",
)

# The cross-boundary edge kinds the ``links`` tool surfaces — edges an LSP
# never sees together. ``calls_route`` is the headline case (an HTTP client
# call bound to its server route across languages/files); ``links_to``,
# ``embeds``, and ``tagged`` are the Markdown page/tag boundary crossings. The
# ordinary in-language ``references`` dependency and the derived ``member_of``
# community edge are deliberately absent — neither crosses a boundary an LSP
# cannot follow.
_CROSS_BOUNDARY_KINDS = (
    "calls_route",
    "links_to",
    "embeds",
    "tagged",
)

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
        stamped_at  TIMESTAMP,
        churn       INTEGER NOT NULL DEFAULT 0
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
        language       TEXT NOT NULL,
        centrality     DOUBLE NOT NULL DEFAULT 0.0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edges (
        src_id     TEXT NOT NULL,
        dst_id     TEXT NOT NULL,
        kind       TEXT NOT NULL,
        path       TEXT NOT NULL,
        resolution TEXT NOT NULL,
        confidence DOUBLE NOT NULL DEFAULT 1.0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT NOT NULL PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS vectors (
        node_id   TEXT NOT NULL,
        embedding DOUBLE[] NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS commits (
        sha          TEXT NOT NULL,
        path         TEXT NOT NULL,
        committed_at TIMESTAMP,
        summary      TEXT NOT NULL,
        ordinal      INTEGER NOT NULL
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

# Schema version 2 added ``edges.confidence`` (numeric per-edge resolution
# confidence, the source of truth from which the ``resolution`` flag is
# derived) and ``nodes.centrality`` (index-time PageRank-style rank). Version 3
# adds the ``vectors`` table (one per-symbol embedding, keyed by ``node_id``)
# backing semantic search. Version 4 added ``files.churn`` (per-file commit
# count) and the ``file_changes_with`` edge kind (git co-change between files).
# Version 5 adds the ``commits`` table (recent git commits per file, for the
# ``history`` tool). A store opened against an older file recreates the schema
# on the next full index.
SCHEMA_VERSION = 5


class CodeStore:
    """A DuckDB-backed code graph.

    The file is always opened read-write — incremental updates are the rule for
    this package — and the schema and indexes are ensured on every open.
    """

    def __init__(
        self,
        db_path: str | None = None,
        root: str | None = None,
        embedder: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or Settings(
            db_path=db_path, root=root, embedder=embedder
        )
        self._con = duckdb.connect(self.settings.db_path)
        self._ensure_schema()

    def index(
        self,
        folder: str | Path,
        *,
        recursive: bool = True,
        rebuild: bool = False,
        use_default_ignores: bool = True,
    ) -> "IndexReport":
        """Index a folder of source code into this store.

        Incremental-by-default: a full rebuild runs when this database is empty
        or ``rebuild=True``; otherwise only changed, added, and deleted files
        are reconciled by comparing stored etags against freshly computed ones,
        and the global cross-file resolution is re-run. Delegates to
        :func:`duckdb_code.indexer.index_folder`; the import is lazy to
        avoid the store ↔ indexer import cycle.

        ``use_default_ignores`` (default ``True``) prunes dependency/build
        directories and files over the size cap; set it ``False`` to index those
        too — the ``.gitignore`` / ``.duckdbgraphignore`` rules are still honored
        either way.
        """
        from duckdb_code.indexer import index_folder

        return index_folder(
            self,
            folder,
            recursive=recursive,
            rebuild=rebuild,
            use_default_ignores=use_default_ignores,
        )

    def _ensure_schema(self) -> None:
        """Create the graph tables and the read-write indexes if absent."""
        for ddl in SCHEMA_DDL:
            self._con.execute(ddl)
        for ddl in INDEX_DDL:
            self._con.execute(ddl)

    def close(self) -> None:
        """Close the underlying connection."""
        self._con.close()

    def __enter__(self) -> "CodeStore":
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
        # Step 3: reverse closure over dependency edges. The transitive set is
        # found by ONE recursive-CTE query over the full edge table (benchmarked
        # ~5.7x faster than a per-hop Python BFS on a large graph, and cycle-safe
        # because the recursion dedups on node id). Distance and resolution are
        # then computed by a BFS over ONLY that reachable subgraph's edges — a
        # small, bounded set — preserving the original layer semantics exactly:
        # a node at distance d is exact iff some incoming exact edge arrives from
        # an exact node at distance d-1 (OR over incoming edges, tie-broken by
        # the ascending-distance order).
        reachable = self._reverse_closure_set(seed_ids)
        sub_edges = self._dependency_edges_within(reachable)
        # Incoming edges per node, within the subgraph: dst -> [(src, exact?)].
        incoming: dict[str, list[tuple[str, bool]]] = {}
        for src_id, dst_id, edge_resolution in sub_edges:
            incoming.setdefault(dst_id, []).append((src_id, edge_resolution == "exact"))

        distance_of = {nid: 0 for nid in seed_ids}
        visited = set(seed_ids)
        frontier = set(seed_ids)
        current_distance = 0
        while frontier and (depth is None or current_distance < depth):
            newly: dict[str, bool] = {}
            for dst_id in frontier:
                for src_id, edge_exact in incoming.get(dst_id, ()):
                    if src_id in visited:
                        continue
                    all_exact = edge_exact and resolution_of.get(dst_id, False)
                    newly[src_id] = newly.get(src_id, False) or all_exact
            if not newly:
                break
            current_distance += 1
            for src_id, is_exact in newly.items():
                visited.add(src_id)
                distance_of[src_id] = current_distance
                resolution_of[src_id] = is_exact
            frontier = set(newly)

        missing = [nid for nid in reachable if nid not in node_by_id]
        for node in self._nodes_by_ids(missing):
            node_by_id[node.node_id] = node

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

        # Blast radius per module: count affected nodes by their path, most
        # affected first (ties by path for a stable order).
        counts: dict[str, int] = {}
        for node in affected_nodes:
            counts[node.path] = counts.get(node.path, 0) + 1
        affected_by_module = dict(
            sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        )
        # The affected nodes that are dependency-graph roots (nothing depends on
        # them) — the outermost reach of the change.
        affected_entry_points = self._entry_points_among(affected_ids)
        # Tests that exercise any affected OR seed symbol (what to run after the
        # change): the sources of `tests` edges into that id set.
        related_tests = self._tests_reaching(affected_ids | seed_ids)

        return ImpactResult(
            target=target,
            resolved=resolved_sorted,
            ambiguous=ambiguous,
            affected=affected,
            affected_files=affected_files,
            affected_by_module=affected_by_module,
            affected_entry_points=affected_entry_points,
            related_tests=related_tests,
        )

    def _entry_points_among(self, node_ids: set[str]) -> list[Node]:
        """Return the nodes in ``node_ids`` with zero inbound dependency edges.

        An entry point is a dependency-graph root — nothing imports, calls,
        inherits, or references it. Ordered by path/line for stability.
        """
        if not node_ids:
            return []
        ids = list(node_ids)
        id_ph = ", ".join(["?"] * len(ids))
        dep = ", ".join(["?"] * len(_DEPENDENCY_KINDS))
        rows = self._con.execute(
            f"""
            SELECT {self._node_select("n")}
            FROM nodes n
            WHERE n.node_id IN ({id_ph})
              AND NOT EXISTS (
                  SELECT 1 FROM edges e
                  WHERE e.dst_id = n.node_id AND e.kind IN ({dep})
              )
            ORDER BY n.path, n.start_line, n.node_id
            """,
            [*ids, *_DEPENDENCY_KINDS],
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def _tests_reaching(self, node_ids: set[str]) -> list[Node]:
        """Return the test symbols with a ``tests`` edge into ``node_ids``.

        These are the tests to run after a change touching that id set — the
        sources of ``tests`` edges whose target is affected or a seed. Distinct,
        ordered by path/line.
        """
        if not node_ids:
            return []
        ids = list(node_ids)
        id_ph = ", ".join(["?"] * len(ids))
        rows = self._con.execute(
            f"""
            SELECT DISTINCT {self._node_select("n")}
            FROM edges e JOIN nodes n ON n.node_id = e.src_id
            WHERE e.kind = 'tests' AND e.dst_id IN ({id_ph})
            ORDER BY n.path, n.start_line, n.node_id
            """,
            ids,
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def related_tests(self, target: str) -> RelatedTestsResult:
        """Return the tests to run after editing ``target`` — a symbol or path.

        Resolves ``target`` the same way as ``impact``/``explain`` (exact
        qualified name, then path, then best-effort suffix), then returns the
        distinct test symbols that reach any resolved node via a ``tests`` edge
        (derived at index time from a test file calling, referencing, or
        importing the symbol). Ordered by path then line. Returns empty
        ``resolved``/``tests`` when the target matches nothing or no test
        reaches it — never raising.
        """
        target = target.strip()
        if not target:
            return RelatedTestsResult(symbol=target, resolved=[], tests=[])
        resolved, _ambiguous, _origin = self._resolve_target(target)
        if not resolved:
            return RelatedTestsResult(symbol=target, resolved=[], tests=[])
        seed_ids = {node.node_id for node in resolved}
        tests = [RelatedTest(node=node) for node in self._tests_reaching(seed_ids)]
        resolved_sorted = sorted(resolved, key=self._order_key)
        return RelatedTestsResult(symbol=target, resolved=resolved_sorted, tests=tests)

    def history(self, target: str) -> HistoryResult:
        """Return git signals for ``target`` — commits, co-change, and churn.

        Resolves ``target`` to node(s), takes the file they live in (history is
        tracked per file), and reads the index-time git data: the recent
        ``commits`` touching that file (newest first), its ``co_change``
        partners from ``file_changes_with`` edges (most-coupled first), and the
        file's ``churn``. Coupling and churn are signals no static graph shows.

        Returns empty signals when the target matches nothing, or when the index
        carries no git history (built outside a repo or without git) — never
        raising. When several resolved nodes span different files, the first by
        the stable order supplies the file.
        """
        target = target.strip()
        if not target:
            return HistoryResult(symbol=target, resolved=[])
        resolved, _ambiguous, _origin = self._resolve_target(target)
        if not resolved:
            return HistoryResult(symbol=target, resolved=[])
        resolved_sorted = sorted(resolved, key=self._order_key)
        path = resolved_sorted[0].path

        commit_rows = self._con.execute(
            "SELECT DISTINCT sha, committed_at, summary, ordinal FROM commits "
            "WHERE path = ? ORDER BY ordinal",
            [path],
        ).fetchall()
        commits = [
            CommitInfo(
                sha=row[0],
                committed_at=_iso_or_none(row[1]),
                summary=row[2],
            )
            for row in commit_rows
        ]

        # Co-change partners: the OTHER endpoint of a file_changes_with edge on
        # this file's node, either direction, ranked by the normalized
        # co-occurrence confidence.
        file_node_id = f"file:{path}@{path}:1"
        partner_rows = self._con.execute(
            """
            SELECT n.path, e.confidence
            FROM edges e JOIN nodes n ON n.node_id =
                CASE WHEN e.src_id = ? THEN e.dst_id ELSE e.src_id END
            WHERE e.kind = 'file_changes_with' AND (e.src_id = ? OR e.dst_id = ?)
            ORDER BY e.confidence DESC, n.path
            """,
            [file_node_id, file_node_id, file_node_id],
        ).fetchall()
        churn_row = self._con.execute(
            "SELECT churn FROM files WHERE path = ?", [path]
        ).fetchone()
        churn = churn_row[0] if churn_row else 0
        # The raw together_count is not stored; surface the normalized coupling
        # strength scaled to the file's churn as a best-effort count proxy.
        co_change = [
            CoChangePartner(
                path=row[0],
                together_count=max(1, round(row[1] * churn)) if churn else 1,
            )
            for row in partner_rows
        ]

        return HistoryResult(
            symbol=target,
            resolved=resolved_sorted,
            path=path,
            commits=commits,
            co_change=co_change,
            churn=churn,
        )

    def links(self, target: str) -> LinksResult:
        """Return the cross-boundary edges touching ``target`` — a symbol or path.

        Resolves ``target`` the same way as ``impact``/``explain`` (exact
        qualified name, then path, then best-effort suffix), then collects the
        cross-boundary edges where any resolved node is the ``src`` OR the
        ``dst`` — both directions, so asking from either endpoint of an HTTP
        route↔client link surfaces it. The cross-boundary kinds are
        :data:`_CROSS_BOUNDARY_KINDS` (``calls_route`` plus the Markdown
        ``links_to``/``embeds``/``tagged``) — the edges an LSP never sees
        together; ordinary in-language dependencies are excluded.

        Each :class:`LinkEdge` carries the from/to symbol nodes, the edge kind,
        the ``path``/``range`` of its source node (the origin to hand to an
        LSP), and the stored ``resolution``/``confidence`` so a heuristic
        binding is never presented as certain. Edges are distinct and ordered by
        kind, then source path/line, then target path/line. Returns empty
        ``resolved``/``links`` when the target matches nothing or carries no
        cross-boundary edge — never raising.
        """
        target = target.strip()
        if not target:
            return LinksResult(symbol=target, resolved=[], links=[])
        resolved, _ambiguous, _origin = self._resolve_target(target)
        if not resolved:
            return LinksResult(symbol=target, resolved=[], links=[])
        resolved_sorted = sorted(resolved, key=self._order_key)
        node_ids = {node.node_id for node in resolved}
        links = self._cross_boundary_edges(node_ids)
        return LinksResult(symbol=target, resolved=resolved_sorted, links=links)

    def _cross_boundary_edges(self, node_ids: set[str]) -> list[LinkEdge]:
        """Return the distinct cross-boundary edges touching any id in a set.

        An edge qualifies when its kind is in :data:`_CROSS_BOUNDARY_KINDS` and
        either endpoint is in ``node_ids``. Both endpoints are joined to
        ``nodes`` so the from/to symbols are materialized; the result is ordered
        by kind, source path/line, then target path/line for a stable order.
        """
        if not node_ids:
            return []
        ids = list(node_ids)
        id_ph = ", ".join(["?"] * len(ids))
        kind_ph = ", ".join(["?"] * len(_CROSS_BOUNDARY_KINDS))
        rows = self._con.execute(
            f"""
            SELECT DISTINCT
                e.kind, e.resolution, e.confidence,
                {self._node_select("s")},
                {self._node_select("d")}
            FROM edges e
            JOIN nodes s ON s.node_id = e.src_id
            JOIN nodes d ON d.node_id = e.dst_id
            WHERE e.kind IN ({kind_ph})
              AND (e.src_id IN ({id_ph}) OR e.dst_id IN ({id_ph}))
            ORDER BY e.kind, s.path, s.start_line, s.node_id,
                     d.path, d.start_line, d.node_id
            """,
            [*_CROSS_BOUNDARY_KINDS, *ids, *ids],
        ).fetchall()
        links: list[LinkEdge] = []
        for row in rows:
            kind, resolution, confidence = row[0], row[1], row[2]
            source = self._row_to_node(row[3:11])
            to = self._row_to_node(row[11:19])
            links.append(
                LinkEdge(
                    kind=kind,
                    source=source,
                    to=to,
                    path=source.path,
                    range=(source.start_line, source.end_line),
                    resolution=resolution,
                    confidence=confidence,
                )
            )
        return links

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

    def _reverse_closure_set(self, seed_ids: set[str]) -> set[str]:
        """Return the transitive reverse-dependency node-id set (incl. seeds).

        A single recursive CTE over the dependency edge kinds. The recursive
        relation carries ONLY ``node_id`` so ``UNION`` dedups on the node and
        the recursion terminates even when the graph has cycles (Markdown pages
        link mutually, which a pair-carrying CTE would loop on forever). This
        finds the closure in one engine-side traversal of the full edge table;
        distance and resolution are then computed by a bounded Python pass over
        just this (small) reachable subgraph. Benchmarked ~5.7x faster than a
        per-hop Python BFS over the full graph.
        """
        if not seed_ids:
            return set()
        seed_values = ", ".join(["(?)"] * len(seed_ids))
        kinds = ", ".join(["?"] * len(_DEPENDENCY_KINDS))
        rows = self._con.execute(
            f"""
            WITH RECURSIVE closure(node_id) AS (
                SELECT * FROM (VALUES {seed_values}) AS s(node_id)
                UNION
                SELECT e.src_id
                FROM edges e
                JOIN closure c ON e.dst_id = c.node_id
                WHERE e.kind IN ({kinds})
            )
            SELECT node_id FROM closure
            """,
            [*seed_ids, *_DEPENDENCY_KINDS],
        ).fetchall()
        return {row[0] for row in rows}

    def _dependency_edges_within(
        self, node_ids: set[str]
    ) -> list[tuple[str, str, str]]:
        """Return ``(src_id, dst_id, resolution)`` dependency edges inside a set.

        Only edges whose src AND dst are both in ``node_ids`` — the subgraph the
        reverse closure spans — so the Python distance/resolution BFS runs over
        a small, bounded edge set rather than the whole graph.
        """
        if not node_ids:
            return []
        placeholders = ", ".join(["?"] * len(node_ids))
        kinds = ", ".join(["?"] * len(_DEPENDENCY_KINDS))
        ids = list(node_ids)
        rows = self._con.execute(
            f"""
            SELECT src_id, dst_id, resolution FROM edges
            WHERE kind IN ({kinds})
              AND src_id IN ({placeholders})
              AND dst_id IN ({placeholders})
            """,
            [*_DEPENDENCY_KINDS, *ids, *ids],
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def _nodes_by_ids(self, node_ids: list[str]) -> list[Node]:
        """Fetch many nodes by id in ONE query (not one round-trip per id).

        Fetching the reachable closure's nodes individually is the same
        N-round-trip trap the recursive closure avoids; a single ``IN (...)``
        select keeps node materialization bounded to one query.
        """
        if not node_ids:
            return []
        placeholders = ", ".join(["?"] * len(node_ids))
        rows = self._con.execute(
            f"SELECT {self._node_select('n')} FROM nodes n "
            f"WHERE n.node_id IN ({placeholders})",
            node_ids,
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

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
        """Introspect the index by pure aggregation on the open connection.

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
            branch=meta.get("branch"),
            head_commit=meta.get("head_commit"),
        )

    # Dependency edge kinds that define "something depends on this" — the same
    # set the analytics phase ranks over. An entry point has zero inbound edges
    # of these kinds (nothing calls/imports/inherits/references it).
    _OVERVIEW_DEP_KINDS = ("imports", "calls", "inherits", "references")

    def overview(self, budget: int = 2000) -> Overview:
        """Return the repo map an agent reads to orient before touching code.

        Three ranked sections, each capped to a share of ``budget`` (a rough
        token budget, translated to item counts) so the whole map fits a
        context window:

        - ``modules`` — indexed ``module``/``page`` nodes, most symbol-rich
          first (symbol count = nodes sharing the module's path).
        - ``entry_points`` — ``function``/``class`` symbols with zero inbound
          dependency edges (dependency-graph roots: CLI commands, handlers,
          ``main``), most central first among those.
        - ``central_symbols`` — the most depended-upon symbols by the index-time
          ``centrality`` rank.

        A non-positive ``budget`` yields empty sections. The counts scale with
        the budget but are floored so a small budget still returns a few of
        each, and capped so a huge budget does not dump the whole graph.
        """
        if budget <= 0:
            return Overview(
                modules=[], entry_points=[], central_symbols=[], budget=budget
            )
        # ~80 tokens per listed item, split across the three sections; floor 5,
        # cap 50 per section so the map is useful at any budget.
        per_section = max(5, min(50, budget // (80 * 3)))

        module_rows = self._con.execute(
            f"""
            SELECT {self._node_select("n")},
                   (SELECT COUNT(*) FROM nodes m WHERE m.path = n.path) AS symbols
            FROM nodes n
            WHERE n.kind IN ('module', 'page')
            ORDER BY symbols DESC, n.qualified_name, n.node_id
            LIMIT ?
            """,
            [per_section],
        ).fetchall()
        modules = [
            ModuleSummary(node=self._row_to_node(row[:-1]), symbol_count=row[-1])
            for row in module_rows
        ]

        dep = ", ".join(["?"] * len(self._OVERVIEW_DEP_KINDS))
        entry_rows = self._con.execute(
            f"""
            SELECT {self._node_select("n")}
            FROM nodes n
            WHERE n.kind IN ('function', 'class')
              AND NOT EXISTS (
                  SELECT 1 FROM edges e
                  WHERE e.dst_id = n.node_id AND e.kind IN ({dep})
              )
            ORDER BY n.centrality DESC, n.qualified_name, n.node_id
            LIMIT ?
            """,
            [*self._OVERVIEW_DEP_KINDS, per_section],
        ).fetchall()
        entry_points = [self._row_to_node(row) for row in entry_rows]

        central_rows = self._con.execute(
            f"""
            SELECT {self._node_select("n")}, n.centrality AS rank
            FROM nodes n
            WHERE n.kind IN ('function', 'class', 'module')
            ORDER BY rank DESC, n.qualified_name, n.node_id
            LIMIT ?
            """,
            [per_section],
        ).fetchall()
        central_symbols = [
            RankedSymbol(node=self._row_to_node(row[:-1]), rank=row[-1])
            for row in central_rows
        ]

        return Overview(
            modules=modules,
            entry_points=entry_points,
            central_symbols=central_symbols,
            budget=budget,
        )

    def search(self, query: str, *, limit: int = 10) -> SearchResult:
        """Rank indexed symbols by semantic relevance to ``query``.

        Embeds ``query`` with the same backend the index recorded (so the query
        and stored vectors share a space), then ranks the ``vectors`` table by
        cosine similarity in DuckDB — no Python-side distance loop — and joins
        to ``nodes`` for the result fields. Returns at most ``limit`` hits, most
        relevant first. An index with no stored embeddings (or an empty query)
        returns ``results=[]`` rather than raising, so a graph built without the
        embedding backend still answers, just with nothing to rank.

        A ``limit`` of zero or negative yields an empty result. Ties in score
        are broken deterministically by ``qualified_name`` then ``node_id`` so
        repeated queries return a stable order.
        """
        query = query.strip()
        if not query or limit <= 0:
            return SearchResult(query=query, results=[])
        has_vectors = self._con.execute("SELECT COUNT(*) FROM vectors").fetchone()
        if not has_vectors or has_vectors[0] == 0:
            return SearchResult(query=query, results=[])

        from duckdb_code.embedders import resolve_embedder

        embedder = resolve_embedder(self.settings.embedder)
        query_vector = embedder.embed([query])[0]
        rows = self._con.execute(
            f"""
            SELECT {self._node_select("n")},
                   list_cosine_similarity(v.embedding, ?) AS score
            FROM vectors v JOIN nodes n ON n.node_id = v.node_id
            WHERE score IS NOT NULL
            ORDER BY score DESC, n.qualified_name, n.node_id
            LIMIT ?
            """,
            [query_vector, limit],
        ).fetchall()
        results = [
            SearchHit(node=self._row_to_node(row[:-1]), score=row[-1]) for row in rows
        ]
        return SearchResult(query=query, results=results)

    def find_similar(self, target: str, *, limit: int = 10) -> SearchResult:
        """Rank indexed symbols by similarity to ``target`` — a symbol or snippet.

        Answers "how do we usually do this here?" and surfaces duplication.
        ``target`` is resolved as a symbol or file path first: when it matches
        indexed node(s) that carry stored embeddings, those stored vectors seed
        the query (averaged when several match) and the seed nodes are excluded
        from the ranking, so a symbol finds *other* code built like it. When the
        target resolves to nothing, it is treated as a raw code snippet and
        embedded directly. Either way the ``vectors`` table is ranked by cosine
        similarity in DuckDB, most similar first, capped at ``limit``.

        Returns ``results=[]`` for an empty target, a non-positive ``limit``, or
        an index with no stored embeddings — never raising. Ties break by
        ``qualified_name`` then ``node_id`` for a stable order.
        """
        target = target.strip()
        if not target or limit <= 0:
            return SearchResult(query=target, results=[])
        has_vectors = self._con.execute("SELECT COUNT(*) FROM vectors").fetchone()
        if not has_vectors or has_vectors[0] == 0:
            return SearchResult(query=target, results=[])

        # Resolve the target to indexed nodes; a symbol/path uses its stored
        # vector(s), an unresolved string is embedded as a snippet.
        resolved, _ambiguous, _origin = self._resolve_target(target)
        seed_ids = [node.node_id for node in resolved]
        query_vector, exclude = self._similarity_query_vector(target, seed_ids)
        if query_vector is None:
            return SearchResult(query=target, results=[])

        exclude_clause = ""
        params: list = [query_vector]
        if exclude:
            placeholders = ", ".join(["?"] * len(exclude))
            exclude_clause = f"AND v.node_id NOT IN ({placeholders})"
            params.extend(exclude)
        params.append(limit)
        rows = self._con.execute(
            f"""
            SELECT {self._node_select("n")},
                   list_cosine_similarity(v.embedding, ?) AS score
            FROM vectors v JOIN nodes n ON n.node_id = v.node_id
            WHERE score IS NOT NULL {exclude_clause}
            ORDER BY score DESC, n.qualified_name, n.node_id
            LIMIT ?
            """,
            params,
        ).fetchall()
        results = [
            SearchHit(node=self._row_to_node(row[:-1]), score=row[-1]) for row in rows
        ]
        return SearchResult(query=target, results=results)

    def _similarity_query_vector(
        self, target: str, seed_ids: list[str]
    ) -> tuple[list[float] | None, list[str]]:
        """Return ``(query_vector, exclude_ids)`` for :meth:`find_similar`.

        When ``seed_ids`` name symbols with stored vectors, the query vector is
        the component-wise mean of those vectors (so several resolved nodes seed
        one query) and the seeds are excluded from the ranking. When no seed has
        a stored vector, ``target`` is embedded as a raw snippet and nothing is
        excluded. Returns ``(None, [])`` when there is nothing to embed.
        """
        if seed_ids:
            placeholders = ", ".join(["?"] * len(seed_ids))
            vectors = [
                row[0]
                for row in self._con.execute(
                    f"SELECT embedding FROM vectors WHERE node_id IN ({placeholders})",
                    seed_ids,
                ).fetchall()
            ]
            if vectors:
                dims = len(vectors[0])
                mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dims)]
                return mean, seed_ids
        # Snippet path: embed the raw target text.
        from duckdb_code.embedders import resolve_embedder

        embedder = resolve_embedder(self.settings.embedder)
        return embedder.embed([target])[0], []

    def export(self) -> CodeExport:
        """Export the whole graph in the viewer's Cytoscape data contract.

        Performs no writes: one ordered read per table (``nodes`` then
        ``edges``) feeds the renamed export models — the stored ``node_id``
        becomes ``id`` and ``src_id``/``dst_id`` become ``source``/``target``
        because Cytoscape requires those keys.
        """
        meta = self.report()
        node_rows = self._con.execute(
            "SELECT node_id, kind, name, qualified_name, path, language, "
            "start_line, end_line FROM nodes ORDER BY path, start_line, node_id"
        ).fetchall()
        nodes = [
            CodeExportNode(
                id=row[0],
                kind=row[1],
                name=row[2],
                qualified_name=row[3],
                path=row[4],
                language=row[5],
                start_line=row[6],
                end_line=row[7],
            )
            for row in node_rows
        ]
        edge_rows = self._con.execute(
            "SELECT src_id, dst_id, kind, resolution FROM edges "
            "ORDER BY src_id, dst_id, kind"
        ).fetchall()
        edges = [
            CodeExportEdge(
                source=row[0],
                target=row[1],
                kind=row[2],
                resolution=row[3],
            )
            for row in edge_rows
        ]
        return CodeExport(meta=meta, nodes=nodes, edges=edges)
