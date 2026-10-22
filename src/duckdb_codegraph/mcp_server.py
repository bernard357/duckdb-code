"""MCP-over-stdio server exposing duckdb-codegraph tools as read-only tools.

Any MCP client (Docker Agent, Claude Desktop, Claude Code, Cursor) can spawn
``duckdb-codegraph mcp`` as a subprocess and call the ``impact``, ``explain``,
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

from mcp.server.mcpserver import MCPServer

from duckdb_codegraph.config import Settings
from duckdb_codegraph.store import CodeGraphStore


def build_server(settings: Settings | None = None) -> MCPServer:
    """Build the MCP server and register the read-only code-graph tools.

    A single :class:`Settings` is resolved once (honoring the
    ``DUCKDB_CODEGRAPH_*`` environment variables and any ``db`` passed from the
    CLI verb) and reused by every tool call, each opening its own read-only
    :class:`CodeGraphStore`.
    """
    resolved = settings or Settings()
    server: MCPServer = MCPServer(name="duckdb-codegraph")

    @server.tool()
    def impact(target: str, depth: int | None = None) -> dict:
        """Transitive reverse-dependency set of a symbol or file path."""
        store = CodeGraphStore(settings=_read_only(resolved))
        try:
            return store.impact(target, depth=depth).model_dump()
        finally:
            store.close()

    @server.tool()
    def explain(target: str) -> dict:
        """Structural neighbourhood of a symbol or file path."""
        store = CodeGraphStore(settings=_read_only(resolved))
        try:
            return store.explain(target).model_dump()
        finally:
            store.close()

    @server.tool()
    def report() -> dict:
        """Introspect the index: db path, schema version, kinds, languages."""
        store = CodeGraphStore(settings=_read_only(resolved))
        try:
            return store.report().model_dump()
        finally:
            store.close()

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
