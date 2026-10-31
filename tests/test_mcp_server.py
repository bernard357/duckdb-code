"""In-process tests for the MCP stdio server's impact/explain/report tools.

Offline and deterministic: the committed fixture is indexed into a temp DB and
the tools are exercised through the server's own ``call_tool`` / ``list_tools``
API (no real subprocess, no stdio pipe). Each tool returns exactly one JSON
object of the right shape.
"""

import asyncio
import json
from pathlib import Path

import pytest
from starlette.applications import Starlette

from duckdb_code.config import Settings
from duckdb_code.indexer import index_folder
from duckdb_code.mcp_server import build_server
from duckdb_code.store import CodeStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


@pytest.fixture
def indexed_db(tmp_path):
    db = str(tmp_path / "mcp.duckdb")
    writer = CodeStore(db_path=db, root=str(FIXTURE))
    index_folder(writer, FIXTURE)
    writer.close()
    return db


def _server(db):
    return build_server(Settings(db_path=db, root=str(FIXTURE)))


def _result(db, name, args):
    server = _server(db)
    outcome = asyncio.run(server.call_tool(name, args))
    assert outcome.is_error is False
    # Each tool returns exactly one JSON object, serialized into the content
    # text block; parse it back to assert the single-object shape.
    return json.loads(outcome.content[0].text)


def test_exactly_twelve_tools_registered(indexed_db):
    server = _server(indexed_db)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {
        "search",
        "find_similar",
        "overview",
        "related_tests",
        "history",
        "links",
        "impact",
        "explain",
        "report",
        "export",
        "analytics",
        "index_status",
    }


def test_streamable_http_app_carries_the_tools(indexed_db):
    # HTTP wiring: the same build_server produces a Starlette app over the
    # Streamable HTTP transport, with the identical tools and no network
    # listener bound.
    server = _server(indexed_db)
    app = server.streamable_http_app()
    assert isinstance(app, Starlette)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {
        "search",
        "find_similar",
        "overview",
        "related_tests",
        "history",
        "links",
        "impact",
        "explain",
        "report",
        "export",
        "analytics",
        "index_status",
    }


def test_impact_tool_returns_one_impact_result(indexed_db):
    payload = _result(indexed_db, "impact", {"target": "payments.charge.Base"})
    assert isinstance(payload, dict)
    assert set(payload) == {
        "target",
        "resolved",
        "ambiguous",
        "affected",
        "affected_files",
        "affected_by_module",
        "affected_entry_points",
        "related_tests",
    }
    affected_qns = {item["node"]["qualified_name"] for item in payload["affected"]}
    assert "payments.charge.Charge" in affected_qns


def test_explain_tool_returns_one_explanation_result(indexed_db):
    payload = _result(indexed_db, "explain", {"target": "payments.charge.Charge"})
    assert isinstance(payload, dict)
    # A single ExplanationResult wrapper, never a bare Explanation / a list.
    assert set(payload) == {"target", "resolved", "ambiguous", "explanations"}
    assert len(payload["explanations"]) == 1
    assert [n["name"] for n in payload["explanations"][0]["defines"]] == ["run"]


def test_report_tool_returns_one_report(indexed_db):
    payload = _result(indexed_db, "report", {})
    assert isinstance(payload, dict)
    assert set(payload) == {
        "db_path",
        "schema_version",
        "languages",
        "files_by_language",
        "nodes_by_kind",
        "edges_by_kind",
        "roots",
        "branch",
        "head_commit",
    }
    assert payload["languages"] == ["python", "typescript"]
    assert payload["files_by_language"] == {"python": 5, "typescript": 2}


def test_export_tool_writes_path_by_default(indexed_db, tmp_path):
    out = str(tmp_path / "graph.json")
    payload = _result(indexed_db, "export", {"out": out})
    # Default: a summary with the path and counts, NOT the inline graph.
    assert payload["path"] == out
    assert "graph" not in payload
    assert payload["node_count"] > 0
    assert payload["edge_count"] > 0
    assert set(payload["meta"]) >= {"db_path", "languages", "nodes_by_kind"}
    # The written file is a valid {meta, nodes, edges} document whose edge
    # source/target reference real node ids.
    doc = json.loads(Path(out).read_text())
    assert set(doc) >= {"meta", "nodes", "edges"}
    ids = {n["id"] for n in doc["nodes"]}
    assert doc["edges"], "fixture should have edges"
    for edge in doc["edges"]:
        assert edge["source"] in ids
        assert edge["target"] in ids


