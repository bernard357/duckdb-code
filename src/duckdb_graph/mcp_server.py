"""MCP-over-stdio server exposing duckdb-graph tools as read-only tools.

Any MCP client (Docker Agent, Claude Desktop, Claude Code, Cursor) can spawn
``duckdb-graph mcp`` as a subprocess and call the ``impact``, ``explain``,
and ``report`` tools. The server is built on the official ``mcp`` SDK.

Note on the import path: in the ``mcp`` 2.x SDK the server class is
``MCPServer`` (``from mcp.server.mcpserver import MCPServer``); its
``.run(transport="stdio")`` serves over stdio, matching the subprocess model
MCP clients use. This mirrors the sibling ``duckdb-vectors`` verbatim.

The serving contract is read-only: every tool opens the resolved
:class:`Settings` with ``read_only=True``, mirroring the baked-index lifecycle.
Each tool returns exactly one JSON object (an :class:`ImpactResult`,
:class:`ExplanationResult`, or :class:`Report` ``.model_dump()`` dict) — never a
bare ``Explanation`` and never a list — so a consuming agent parses one stable
shape per tool.
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


def build_server(settings: Settings | None = None) -> MCPServer:
    """Build the MCP server and register the read-only code-graph tools.

    A single :class:`Settings` is resolved once (honoring the
    ``DUCKDB_GRAPH_*`` environment variables and any ``db`` passed from the
    CLI verb) and reused by every tool call, each opening its own read-only
    :class:`GraphStore`.
    """
    resolved = settings or Settings()
    server: MCPServer = MCPServer(name="duckdb-graph")

    @server.tool()
    def impact(target: str, depth: int | None = None) -> dict:
        """Transitive reverse-dependency set of a symbol or file path."""
        store = GraphStore(settings=_read_only(resolved))
        try:
            return store.impact(target, depth=depth).model_dump()
        finally:
            store.close()

    @server.tool()
    def explain(target: str) -> dict:
        """Structural neighbourhood of a symbol or file path."""
        store = GraphStore(settings=_read_only(resolved))
        try:
            return store.explain(target).model_dump()
        finally:
            store.close()

    @server.tool()
    def report() -> dict:
        """Introspect the index: db path, schema version, kinds, languages."""
        store = GraphStore(settings=_read_only(resolved))
        try:
            return store.report().model_dump()
        finally:
            store.close()

    @server.tool()
    def export(out: str | None = None, inline: bool = False) -> dict:
        """Export the whole graph as JSON ({meta, nodes, edges}).

        By default writes the JSON to a file and returns a small summary
        ``{path, node_count, edge_count, meta}`` — NOT the graph inline, because
        a full index is tens of megabytes, which an MCP response should not
        carry. Pass ``out`` to choose the file path (otherwise a temp file is
        written). Pass ``inline=True`` to get the JSON directly in ``graph``,
        but only for a small graph (at or under the element cap); above the cap
        it falls back to the path handoff and says so in ``note``.
        """
        store = GraphStore(settings=_read_only(resolved))
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


def _read_only(settings: Settings) -> Settings:
    """Return a copy of ``settings`` forced to open the store read-only.

    Copies only the three fields :class:`Settings` actually has — ``db_path``,
    ``root``, ``read_only``. The sibling's ``backend``/``chunker``/
    ``vector_sql``/``text_sql`` do not exist here and must not be referenced
    (fix 7 of the design).
    """
    return Settings(
        db_path=settings.db_path,
        root=settings.root,
        read_only=True,
    )


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
    ``port`` override it. The same ``build_server(settings)`` and the same three
    read-only tools serve over both transports, so the read-only serving
    contract and per-tool JSON-object shapes are unchanged — only the wire
    transport differs from :func:`run_stdio`.
    """
    build_server(settings).run(transport="streamable-http", host=host, port=port)
