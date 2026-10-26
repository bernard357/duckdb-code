"""MCP-over-stdio server exposing duckdb-graph query tools.

Any MCP client (Docker Agent, Claude Desktop, Claude Code, Cursor) can spawn
``duckdb-graph mcp`` as a subprocess and call the four tools ``impact``,
``explain``, ``report``, and ``export``. The server also advertises a
``description`` and ``instructions`` so a client can route to it and pick a
tool by intent. The server is built on the official ``mcp`` SDK.

Note on the import path: in the ``mcp`` 2.x SDK the server class is
``MCPServer`` (``from mcp.server.mcpserver import MCPServer``); its
``.run(transport="stdio")`` serves over stdio, matching the subprocess model
MCP clients use. This mirrors the sibling ``duckdb-vectors`` verbatim.

Each tool opens the resolved :class:`Settings` read-write (the store always
opens read-write) but only ever runs query methods, so serving is query-only
in behaviour. Each tool returns exactly one JSON object (an
:class:`ImpactResult`, :class:`ExplanationResult`, or :class:`Report`
``.model_dump()`` dict) — never a bare ``Explanation`` and never a list — so a
consuming agent parses one stable shape per tool.
"""

import os
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from duckdb_graph.config import Settings
from duckdb_graph.store import GraphStore

# Inline JSON is only returned when the graph is at or under this node+edge
# count. A full index runs to ~100k edges / tens of MB of JSON — far past what
# an MCP tool response should carry inline — so the default is a path handoff
# and inline is a bounded convenience for small graphs only.
_INLINE_ELEMENT_CAP = 2000

# The client-facing description each tool advertises. Kept as module constants
# (passed explicitly to ``@server.tool(description=...)``) rather than drawn
# from the function docstring: the SDK uses ``fn.__doc__`` verbatim, so a
# docstring would leak the source indentation that ruff enforces into the text
# a client reads. Each leads with the intent ("use this to …") so an agent can
# route a task to the right tool.
_IMPACT_DESCRIPTION = (
    "Use this to evaluate the implications of a change. Returns the transitive "
    "reverse-dependency set of a symbol or file path — everything that depends "
    "on the target, directly or indirectly — so you can scope the blast radius "
    "before editing it. "
    "Argument: 'target' is a dotted qualified name (e.g. 'pkg.module.Symbol') "
    "or a file path; 'depth' optionally caps how many hops out to walk. "
    "Returns one JSON object: {target, resolved (the node(s) the target matched), "
    "ambiguous (true when a symbol matched more than one qualified name), "
    "affected (nodes that depend on the target, each {node, distance >= 1, "
    "resolution: 'exact'|'best_effort'}, deterministically ordered), "
    "affected_files (sorted distinct paths)}. An unresolved target returns empty "
    "'resolved' and 'affected'."
)
_EXPLAIN_DESCRIPTION = (
    "Use this to review the structure around a given symbol or file. Reach for "
    "this to understand a symbol or file in context before changing or extending "
    "it. "
    "Argument: 'target' is a dotted qualified name (e.g. 'pkg.module.Symbol') or "
    "a file path. "
    "Returns one JSON object: {target, resolved (the node(s) the target matched), "
    "ambiguous, explanations (one per resolved node, in the same order)}. Each "
    "explanation is {node, defines (symbols it contains), fan_out (what it "
    "depends on), fan_in (what depends on it), bases and subclasses (for a class "
    "node), fan_in_count, fan_out_count}. An unresolved target returns empty "
    "'resolved' and 'explanations'."
)
_REPORT_DESCRIPTION = (
    "Use this to explore the metadata of the codebase index itself. Introspects "
    "the index rather than the code; reach for this to learn what the index "
    "contains and how big it is before querying it. Takes no arguments. "
    "Returns one JSON object: {db_path, schema_version, languages (sorted list), "
    "files_by_language, nodes_by_kind, edges_by_kind (each a count map by kind), "
    "roots (the indexed source roots), branch and head_commit (the git state at "
    "index time, or null)}."
)
_EXPORT_DESCRIPTION = (
    "Use this to get access to the entire graph, for a whole-graph view or "
    "offline processing. The graph itself is a {meta, nodes, edges} document "
    "(meta is the same shape 'report' returns; nodes and edges are the "
    "Cytoscape shape — each node has an 'id', each edge a 'source' and "
    "'target'). "
    "Arguments: 'out' chooses the output file path (otherwise a temp file is "
    "written); 'inline' (default false) requests the graph inline. "
    "Returns one JSON object. By default (and whenever the graph exceeds the "
    "inline element cap) it writes the document to a file and returns a summary "
    "{path, node_count, edge_count, meta} — NOT the graph inline, because a full "
    "index is tens of megabytes, which an MCP response should not carry; when "
    "'inline=true' is overridden by the cap, a 'note' explains the fallback. "
    "With 'inline=true' on a small graph it returns {graph, node_count, "
    "edge_count, meta} with the full document in 'graph'."
)


