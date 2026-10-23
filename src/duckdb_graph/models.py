"""Core data models for duckdb-graph.

All models are pydantic ``BaseModel``. Node ``kind``, edge ``kind``, and
``resolution`` are kept as plain strings (not enums) so a future language can
add a kind without a model change; the allowed values are the documented
constants ``NODE_KINDS``, ``EDGE_KINDS``, and ``RESOLUTIONS`` below, validated
at extraction time.
"""

from pydantic import BaseModel

NODE_KINDS = ("file", "module", "class", "function", "import")
EDGE_KINDS = ("defines", "imports", "calls", "inherits", "references")
RESOLUTIONS = ("exact", "best_effort")


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


class Edge(BaseModel):
    """A directed dependency edge extracted from a single file.

    ``src_id`` is the dependent (the "from"), ``dst_id`` is the dependency (the
    "to"). ``resolution`` is ``"exact"`` when the bind produced exactly one
    in-set node from a scoped source, else ``"best_effort"``.
    """

    src_id: str
    dst_id: str
    kind: str
    path: str
    resolution: str


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
