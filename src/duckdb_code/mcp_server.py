"""MCP-over-stdio server exposing duckdb-code query tools.

Any MCP client (Docker Agent, Claude Desktop, Claude Code, Cursor) can spawn
``duckdb-code mcp`` as a subprocess and call the five tools ``search``,
``impact``, ``explain``, ``report``, and ``export``. The server also advertises
a
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

import inspect
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from duckdb_code.config import Settings
from duckdb_code.store import CodeStore

# Inline JSON is only returned when the graph is at or under this node+edge
# count. A full index runs to ~100k edges / tens of MB of JSON — far past what
# an MCP tool response should carry inline — so the default is a path handoff
# and inline is a bounded convenience for small graphs only.
_INLINE_ELEMENT_CAP = 2000


def _described(server: MCPServer) -> Callable:
    """Register a tool whose client description is its own cleaned docstring.

    The SDK uses ``fn.__doc__`` verbatim, and ruff indents a docstring's
    continuation lines to the code block — so a raw docstring would leak that
    indentation into the text a client reads. Running :func:`inspect.cleandoc`
    over the docstring strips the common leading whitespace, letting each
    tool's full, formatted docstring double as its advertised description with
    no drift between the two.
    """

    def register(fn: Callable) -> Callable:
        description = inspect.cleandoc(fn.__doc__ or "")
        return server.tool(description=description)(fn)

    return register


def build_server(settings: Settings | None = None) -> MCPServer:
    """Build the MCP server and register the code-intelligence query tools.

    A single :class:`Settings` is resolved once (honoring the
    ``DUCKDB_CODE_*`` environment variables and any ``db`` passed from the
    CLI verb) and reused by every tool call, each opening its own
    :class:`CodeStore`.
    """
    resolved = settings or Settings()
    server: MCPServer = MCPServer(
        name="duckdb-code",
        description=(
            "Indexes the structure of a codebase into a local graph and answers "
            "questions about the relationships across its symbols and files."
        ),
        instructions=(
            "Use this server when a task needs the structure of the codebase or "
            "the relationships between its symbols and files, rather than reading "
            "files one at a time. Pick the tool by intent:\n"
            "- `search` — find symbols by intent when you don't know the name "
            "(ranked semantic search); the first tool to reach for.\n"
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
            "returns a single JSON object, and every result carries a path and "
            "line range so you can hand a precise follow-up to a language "
            "server: this server answers where in the repo and what's at stake, "
            "not what exactly is at a cursor position."
        ),
    )

    @_described(server)
    def search(query: str, limit: int = 10) -> dict:
        """Use this to find symbols by intent when you don't know the name.

        Ranked semantic search over the indexed symbols — answers "where do we
        retry failed payments?" or "the http client setup" without a known
        identifier. This is the first tool to reach for when orienting in an
        unfamiliar codebase; follow a hit's ``path``/``range`` to the source or
        to `explain`/`impact` for structure and blast radius.

        Arguments:
        - query: a natural-language or keyword description of what you are
          looking for.
        - limit: maximum number of ranked results (default 10).

        Returns one JSON object with:
        - query: the query string, echoed back.
        - results: symbols ranked most-relevant first, each
          {node, score} where ``node`` carries node_id, kind, name,
          qualified_name, path, start_line, end_line, language, and ``score``
          is the cosine similarity of the symbol to the query (higher is
          closer). Ties break by qualified_name then node_id for a stable
          order.

        Returns an empty ``results`` list when the index carries no embeddings
        (built without the embedding backend) or nothing matches.
        """
        store = CodeStore(settings=resolved)
        try:
            return store.search(query, limit=limit).model_dump()
        finally:
            store.close()

    @_described(server)
    def impact(target: str, depth: int | None = None) -> dict:
        """Use this to evaluate the implications of a change.

        Returns the transitive reverse-dependency set of a symbol or file path:
        everything that depends on the target, directly or indirectly, so you
        can scope the blast radius before editing it.

        Arguments:
        - target: a dotted qualified name (e.g. ``pkg.module.Symbol``) or a file
          path. A path seeds every node stored under it (the file plus the
          symbols in it); a symbol is matched by exact qualified name first,
          then by a best-effort suffix match.
        - depth: optional cap on how many reverse hops to walk. ``depth=0``
          yields no affected nodes; omit it to walk the full closure.

        Returns one JSON object with:
        - target: the target string, echoed back.
        - resolved: the node(s) the target matched (empty when nothing matched).
          Each node carries node_id, kind, name, qualified_name, path,
          start_line, end_line, language.
        - ambiguous: true when a symbol target matched more than one distinct
          qualified name (the result then spans all of them).
        - affected: the nodes that depend on the target, ordered by ascending
          distance then path. Each item is {node, distance, resolution} where
          distance is the shortest reverse-hop count from the target (always
          >= 1, because the seeds themselves are distance 0 and never emitted),
          and resolution is "exact" when the node is reached through exact binds
          from an exact/seed origin, else "best_effort" (a node reached only via
          a suffix-matched seed or a best-effort edge). exact wins an
          equal-distance tie.
        - affected_files: the sorted distinct paths of the affected nodes.

        An unresolved target returns empty ``resolved`` and ``affected``.
        """
        store = CodeStore(settings=resolved)
        try:
            return store.impact(target, depth=depth).model_dump()
        finally:
            store.close()

    @_described(server)
    def explain(target: str) -> dict:
        """Use this to review the structure around a given symbol or file.

        Reach for this to understand a symbol or file in context — what it is
        made of and how it connects — before changing or extending it.

        Argument:
        - target: a dotted qualified name (e.g. ``pkg.module.Symbol``) or a file
          path, resolved the same way as ``impact``.

        Returns one JSON object with:
        - target: the target string, echoed back.
        - resolved: the node(s) the target matched, in the same order as
          ``explanations`` (empty when nothing matched).
        - ambiguous: true when a symbol target matched more than one distinct
          qualified name.
        - explanations: one entry per resolved node, each with:
          - node: the resolved node itself.
          - defines: the symbols this node contains (a file/module/class's
            members) — the one forward read of the ``defines`` relation.
          - fan_out: its direct dependencies (nodes this one points to).
          - fan_in: its direct dependents (nodes that point to this one).
          - bases / subclasses: for a class node, its parent and child classes
            via the inherits relation; empty for non-class nodes.
          - fan_in_count / fan_out_count: the sizes of fan_in / fan_out.

        An unresolved target returns empty ``resolved`` and ``explanations``.
        """
        store = CodeStore(settings=resolved)
        try:
            return store.explain(target).model_dump()
        finally:
            store.close()

    @_described(server)
    def report() -> dict:
        """Use this to explore the metadata of the codebase index itself.

        Introspects the index rather than the code; reach for this to learn what
        the index contains and how big it is before querying it. Takes no
        arguments.

        Returns one JSON object with:
        - db_path: the DuckDB file backing this index.
        - schema_version: the index schema version (an integer).
        - languages: the sorted list of languages present in the index.
        - files_by_language: a map of language -> indexed file count.
        - nodes_by_kind: a map of node kind (file, module, class, function,
          import, page, heading, tag) -> count.
        - edges_by_kind: a map of edge kind (defines, imports, calls, inherits,
          references, links_to, embeds, tagged) -> count.
        - roots: the indexed source root(s) — the configured root when set,
          otherwise inferred from the stored file paths.
        - branch / head_commit: the git branch and commit the index was built
          from, or null when the source was not a git working tree.
        """
        store = CodeStore(settings=resolved)
        try:
            return store.report().model_dump()
        finally:
            store.close()

    @_described(server)
    def export(out: str | None = None, inline: bool = False) -> dict:
        """Use this to get access to the entire graph.

        For a whole-graph view or offline processing. The graph document is
        {meta, nodes, edges}: ``meta`` is the same shape ``report`` returns, and
        ``nodes``/``edges`` use the Cytoscape shape — each node carries an
        ``id`` (the renamed node_id) plus kind/name/qualified_name/path/
        language/start_line/end_line, and each edge carries ``source`` and
        ``target`` (the renamed src_id/dst_id) plus kind and resolution.

        Arguments:
        - out: output file path for the JSON document; when omitted a temp file
          is written.
        - inline: default false. When true, request the document inline rather
          than via a file.

        Returns one JSON object. By default — and whenever the graph exceeds the
        inline element cap — it writes the document to a file and returns a
        summary: {path (where the document was written), node_count, edge_count,
        meta}. It does NOT inline the graph by default because a full index is
        tens of megabytes, too large for an MCP response. When ``inline=true``
        but the graph is over the cap, the same file summary is returned with an
        extra ``note`` explaining the fallback. When ``inline=true`` and the
        graph is small enough, it returns {graph (the full {meta, nodes, edges}
        document), node_count, edge_count, meta}.
        """
        store = CodeStore(settings=resolved)
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
            fd, tmp_name = tempfile.mkstemp(suffix=".json", prefix="duckdb-code-")
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
