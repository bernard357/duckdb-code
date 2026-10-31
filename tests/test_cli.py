"""CLI smoke tests for index / report / impact / explain / mcp via CliRunner.

Builds a temp DB from the committed fixture, then exercises each verb's human
and ``--json`` forms against it. All offline and deterministic.
"""

import json
from pathlib import Path

import typer.main
from typer.testing import CliRunner

from duckdb_code.cli import app

runner = CliRunner()


def _click_command():
    """Return the underlying Click command for the Typer app.

    Help-output substring assertions are brittle: Click renders the options
    panel inside a box wrapped to the terminal width, so a long option name can
    be split across the border on a narrow or differently-sized terminal (this
    broke CI across Python/terminal differences). Inspecting the command's
    registered subcommands and parameters is rendering-, width-, and
    version-independent, so these tests assert structure, not layout.
    """
    return typer.main.get_command(app)


def _subcommand_names() -> set[str]:
    group = _click_command()
    return set(group.commands)  # type: ignore[attr-defined]


def _option_names(verb: str) -> set[str]:
    group = _click_command()
    cmd = group.commands[verb]  # type: ignore[attr-defined]
    names: set[str] = set()
    for param in cmd.params:
        names.update(param.opts)
        names.update(param.secondary_opts)
    return names


FIXTURE = Path(__file__).parent / "fixtures" / "sample"


def _index(tmp_path) -> str:
    db = str(tmp_path / "cli.duckdb")
    indexed = runner.invoke(app, ["index", str(FIXTURE), "--db", db])
    assert indexed.exit_code == 0, indexed.output
    assert "Indexed" in indexed.output
    return db


def test_help_lists_all_verbs():
    names = _subcommand_names()
    for verb in ("index", "watch", "report", "impact", "explain", "export", "mcp"):
        assert verb in names


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
    assert payload["schema_version"] == 3
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


def test_index_help_lists_rebuild_flag():
    assert "--rebuild" in _option_names("index")


def test_index_rebuild_forces_full_and_plain_rerun_is_incremental(tmp_path):
    db = str(tmp_path / "cli.duckdb")
    first = runner.invoke(app, ["index", str(FIXTURE), "--db", db, "--json"])
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["mode"] == "full"

    second = runner.invoke(app, ["index", str(FIXTURE), "--db", db, "--json"])
    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["mode"] == "incremental"

    forced = runner.invoke(
        app, ["index", str(FIXTURE), "--db", db, "--rebuild", "--json"]
    )
    assert forced.exit_code == 0, forced.output
    assert json.loads(forced.output)["mode"] == "full"


def test_export_stdout_json_shape(tmp_path):
    db = _index(tmp_path)
    result = runner.invoke(app, ["export", "--db", db])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload) == {"meta", "nodes", "edges"}


def test_export_out_writes_json_file(tmp_path):
    db = _index(tmp_path)
    out = tmp_path / "graph.json"
    result = runner.invoke(app, ["export", "--db", db, "--out", str(out)])
    assert result.exit_code == 0, result.output
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert set(payload) == {"meta", "nodes", "edges"}
    node_ids = {node["id"] for node in payload["nodes"]}
    for edge in payload["edges"]:
        assert edge["source"] in node_ids
        assert edge["target"] in node_ids


def test_mcp_verb_is_wired():
    # Smoke: the command exists and exposes its transport option; we do not
    # start the stdio loop.
    assert "mcp" in _subcommand_names()
    assert "--transport" in _option_names("mcp")


def test_mcp_http_transport_dispatches_to_run_http(monkeypatch):
    # The verb imports the runners lazily from duckdb_code.mcp_server; patch
    # them there and assert --transport http routes to run_http with the parsed
    # host/port and never touches run_stdio.
    import duckdb_code.mcp_server as mcp_server

    calls = {}
    monkeypatch.setattr(
        mcp_server,
        "run_http",
        lambda settings, *, host, port: calls.update(host=host, port=port),
    )
    monkeypatch.setattr(
        mcp_server, "run_stdio", lambda *a, **k: calls.update(stdio=True)
    )
    result = runner.invoke(
        app, ["mcp", "--transport", "http", "--host", "0.0.0.0", "--port", "9001"]
    )
    assert result.exit_code == 0, result.output
    assert calls == {"host": "0.0.0.0", "port": 9001}


