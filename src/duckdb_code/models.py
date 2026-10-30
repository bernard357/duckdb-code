"""Core data models for duckdb-code.

All models are pydantic ``BaseModel``. Node ``kind``, edge ``kind``, and
``resolution`` are kept as plain strings (not enums) so a future language can
add a kind without a model change; the allowed values are the documented
constants ``NODE_KINDS``, ``EDGE_KINDS``, and ``RESOLUTIONS`` below, validated
at extraction time.
"""

from pydantic import BaseModel

NODE_KINDS = ("file", "module", "class", "function", "import", "page", "heading", "tag")
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
    distinct paths of affected nodes, sorted.
    """

    target: str
    resolved: list[Node]
    ambiguous: bool = False
    affected: list[ImpactNode]
    affected_files: list[str]


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
