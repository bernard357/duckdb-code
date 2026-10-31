"""Core data models for duckdb-code.

All models are pydantic ``BaseModel``. Node ``kind``, edge ``kind``, and
``resolution`` are kept as plain strings (not enums) so a future language can
add a kind without a model change; the allowed values are the documented
constants ``NODE_KINDS``, ``EDGE_KINDS``, and ``RESOLUTIONS`` below, validated
at extraction time.
"""

from pydantic import BaseModel, ConfigDict, Field

NODE_KINDS = (
    "file",
    "module",
    "class",
    "function",
    "import",
    "page",
    "heading",
    "tag",
    "route",
)
EDGE_KINDS = (
    "defines",
    "imports",
    "calls",
    "inherits",
    "references",
    "links_to",
    "embeds",
    "tagged",
    "member_of",
    "tests",
    "file_changes_with",
    "calls_route",
)
RESOLUTIONS = ("exact", "best_effort")

# Per-strategy resolution confidence (0.0-1.0), the source of truth from which
# the ``resolution`` flag is derived. The scale follows the Codebase-Memory
# cascade: an exact qualified-name or module hit is near-certain; a scoped
# unique-name match is strong; a suffix / last-segment fallback is weak. A
# ``defines`` edge and any structurally exact bind are 1.0.
CONFIDENCE_EXACT = 1.0  # defines, and a single exact qualified-name/module bind
CONFIDENCE_IMPORT = 0.95  # resolved through the file's import map
CONFIDENCE_SAME_MODULE = 0.90  # module-local unique bind
CONFIDENCE_UNIQUE_NAME = 0.75  # unique project-wide simple-name bind
CONFIDENCE_SUFFIX = 0.55  # last-segment / suffix fallback
CONFIDENCE_FUZZY = 0.30  # last-resort

# The threshold projecting numeric confidence onto the two-value resolution
# flag: at or above is ``exact``, below is ``best_effort``. Kept here so the
# store, the resolver, and the MCP contract share one definition.
RESOLUTION_EXACT_THRESHOLD = 0.9


def resolution_of(confidence: float) -> str:
    """Project a numeric ``confidence`` onto the agent-facing resolution flag."""
    return "exact" if confidence >= RESOLUTION_EXACT_THRESHOLD else "best_effort"


class Node(BaseModel):
    """A single graph node: a file, module, class, function, or import.

    ``node_id`` is the stable, deterministic key
    ``f"{kind}:{qualified_name}@{path}:{start_line}"`` so re-indexing the same
    bytes yields byte-identical ids. ``language`` is the detected language name
    (the registry's language key), not the file extension.
    """

    node_id: str
    kind: str
    name: str
    qualified_name: str
    path: str
    start_line: int
    end_line: int
    language: str
    centrality: float = 0.0


class Edge(BaseModel):
    """A directed dependency edge extracted from a single file.

    ``src_id`` is the dependent (the "from"), ``dst_id`` is the dependency (the
    "to"). ``confidence`` is the numeric resolution confidence (0.0-1.0, see the
    ``CONFIDENCE_*`` constants) and is the source of truth; ``resolution`` is
    its two-value projection (``"exact"`` at or above
    :data:`RESOLUTION_EXACT_THRESHOLD`, else ``"best_effort"``), kept so a
    consumer can filter without re-deriving.
    """

    src_id: str
    dst_id: str
    kind: str
    path: str
    resolution: str
    confidence: float = 1.0


class ImpactNode(BaseModel):
    """One affected node in an ``impact`` result, with its reverse-hop distance.

    ``distance`` is the shortest reverse-hop count from the target; it is always
    >= 1 because seeds are distance 0 and are never emitted.

    ``resolution`` is the per-node propagated resolution. A node is ``"exact"``
    iff it is first reached via an exact edge from an exact-or-seed predecessor;
    otherwise it is ``"best_effort"``, and exact wins on an equal-distance tie.
    Propagation also carries the seed-origin: a seed resolved by path match or
    exact qualified-name match is an exact origin, so nodes reached through it
    can stay exact; a seed resolved only by symbol suffix match is a best_effort
    origin, so every node reachable only through it inherits best_effort. In
    short: path/exact-qn seed = exact origin; suffix-only seed = best_effort
    origin; exact wins equal-distance tie.
    """

    node: Node
    distance: int
    resolution: str