def build_server(settings: Settings | None = None) -> MCPServer:
    """Build the MCP server and register the code-graph query tools.

    A single :class:`Settings` is resolved once (honoring the
    ``DUCKDB_GRAPH_*`` environment variables and any ``db`` passed from the
    CLI verb) and reused by every tool call, each opening its own
    :class:`GraphStore`.
    """
    resolved = settings or Settings()
    server: MCPServer = MCPServer(
        name="duckdb-graph",
        description=(
            "Indexes the structure of a codebase into a local graph and answers "
            "questions about the relationships across its symbols and files."
        ),
        instructions=(
            "Use this server when a task needs the structure of the codebase or "
            "the relationships between its symbols and files, rather than reading "
            "files one at a time. Pick the tool by intent:\n"
            "- `explain` — review the structure around a given symbol or file "
            "(what it defines, what it depends on, what depends on it).\n"
            "- `impact` — evaluate the implications of a change by finding "
            "everything that transitively depends on a symbol or file.\n"
            "- `report` — explore the metadata of the index itself (languages, "
            "node/edge kinds and counts, roots).\n"
            "- `export` — get access to the entire graph as JSON when a whole-"
            "graph view or offline processing is needed.\n"
            "Targets are a dotted qualified name (e.g. `pkg.module.Symbol`) or a "
            "file path. The graph covers code and Markdown pages. Every tool "
            "returns a single JSON object."
        ),
    )

    @server.tool(description=_IMPACT_DESCRIPTION)
    def impact(target: str, depth: int | None = None) -> dict:
        """Transitive reverse-dependency set of a symbol or file path."""
        store = GraphStore(settings=resolved)
        try:
            return store.impact(target, depth=depth).model_dump()
        finally:
            store.close()

    @server.tool(description=_EXPLAIN_DESCRIPTION)
    def explain(target: str) -> dict:
        """Structural neighbourhood of a symbol or file path."""
        store = GraphStore(settings=resolved)
        try:
            return store.explain(target).model_dump()
        finally:
            store.close()

    @server.tool(description=_REPORT_DESCRIPTION)
    def report() -> dict:
        """Introspect the index: db path, schema version, kinds, languages."""
        store = GraphStore(settings=resolved)
        try:
            return store.report().model_dump()
        finally:
            store.close()

    @server.tool(description=_EXPORT_DESCRIPTION)
    def export(out: str | None = None, inline: bool = False) -> dict:
        """Export the whole graph as JSON; file handoff by default."""
        store = GraphStore(settings=resolved)
        try:
            result = store.export()
        finally:
            store.close()
        node_count = len(result.nodes)
        edge_count = len(result.edges)
        summary: dict = {
            "node_count": node_count,
            "edge_count": edge_count,
            "meta": result.meta.model_dump(),
        }
        if inline and node_count + edge_count <= _INLINE_ELEMENT_CAP:
            summary["graph"] = result.model_dump()
            return summary
        if inline:
            summary["note"] = (
                f"graph has {node_count + edge_count} elements, over the "
                f"{_INLINE_ELEMENT_CAP} inline cap; written to a file instead."
            )
        if out is not None:
            path = Path(out)
        else:
            fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="duckdb-graph-")
            os.close(fd)
            path = Path(tmp_name)
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        summary["path"] = str(path)
        return summary

    return server


def run_stdio(settings: Settings | None = None) -> None:
    """Build the server and serve it over stdio until the client disconnects."""
    build_server(settings).run(transport="stdio")


def run_http(
    settings: Settings | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """Serve the tools over Streamable HTTP, binding loopback by default.

    Binds ``127.0.0.1:8000`` with the SDK's ``/mcp`` path unless ``host`` or
    ``port`` override it. The same ``build_server(settings)`` and the same four
    tools serve over both transports, so the query-only behaviour and per-tool
    JSON-object shapes are unchanged — only the wire transport differs from
    :func:`run_stdio`.
    """
    build_server(settings).run(transport="streamable-http", host=host, port=port)
