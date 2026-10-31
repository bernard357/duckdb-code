"""The `links` tool: cross-boundary edges (issue #32).

Indexes the same cross-language scenario as `test_routes.py` — a Python
FastAPI server and a TypeScript client calling its routes — and asserts
`links(symbol)` surfaces the `calls_route` edge from either endpoint, with the
right from/to symbols and resolution, while an unrelated symbol carries no
cross-boundary link.
"""

import asyncio
import json
import textwrap

from duckdb_code.config import Settings
from duckdb_code.indexer import index_folder
from duckdb_code.mcp_server import build_server
from duckdb_code.store import CodeStore


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def _app_tree(tmp_path):
    """Build the FastAPI-server + TypeScript-client tree and index it."""
    tree = tmp_path / "app"
    tree.mkdir()
    _write(
        tree / "api.py",
        """
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/orders")
        def list_orders():
            return []

        @app.post("/orders")
        def create_order():
            return {}
        """,
    )
    _write(
        tree / "client.ts",
        """
        export async function load() {
          const r = await fetch("/orders");
          await axios.post("/orders", {});
          return r;
        }
        """,
    )
    store = CodeStore(db_path=str(tmp_path / "links.duckdb"), root=str(tree))
    index_folder(store, tree)
    return store


def test_links_surface_calls_route_from_the_client(tmp_path):
    store = _app_tree(tmp_path)
    try:
        result = store.links("client.ts")
    finally:
        store.close()
    assert result.resolved, "the client file should resolve"
    kinds = {link.kind for link in result.links}
    assert kinds == {"calls_route"}
    # Each client call is bound to a server route: source is the client file,
    # target the server route node, exact (one route per signature).
    dst_sigs = {link.to.qualified_name for link in result.links}
    assert dst_sigs == {"GET /orders", "POST /orders"}
    assert all(link.source.path == "client.ts" for link in result.links)
    assert all(link.to.path == "api.py" for link in result.links)
    assert all(link.resolution == "exact" for link in result.links)
    assert all(link.confidence == 1.0 for link in result.links)


def test_links_surface_calls_route_from_the_server_side(tmp_path):
    store = _app_tree(tmp_path)
    try:
        # Asking from the server file (the route endpoint) must also surface
        # the edge — links collects both directions, so the server sees its
        # inbound client calls.
        result = store.links("api.py")
    finally:
        store.close()
    assert result.resolved
    kinds = {link.kind for link in result.links}
    assert kinds == {"calls_route"}
    # The route nodes live in api.py and are the `to` endpoint; the client
    # calls in client.ts are the `from` endpoint.
    assert {link.to.qualified_name for link in result.links} == {
        "GET /orders",
        "POST /orders",
    }
    assert all(link.source.path == "client.ts" for link in result.links)


def test_unrelated_symbol_has_no_cross_boundary_links(tmp_path):
    store = _app_tree(tmp_path)
    try:
        # The server handler function has no cross-boundary edge of its own.
        result = store.links("list_orders")
    finally:
        store.close()
    assert result.resolved, "the handler should still resolve"
    assert result.links == []


def test_unresolved_target_returns_empty(tmp_path):
    store = _app_tree(tmp_path)
    try:
        result = store.links("nothing.here.at.all")
    finally:
        store.close()
    assert result.resolved == []
    assert result.links == []


def test_links_mcp_tool_emits_the_contract_shape(tmp_path):
    db = str(tmp_path / "links.duckdb")
    _app_tree(tmp_path).close()
    tree = str(tmp_path / "app")
    server = build_server(Settings(db_path=db, root=tree))
    outcome = asyncio.run(server.call_tool("links", {"target": "client.ts"}))
    assert outcome.is_error is False
    payload = json.loads(outcome.content[0].text)
    assert set(payload) == {"symbol", "resolved", "links"}
    assert payload["links"], "the client should carry calls_route links"
    for link in payload["links"]:
        # The JSON uses the contract key `from` (aliased), not the Python
        # field name `source`.
        assert set(link) == {
            "kind",
            "from",
            "to",
            "path",
            "range",
            "resolution",
            "confidence",
        }
        assert "source" not in link
        assert link["kind"] == "calls_route"
        assert link["from"]["path"] == "client.ts"
        assert link["to"]["qualified_name"] in {"GET /orders", "POST /orders"}


def test_links_surface_calls_route_from_the_client_function(tmp_path):
    store = _app_tree(tmp_path)
    try:
        # Querying by the client *function* that contains the fetch/axios
        # calls must surface the edges: the JS extractor anchors a call to its
        # enclosing function, not the module, so `links("load")` is the natural
        # query an agent makes from the call site.
        result = store.links("load")
    finally:
        store.close()
    assert result.resolved, "the client function should resolve"
    assert all(node.kind == "function" for node in result.resolved)
    assert {link.kind for link in result.links} == {"calls_route"}
    assert {link.to.qualified_name for link in result.links} == {
        "GET /orders",
        "POST /orders",
    }
    # The edge is anchored on the function node, in both directions.
    assert all(link.source.qualified_name == "client.load" for link in result.links)


def test_links_resolve_a_route_by_method_and_path(tmp_path):
    store = _app_tree(tmp_path)
    try:
        # A `METHOD /path` string resolves to the route node (not a file path),
        # so the cross-boundary edge is reachable from the server endpoint by
        # its route signature.
        result = store.links("GET /orders")
    finally:
        store.close()
    assert len(result.resolved) == 1
    assert result.resolved[0].kind == "route"
    assert result.resolved[0].qualified_name == "GET /orders"
    assert {link.kind for link in result.links} == {"calls_route"}
    assert {link.to.qualified_name for link in result.links} == {"GET /orders"}
    assert all(link.source.qualified_name == "client.load" for link in result.links)