def test_mcp_default_dispatches_to_run_stdio(monkeypatch):
    import duckdb_code.mcp_server as mcp_server

    calls = {}
    monkeypatch.setattr(mcp_server, "run_stdio", lambda *a, **k: calls.update(stdio=1))
    monkeypatch.setattr(mcp_server, "run_http", lambda *a, **k: calls.update(http=1))
    result = runner.invoke(app, ["mcp"])
    assert result.exit_code == 0, result.output
    assert calls == {"stdio": 1}


def test_mcp_help_lists_startup_index_flags():
    options = _option_names("mcp")
    assert "--index" in options
    assert "--no-startup-index" in options


def _patch_serve_noops(monkeypatch):
    """Patch both runners to no-ops so the verb returns after the startup build.

    Returns the ``calls`` dict so a test can assert which transport was reached
    (or that neither was, when the startup build fails).
    """
    import duckdb_code.mcp_server as mcp_server

    calls: dict = {}
    monkeypatch.setattr(mcp_server, "run_stdio", lambda *a, **k: calls.update(stdio=1))
    monkeypatch.setattr(mcp_server, "run_http", lambda *a, **k: calls.update(http=1))
    return calls


def _node_count(db: str) -> int:
    from duckdb_code.store import CodeStore

    store = CodeStore(db_path=db)
    try:
        return sum(store.report().nodes_by_kind.values())
    finally:
        store.close()


def test_mcp_index_builds_db_before_serving(tmp_path, monkeypatch):
    # --index runs the startup incremental build (populating an empty db), then
    # dispatches to the stdio runner. The build progress line is on stderr.
    calls = _patch_serve_noops(monkeypatch)
    db = str(tmp_path / "startup.duckdb")
    result = runner.invoke(app, ["mcp", "--db", db, "--index", str(FIXTURE)])
    assert result.exit_code == 0, result.output
    assert calls == {"stdio": 1}
    assert _node_count(db) > 0


def test_mcp_no_startup_index_skips_the_build(tmp_path, monkeypatch):
    # --no-startup-index leaves the db empty even though --index names a folder.
    calls = _patch_serve_noops(monkeypatch)
    db = str(tmp_path / "startup.duckdb")
    result = runner.invoke(
        app, ["mcp", "--db", db, "--index", str(FIXTURE), "--no-startup-index"]
    )
    assert result.exit_code == 0, result.output
    assert calls == {"stdio": 1}
    assert _node_count(db) == 0


def test_mcp_bare_db_does_not_reindex(tmp_path, monkeypatch):
    # A prebuilt db served with no --index is left exactly as-is.
    db = _index(tmp_path)
    before = _node_count(db)
    calls = _patch_serve_noops(monkeypatch)
    result = runner.invoke(app, ["mcp", "--db", db])
    assert result.exit_code == 0, result.output
    assert calls == {"stdio": 1}
    assert _node_count(db) == before


def test_mcp_failed_startup_index_aborts_without_serving(tmp_path, monkeypatch):
    # Pointing --index at a non-directory fails fast: the verb exits non-zero
    # and neither transport runner is ever reached.
    calls = _patch_serve_noops(monkeypatch)
    db = str(tmp_path / "startup.duckdb")
    missing = str(tmp_path / "does-not-exist")
    result = runner.invoke(app, ["mcp", "--db", db, "--index", missing])
    assert result.exit_code != 0
    assert calls == {}


def test_version_flag_prints_metadata_version():
    """`--version` exits 0 with the installed version, not the no-args help."""
    from importlib.metadata import version

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    printed = result.stdout.strip()
    assert printed, "no version printed"
    assert printed == version("duckdb-code")
    # It printed the version, not the help screen that no_args_is_help shows.
    assert "Usage:" not in result.stdout


def test_version_falls_back_to_sentinel(monkeypatch):
    """A missing distribution degrades to the sentinel, never a traceback."""
    import duckdb_code
    import duckdb_code.cli as cli_module

    monkeypatch.setattr(cli_module, "__version__", "0.0.0+unknown")
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "0.0.0+unknown"
    # Sanity: the resolver itself yields the sentinel on PackageNotFoundError.
    from importlib.metadata import PackageNotFoundError

    def _raise(_name):
        raise PackageNotFoundError

    monkeypatch.setattr("duckdb_code.version", _raise)
    assert duckdb_code._resolve_version() == "0.0.0+unknown"