class ImpactResult(BaseModel):
    """The result of an ``impact`` query: the transitive reverse-dependency set.

    ``resolved`` is the node(s) the ``target`` string resolved to. ``ambiguous``
    is ``True`` when a symbol target matched more than one qualified name.
    ``affected`` is deterministically ordered; ``affected_files`` lists the
    distinct paths of affected nodes, sorted. ``affected_by_module`` aggregates
    the affected nodes by their module path (path -> count, descending), so a
    caller sees the blast radius per module without walking ``affected``.
    ``affected_entry_points`` are the affected nodes that are dependency-graph
    roots (nothing depends on them) — the outermost things a change reaches.
    ``related_tests`` are the test symbols that exercise any affected (or seed)
    symbol via a ``tests`` edge — what to run after the change.
    """

    target: str
    resolved: list[Node]
    ambiguous: bool = False
    affected: list[ImpactNode]
    affected_files: list[str]
    affected_by_module: dict[str, int] = {}
    affected_entry_points: list[Node] = []
    related_tests: list[Node] = []


class Explanation(BaseModel):
    """The structural neighbourhood of one resolved node.

    ``defines`` are the symbols a file/module/class defines; ``fan_out`` are the
    direct dependencies (this -> X); ``fan_in`` are the direct dependents
    (X -> this). ``bases``/``subclasses`` apply to a class node.
    """

    node: Node
    defines: list[Node]
    fan_out: list[Node]
    fan_in: list[Node]
    bases: list[Node]
    subclasses: list[Node]
    fan_in_count: int
    fan_out_count: int


class ExplanationResult(BaseModel):
    """The result of an ``explain`` query: one explanation per resolved node.

    ``resolved`` is the node(s) the ``target`` string resolved to, in the same
    order as ``explanations``. ``ambiguous`` is ``True`` when a symbol target
    matched more than one qualified name.
    """

    target: str
    resolved: list[Node]
    ambiguous: bool = False
    explanations: list[Explanation]


class Report(BaseModel):
    """Introspection of a code-graph index."""

    db_path: str
    schema_version: int
    languages: list[str]
    files_by_language: dict[str, int]
    nodes_by_kind: dict[str, int]
    edges_by_kind: dict[str, int]
    roots: list[str]
    branch: str | None = None
    head_commit: str | None = None


class CodeExportNode(BaseModel):
    """A node in the Cytoscape-shaped graph export.

    The stored :class:`Node` key ``node_id`` is deliberately renamed to ``id``
    here because Cytoscape requires each element's data to carry an ``id``. The
    field is named ``id`` directly (no alias) so ``model_dump()`` and
    ``model_dump_json()`` always emit the Cytoscape shape.
    """

    id: str
    kind: str
    name: str
    qualified_name: str
    path: str
    language: str
    start_line: int
    end_line: int


class CodeExportEdge(BaseModel):
    """An edge in the Cytoscape-shaped graph export.

    The stored :class:`Edge` keys ``src_id`` and ``dst_id`` are deliberately
    renamed to ``source`` and ``target`` here because Cytoscape requires each
    edge's data to carry ``source`` and ``target`` endpoints. The fields are
    named ``source``/``target`` directly (no aliases) so ``model_dump()`` and
    ``model_dump_json()`` always emit the Cytoscape shape.
    """

    source: str
    target: str
    kind: str
    resolution: str


class CodeExport(BaseModel):
    """The whole graph serialized for the viewer in the Cytoscape data contract.

    ``meta`` is the :class:`Report` introspection; ``nodes`` and ``edges`` carry
    the id/source/target-renamed elements. This is the stable shape the web
    viewer consumes.
    """

    meta: Report
    nodes: list[CodeExportNode]
    edges: list[CodeExportEdge]


class SearchHit(BaseModel):
    """One ranked symbol in a :class:`SearchResult`.

    ``node`` is the matched symbol; ``score`` is the cosine similarity of its
    stored embedding to the query embedding (higher is closer, range -1..1 but
    in practice 0..1 for normalized identifier vectors).
    """

    node: Node
    score: float


