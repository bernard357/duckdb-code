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

from duckdb_codegraph.config import Settings
from duckdb_codegraph.indexer import index_folder
from duckdb_codegraph.mcp_server import build_server
from duckdb_codegraph.store import CodeGraphStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


@pytest.fixture
def indexed_db(tmp_path):
    db = str(tmp_path / "mcp.duckdb")
    writer = CodeGraphStore(db_path=db, root=str(FIXTURE))
    index_folder(writer, FIXTURE)
    writer.close()
    return db


def _server(db):
    return build_server(Settings(db_path=db, root=str(FIXTURE), read_only=True))


def _result(db, name, args):
    server = _server(db)
    outcome = asyncio.run(server.call_tool(name, args))
    assert outcome.is_error is False
    # Each tool returns exactly one JSON object, serialized into the content
    # text block; parse it back to assert the single-object shape.
    return json.loads(outcome.content[0].text)


def test_exactly_three_tools_registered(indexed_db):
    server = _server(indexed_db)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"impact", "explain", "report"}


def test_impact_tool_returns_one_impact_result(indexed_db):
    payload = _result(indexed_db, "impact", {"target": "payments.charge.Base"})
    assert isinstance(payload, dict)
    assert set(payload) == {
        "target",
        "resolved",
        "ambiguous",
        "affected",
        "affected_files",
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
    }
    assert payload["languages"] == ["python", "typescript"]
    assert payload["files_by_language"] == {"python": 5, "typescript": 2}