def test_export_tool_inlines_a_small_graph(indexed_db):
    # The sample fixture is small (well under the inline cap), so inline=True
    # returns the graph directly.
    payload = _result(indexed_db, "export", {"inline": True})
    assert "graph" in payload
    assert set(payload["graph"]) >= {"meta", "nodes", "edges"}
    assert payload["node_count"] == len(payload["graph"]["nodes"])


def test_export_tool_inline_cap_falls_back_to_path(indexed_db, monkeypatch):
    # Force the inline cap below the fixture size: inline=True must fall back to
    # a path handoff with an explanatory note, never dumping an oversized blob.
    import duckdb_code.mcp_server as mcp_mod

    monkeypatch.setattr(mcp_mod, "_INLINE_ELEMENT_CAP", 1)
    payload = _result(indexed_db, "export", {"inline": True})
    assert "graph" not in payload
    assert "path" in payload
    assert "note" in payload
    assert Path(payload["path"]).exists()


def test_startup_index_populates_an_empty_db(tmp_path):
    # Mirrors the `mcp --index` startup build: an empty db is populated by an
    # incremental build (which full-builds when empty), then the report tool
    # sees the graph.
    db = str(tmp_path / "startup.duckdb")
    store = CodeStore(db_path=db, root=str(FIXTURE))
    report = index_folder(store, FIXTURE, rebuild=False)
    store.close()
    assert report.nodes > 0
    assert report.edges > 0

    payload = _result(db, "report", {})
    assert payload["nodes_by_kind"]
    assert payload["files_by_language"] == {"python": 5, "typescript": 2}


def test_startup_index_second_run_is_a_noop(tmp_path):
    # A second incremental build over the unchanged folder leaves the graph
    # totals identical — the startup index is idempotent.
    db = str(tmp_path / "startup.duckdb")
    store = CodeStore(db_path=db, root=str(FIXTURE))
    first = index_folder(store, FIXTURE, rebuild=False)
    second = index_folder(store, FIXTURE, rebuild=False)
    store.close()
    assert (second.nodes, second.edges) == (first.nodes, first.edges)
    assert second.mode == "incremental"


def test_store_opened_for_serving_still_accepts_writes(tmp_path):
    # The serving path opens the store read-write (read-only was removed), so a
    # store opened the same way the server opens it can still be written.
    db = str(tmp_path / "startup.duckdb")
    store = CodeStore(db_path=db, root=str(FIXTURE))
    index_folder(store, FIXTURE, rebuild=False)
    # A second index on the same open store writes without any guard refusing.
    report = index_folder(store, FIXTURE, rebuild=True)
    store.close()
    assert report.nodes > 0


def test_analytics_tool_returns_one_analytics_result(indexed_db):
    payload = _result(indexed_db, "analytics", {"kind": "dead_code"})
    assert isinstance(payload, dict)
    assert set(payload) == {"kind", "results"}
    assert payload["kind"] == "dead_code"
    assert isinstance(payload["results"], list)


def test_analytics_tool_errors_on_unknown_kind(indexed_db):
    # A bad kind is a deliberate ToolError: the SDK surfaces it by raising
    # (with the message preserved), not as a normal result.
    from mcp.server.mcpserver.exceptions import ToolError

    server = _server(indexed_db)
    with pytest.raises(ToolError, match="unknown analytics kind"):
        asyncio.run(server.call_tool("analytics", {"kind": "bogus"}))


def test_index_status_tool_returns_one_status(indexed_db):
    payload = _result(indexed_db, "index_status", {})
    assert isinstance(payload, dict)
    assert set(payload) == {
        "indexed_commit",
        "branch",
        "dirty",
        "indexed_at",
        "node_count",
        "edge_count",
    }
    assert payload["node_count"] > 0