class SearchResult(BaseModel):
    """The result of a ``search`` query: symbols ranked by semantic relevance.

    ``query`` is the search string, echoed back. ``results`` is the ranked list
    (most relevant first), capped at the requested limit. An index built with
    no embeddings (or a query that matches nothing) returns ``results=[]``.
    """

    query: str
    results: list[SearchHit]


class ModuleSummary(BaseModel):
    """One module in an :class:`Overview`: its node plus how much it holds."""

    node: Node
    symbol_count: int


class RankedSymbol(BaseModel):
    """A symbol ranked by centrality in an :class:`Overview`."""

    node: Node
    rank: float


class Overview(BaseModel):
    """The repo map an agent reads to orient before touching anything.

    ``modules`` are the indexed modules (most symbol-rich first); ``entry_points``
    are symbols nothing depends on (dependency-graph roots — CLI commands,
    top-level handlers, ``main``); ``central_symbols`` are the most depended-upon
    symbols by index-time centrality. ``budget`` is the token budget the caller
    passed; each section is capped to a share of it so the whole map fits.
    """

    modules: list[ModuleSummary]
    entry_points: list[Node]
    central_symbols: list[RankedSymbol]
    budget: int


class RelatedTest(BaseModel):
    """One test in a :class:`RelatedTestsResult`.

    ``node`` is the test symbol; ``reason`` names why it is related — the kind
    of ``tests`` edge binding it to the target (currently always
    ``"reaches"``, i.e. the test calls/references/imports the symbol), left as a
    field so richer reasons (e.g. naming-convention matches) can be added
    without changing the shape.
    """

    node: Node
    reason: str = "reaches"


class RelatedTestsResult(BaseModel):
    """The result of a ``related_tests`` query: the tests to run after an edit.

    ``symbol`` is the query string, echoed back. ``resolved`` is the node(s) it
    matched; ``tests`` is the distinct set of test symbols that exercise any
    resolved node, ordered by path then line. Empty when the target resolves to
    nothing or no test reaches it.
    """

    symbol: str
    resolved: list[Node]
    tests: list[RelatedTest]


class CommitInfo(BaseModel):
    """One recent commit touching a symbol's file, in a :class:`HistoryResult`."""

    sha: str
    committed_at: str | None = None
    summary: str


class CoChangePartner(BaseModel):
    """A file that changes together with the target's file.

    ``together_count`` is the number of recent commits that touched both files;
    higher means tighter coupling.
    """

    path: str
    together_count: int


class LinkEdge(BaseModel):
    """One cross-boundary edge in a :class:`LinksResult`.

    A cross-boundary edge is one an LSP never sees together — the headline case
    is an HTTP ``calls_route`` edge binding a client call to its server route
    across languages; the Markdown ``links_to``/``embeds``/``tagged`` edges that
    cross page/tag boundaries are the other populated kinds.

    ``kind`` is the edge kind; ``source``/``to`` are the from/to symbols (the
    edge's ``src``/``dst`` nodes). The contract spells the JSON keys ``from`` and
    ``to``; ``from`` is a Python keyword, so the field is named ``source`` with
    the serialization alias ``from`` (dump with ``by_alias=True`` to emit the
    contract shape). ``path`` and ``range`` locate the edge's origin (the source
    node's file and line span) so a consumer can hand a precise follow-up to the
    LSP. ``resolution`` is the two-value projection of ``confidence`` (the
    numeric source of truth), so a best-effort binding is never presented as
    certain.
    """

    model_config = ConfigDict(populate_by_name=True)

    kind: str
    source: Node = Field(serialization_alias="from")
    to: Node
    path: str
    range: tuple[int, int]
    resolution: str
    confidence: float


class LinksResult(BaseModel):
    """The result of a ``links`` query: the cross-boundary edges of a symbol.

    ``symbol`` is the query string, echoed back; ``resolved`` is the node(s) it
    matched; ``links`` is the cross-boundary edges touching any resolved node in
    either direction, ordered deterministically. Empty ``resolved``/``links``
    when the target matches nothing or carries no cross-boundary edge (the
    common case for ordinary in-language code). Dump with ``by_alias=True`` so
    each edge's ``from`` field carries the contract key.
    """

    symbol: str
    resolved: list[Node]
    links: list[LinkEdge]


