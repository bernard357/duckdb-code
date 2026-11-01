"""In-process tests for the MCP stdio server's impact/explain/report tools.

Offline and deterministic: the committed fixture is indexed into a temp DB and
the tools are exercised through the server's own ``call_tool`` / ``list_tools``
API (no real subprocess, no stdio pipe). Each tool returns exactly one JSON
object of the right shape.
"""

import asyncio
import inspect
import json
import re
from pathlib import Path

import pytest
from starlette.applications import Starlette

from duckdb_code.config import Settings
from duckdb_code.indexer import index_folder
from duckdb_code.mcp_server import build_server
from duckdb_code.models import EDGE_KINDS, NODE_KINDS
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
        "report",
        "export",
        "analytics",
        "index_status",
        "context_for_change",
    }
    # explain is intentionally not advertised over MCP (folded); it stays on
    # the SDK and CLI.
    assert "explain" not in names


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
        "report",
        "export",
        "analytics",
        "index_status",
        "context_for_change",
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


def test_explain_is_not_advertised_over_mcp(indexed_db):
    # explain is folded: no longer an MCP tool (its neighbourhood is covered by
    # overview/context_for_change), though it stays on the SDK and CLI. Calling
    # it over MCP fails rather than returning a result.
    from mcp.server.mcpserver.exceptions import ToolError

    server = _server(indexed_db)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "explain" not in names
    with pytest.raises(ToolError):
        asyncio.run(server.call_tool("explain", {"target": "payments.charge.Charge"}))


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
    # `kind` is a Literal, so a bad value is rejected by argument validation
    # before the tool runs; the SDK surfaces that as a ToolError naming the
    # allowed values, never as a normal result.
    from mcp.server.mcpserver.exceptions import ToolError

    server = _server(indexed_db)
    with pytest.raises(ToolError, match="Input should be 'cycles'"):
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


def test_context_for_change_tool_returns_the_contract_shape(indexed_db):
    # The sample fixture is not a git repo, so the diff degrades to empty — but
    # the tool still returns the well-formed {base, changed, neighbourhood}.
    payload = _result(indexed_db, "context_for_change", {})
    assert isinstance(payload, dict)
    assert set(payload) == {"base", "changed", "neighbourhood"}
    assert payload["base"] == "working-tree"
    assert payload["changed"] == []
    assert payload["neighbourhood"] == []


# Discovery strings: what a client reads before it calls any tool. These guard
# the server instructions and tool descriptions against drifting from the
# registered tool set (e.g. a tool folded off MCP but still advertised).

_BULLET_TOOL = re.compile(r"^- `([a-z_]+)` —", re.MULTILINE)
_BACKTICKED = re.compile(r"`+([a-z_]+)`+")


def _tools(db):
    return asyncio.run(_server(db).list_tools())


def test_instructions_route_exactly_the_registered_tools(indexed_db):
    server = _server(indexed_db)
    registered = {tool.name for tool in _tools(indexed_db)}
    routed = _BULLET_TOOL.findall(server.instructions)
    assert len(routed) == len(set(routed)), "a tool is routed twice"
    assert set(routed) == registered


def test_no_discovery_string_names_an_unregistered_tool(indexed_db):
    # Public CodeStore query methods that are NOT MCP tools (today: explain,
    # plus index/close) must never be named as if a client could call them.
    server = _server(indexed_db)
    tools = _tools(indexed_db)
    registered = {tool.name for tool in tools}
    store_methods = {
        name
        for name, member in inspect.getmembers(CodeStore, inspect.isfunction)
        if not name.startswith("_")
    }
    unregistered = store_methods - registered
    assert "explain" in unregistered  # the guard covers the folded tool
    texts = {
        "instructions": server.instructions,
        "description": server.description,
        **{f"tool:{tool.name}": tool.description for tool in tools},
    }
    for where, text in texts.items():
        named = set(_BACKTICKED.findall(text or ""))
        assert not (named & unregistered), f"{where} names {named & unregistered}"


def test_report_description_lists_every_kind(indexed_db):
    report = next(tool for tool in _tools(indexed_db) if tool.name == "report")
    for kind in (*NODE_KINDS, *EDGE_KINDS):
        assert re.search(rf"\b{kind}\b", report.description), kind


def test_analytics_schema_enumerates_kinds(indexed_db):
    analytics = next(tool for tool in _tools(indexed_db) if tool.name == "analytics")
    kind = analytics.input_schema["properties"]["kind"]
    assert kind["enum"] == ["cycles", "clusters", "dead_code", "hotspots"]
    assert analytics.input_schema["required"] == ["kind"]
