"""Cross-boundary HTTP route edges (issue #26 / #32 data source).

Indexes a tiny tree with a Python/FastAPI server and a TypeScript client that
calls its routes, and asserts `route` nodes and resolved `calls_route` edges
connect the two across languages — the link LSP never sees.
"""

import textwrap

from duckdb_code.indexer import index_folder
from duckdb_code.store import CodeStore


def _write(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")


def test_client_call_binds_to_server_route_across_languages(tmp_path):
    tree = tmp_path / "app"
    tree.mkdir()
    # Python FastAPI server defining two routes.
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
    # TypeScript client calling them.
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
    store = CodeStore(db_path=str(tmp_path / "routes.duckdb"), root=str(tree))
    index_folder(store, tree)
    con = store._con

    # Two route nodes on the Python side.
    routes = dict(
        con.execute(
            "SELECT qualified_name, path FROM nodes WHERE kind = 'route'"
        ).fetchall()
    )
    assert set(routes) == {"GET /orders", "POST /orders"}
    assert all(p == "api.py" for p in routes.values())

    # The client's calls_route edges resolved to the server route nodes
    # (cross-language, cross-file), one GET and one POST.
    bound = con.execute(
        """
        SELECT s.path, d.qualified_name
        FROM edges e
        JOIN nodes s ON s.node_id = e.src_id
        JOIN nodes d ON d.node_id = e.dst_id
        WHERE e.kind = 'calls_route'
        """
    ).fetchall()
    dst_sigs = {d for _s, d in bound}
    assert dst_sigs == {"GET /orders", "POST /orders"}
    # The source is the client file, the target the server file: cross-boundary.
    assert all(s == "client.ts" for s, _d in bound)


def test_unmatched_client_call_is_dropped(tmp_path):
    tree = tmp_path / "app2"
    tree.mkdir()
    _write(
        tree / "client.ts",
        """
        export async function load() {
          return fetch("/nowhere");
        }
        """,
    )
    store = CodeStore(db_path=str(tmp_path / "routes2.duckdb"), root=str(tree))
    index_folder(store, tree)
    con = store._con
    # No server route for "/nowhere", so the calls_route edge is dropped.
    cr = con.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'calls_route'"
    ).fetchone()[0]
    assert cr == 0
    store.close()