class HistoryResult(BaseModel):
    """The result of a ``history`` query: git signals for a symbol.

    ``symbol`` is the query, echoed back; ``resolved`` is the node(s) it matched;
    ``path`` is the file those nodes live in (the unit history is tracked at).
    ``commits`` are the recent commits touching that file (newest first),
    ``co_change`` the files that usually change with it (most-coupled first),
    and ``churn`` the file's recent commit count. Empty signals when the index
    carries no git history (built outside a repo or without git).
    """

    symbol: str
    resolved: list[Node]
    path: str | None = None
    commits: list[CommitInfo] = []
    co_change: list[CoChangePartner] = []
    churn: int = 0


class AnalyticsItem(BaseModel):
    """One row of an ``analytics`` result, shaped per the requested ``kind``.

    A single flexible row covers all four analytics views so the result stays
    ``{kind, results: [...]}`` without a union type. Which fields are populated
    depends on the kind:

    - ``cycles`` — ``members`` holds the nodes of one dependency cycle (a
      strongly-connected component of size > 1); ``size`` is their count.
    - ``clusters`` — ``members`` holds the nodes of one community (the
      index-time ``member_of`` grouping); ``size`` is their count.
    - ``dead_code`` — ``node`` is a symbol nothing depends on (conservatively
      scoped: entry points, exported/public, and test symbols excluded).
    - ``hotspots`` — ``node`` is a volatile, central symbol; ``score`` is its
      ``centrality`` x file ``churn`` and ``churn`` the raw file churn.
    """

    node: Node | None = None
    members: list[Node] = []
    size: int = 0
    score: float = 0.0
    churn: int = 0


class AnalyticsResult(BaseModel):
    """The result of an ``analytics`` query: a whole-repo structural view.

    ``kind`` echoes the requested view (``cycles``, ``clusters``, ``dead_code``,
    or ``hotspots``); ``results`` are its rows, each an :class:`AnalyticsItem`
    shaped for that kind, ordered most-significant first (largest cycle/cluster,
    or highest hotspot score). An empty ``results`` means the view found nothing
    (e.g. an acyclic graph for ``cycles``), never an error.
    """

    kind: str
    results: list[AnalyticsItem]


class IndexStatus(BaseModel):
    """The result of an ``index_status`` query: index freshness and size.

    ``indexed_commit``/``branch`` are the git revision the index was built from
    (``None`` when built outside a repo). ``dirty`` is whether the working tree
    has uncommitted changes since ``indexed_commit``, computed at query time;
    it is ``None`` when git is unavailable or the root is not a repo, so an
    unknown state is never reported as clean. ``indexed_at`` is the most recent
    file stamp (ISO string). ``node_count``/``edge_count`` are the index totals.
    """

    indexed_commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    indexed_at: str | None = None
    node_count: int = 0
    edge_count: int = 0


class ChangedSymbol(BaseModel):
    """A symbol touched by a change, in a ``context_for_change`` result.

    ``node`` is the touched symbol; it is one whose ``[start_line, end_line]``
    span overlaps a changed line range in its file. The ``id``/``path``/``range``
    the contract lists are read off ``node`` (``node.node_id`` /``node.path`` /
    ``(node.start_line, node.end_line)``).
    """

    node: Node


class NeighbourSymbol(BaseModel):
    """A one-hop graph neighbour of a changed symbol.

    ``node`` is a symbol directly connected to a changed symbol by a dependency
    edge; ``relation`` names how, as ``"<kind>:out"`` when the changed symbol
    depends on this neighbour (the edge points out from the change) or
    ``"<kind>:in"`` when this neighbour depends on the changed symbol (the edge
    points in). It is the blast-radius context for review and PR-description
    work, not the full transitive set — exactly one hop.
    """

    node: Node
    relation: str


class ContextForChange(BaseModel):
    """The result of a ``context_for_change`` query: a change's local context.

    ``changed`` are the symbols the diff touched (by line-range overlap);
    ``neighbourhood`` are their direct dependency neighbours, each tagged with
    the ``relation`` by which it connects. ``base`` echoes what the change was
    measured against (``"working-tree"`` or a branch/ref name). Empty
    ``changed``/``neighbourhood`` when git is unavailable, the root is not a
    repo, or the diff touches nothing in the index — never an error.
    """

    base: str
    changed: list[ChangedSymbol]
    neighbourhood: list[NeighbourSymbol]
