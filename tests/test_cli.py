"""CLI smoke tests for index / report / impact / explain / mcp via CliRunner.

Builds a temp DB from the committed fixture, then exercises each verb's human
and ``--json`` forms against it. All offline and deterministic.
"""

import json
from pathlib import Path

from typer.testing import CliRunner

from duckdb_codegraph.cli import app

runner = CliRunner()

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


def _index(tmp_path) -> str:
    db = str(tmp_path / "cli.duckdb")
    indexed = runner.invoke(app, ["index", str(FIXTURE), "--db", db])
    assert indexed.exit_code == 0, indexed.output
    assert "Indexed" in indexed.output
    return db


def test_help_lists_all_five_verbs():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    for verb in ("index", "report", "impact", "explain", "mcp"):
        assert verb in result.output


def test_index_json_returns_expected_counts(tmp_path):
    db = str(tmp_path / "cli.duckdb")
    result = runner.invoke(app, ["index", str(FIXTURE), "--db", db, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["skipped"] == 2
    assert payload["indexed"] == payload["files"] - payload["skipped"]
    assert payload["languages"] == {"python": 5, "typescript": 2}
    assert payload["nodes"] > 0
    assert payload["edges"] > 0


def test_report_json_shape(tmp_path):
    db = _index(tmp_path)
    result = runner.invoke(app, ["report", "--db", db, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["languages"] == ["python", "typescript"]
    assert payload["files_by_language"] == {"python": 5, "typescript": 2}
    assert payload["nodes_by_kind"]["class"] == 4
    assert payload["edges_by_kind"]["inherits"] == 2


def test_impact_json_shape(tmp_path):
    db = _index(tmp_path)
    result = runner.invoke(
        app, ["impact", "payments.charge.Base", "--db", db, "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["target"] == "payments.charge.Base"
    assert [n["qualified_name"] for n in payload["resolved"]] == [
        "payments.charge.Base"
    ]
    affected_qns = {item["node"]["qualified_name"] for item in payload["affected"]}
    assert "payments.charge.Charge" in affected_qns
    assert "payments.premium.Premium" in affected_qns


def test_impact_human_form_lists_columns(tmp_path):
    db = _index(tmp_path)
    result = runner.invoke(app, ["impact", "payments.charge.Base", "--db", db])
    assert result.exit_code == 0, result.output
    # Human form lists: distance  resolution  kind  qualified_name  path.
    assert "payments.charge.Charge" in result.output
    assert "payments/charge.py" in result.output
    assert "exact" in result.output


def test_explain_json_single_wrapper(tmp_path):
    db = _index(tmp_path)
    result = runner.invoke(
        app, ["explain", "payments.charge.Charge", "--db", db, "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # Exactly one wrapper object with one explanation (never a list / bare one).
    assert set(payload) == {"target", "resolved", "ambiguous", "explanations"}
    assert len(payload["explanations"]) == 1
    explanation = payload["explanations"][0]
    assert [n["name"] for n in explanation["defines"]] == ["run"]
    assert [n["qualified_name"] for n in explanation["bases"]] == [
        "payments.charge.Base"
    ]


def test_explain_human_form_prints_counts(tmp_path):
    db = _index(tmp_path)
    result = runner.invoke(app, ["explain", "payments.charge.Charge", "--db", db])
    assert result.exit_code == 0, result.output
    assert "fan_in:" in result.output
    assert "fan_out:" in result.output


def test_impact_depth_zero_has_no_affected(tmp_path):
    db = _index(tmp_path)
    result = runner.invoke(
        app, ["impact", "payments.charge.Charge", "--db", db, "--depth", "0", "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["affected"] == []


def test_mcp_verb_is_wired():
    # Smoke: the command exists and is listed; we do not start the stdio loop.
    result = runner.invoke(app, ["mcp", "--help"])
    assert result.exit_code == 0, result.output
    assert "stdio" in result.output.lower()
