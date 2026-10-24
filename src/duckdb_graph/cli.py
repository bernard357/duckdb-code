"""Command-line interface for duckdb-graph.

Six verbs: ``index`` a folder of source code, ``report`` on database content,
``impact`` the reverse-dependency set of a target, ``explain`` a target's
structure, ``export`` the whole graph in the viewer's Cytoscape shape, and
``mcp`` to serve the read-only tools over stdio. All honor the
``DUCKDB_GRAPH_*`` environment variables and the ``--db`` override. Every
serving verb opens the store read-only; only ``index`` opens it writable.
"""

from pathlib import Path

import typer

from duckdb_graph.indexer import index_folder
from duckdb_graph.store import GraphStore

app = typer.Typer(
    help="Embeddable code-graph impact analysis over a local DuckDB database.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def index(
    folder: str = typer.Argument(..., help="Folder of source code."),
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    recursive: bool = typer.Option(
        True, "--recursive/--no-recursive", help="Recurse into sub-folders."
    ),
    rebuild: bool = typer.Option(
        False,
        "--rebuild/--no-rebuild",
        help="Force a full rebuild instead of the default incremental re-index.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Index a folder of source code into the database.

    Incremental-by-default: a full rebuild runs when the database is empty or
    ``--rebuild`` is passed; otherwise only changed, added, and deleted files
    are reconciled against the stored etags.
    """
    store = GraphStore(db_path=db)
    try:
        report = index_folder(store, folder, recursive=recursive, rebuild=rebuild)
    finally:
        store.close()
    if as_json:
        typer.echo(report.model_dump_json())
    elif report.mode == "incremental":
        typer.echo(
            f"Incremental: {report.rebuilt} rebuilt, {report.deleted} deleted, "
            f"{report.files} files "
            f"({report.indexed} indexed, {report.skipped} skipped), "
            f"{report.nodes} nodes, {report.edges} edges."
        )
    else:
        typer.echo(
            f"Indexed {report.files} files "
            f"({report.indexed} indexed, {report.skipped} skipped), "
            f"{report.nodes} nodes, {report.edges} edges."
        )


@app.command()
def report(
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Report on database content."""
    store = GraphStore(db_path=db, read_only=True)
    try:
        result = store.report()
    finally:
        store.close()
    if as_json:
        typer.echo(result.model_dump_json())
    else:
        typer.echo(f"Database      : {result.db_path}")
        typer.echo(f"Schema version: {result.schema_version}")
        typer.echo(f"Roots         : {', '.join(result.roots)}")
        typer.echo(f"Languages     : {', '.join(result.languages)}")
        for language, count in result.files_by_language.items():
            typer.echo(f"  {language}: {count} files")
        for kind, count in result.nodes_by_kind.items():
            typer.echo(f"  node {kind}: {count}")
        for kind, count in result.edges_by_kind.items():
            typer.echo(f"  edge {kind}: {count}")


@app.command()
def impact(
    target: str = typer.Argument(..., help="Symbol or file path."),
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    depth: int | None = typer.Option(
        None, "--depth", help="Maximum reverse-hop distance."
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Report the transitive reverse-dependency set of a target."""
    store = GraphStore(db_path=db, read_only=True)
    try:
        result = store.impact(target, depth=depth)
    finally:
        store.close()
    if as_json:
        typer.echo(result.model_dump_json())
    else:
        if result.ambiguous:
            typer.echo(f"(ambiguous: {len(result.resolved)} resolved nodes)")
        typer.echo(f"Resolved      : {len(result.resolved)} node(s)")
        typer.echo(f"Affected      : {len(result.affected)} node(s)")
        for item in result.affected:
            typer.echo(
                f"  {item.distance}  {item.resolution}  {item.node.kind}  "
                f"{item.node.qualified_name}  {item.node.path}"
            )


@app.command()
def explain(
    target: str = typer.Argument(..., help="Symbol or file path."),
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Explain the structural neighbourhood of a target."""
    store = GraphStore(db_path=db, read_only=True)
    try:
        result = store.explain(target)
    finally:
        store.close()
    if as_json:
        typer.echo(result.model_dump_json())
    else:
        if result.ambiguous:
            typer.echo(f"(ambiguous: {len(result.resolved)} resolved nodes)")
        for explanation in result.explanations:
            node = explanation.node
            typer.echo(f"{node.kind} {node.qualified_name} ({node.path})")
            typer.echo(
                f"  fan_in: {explanation.fan_in_count}  "
                f"fan_out: {explanation.fan_out_count}"
            )
            for defined in explanation.defines:
                typer.echo(f"  defines : {defined.qualified_name}")
            for dependency in explanation.fan_out:
                typer.echo(f"  depends : {dependency.qualified_name}")
            for dependent in explanation.fan_in:
                typer.echo(f"  used by : {dependent.qualified_name}")
            for base in explanation.bases:
                typer.echo(f"  base    : {base.qualified_name}")
            for subclass in explanation.subclasses:
                typer.echo(f"  subclass: {subclass.qualified_name}")


@app.command()
def export(
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    out: str | None = typer.Option(
        None, "--out", help="Output JSON path (default: stdout)."
    ),
) -> None:
    """Export the whole graph in the viewer's Cytoscape JSON shape."""
    store = GraphStore(db_path=db, read_only=True)
    try:
        result = store.export()
    finally:
        store.close()
    text = result.model_dump_json(indent=2)
    if out is not None:
        Path(out).write_text(text, encoding="utf-8")
    else:
        typer.echo(text)


@app.command()
def mcp(
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    transport: str = typer.Option(
        "stdio", "--transport", help="Transport to serve over: stdio|http."
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="HTTP bind host (http transport only)."
    ),
    port: int = typer.Option(
        8000, "--port", help="HTTP bind port (http transport only)."
    ),
) -> None:
    """Serve the read-only code-graph tools as an MCP server.

    Defaults to stdio, the same-container subprocess transport. Pass
    ``--transport http`` to serve Streamable HTTP, which binds loopback
    ``127.0.0.1:8000`` (path ``/mcp``) by default. Serving stays read-only over
    both transports.
    """
    from duckdb_graph.config import Settings
    from duckdb_graph.mcp_server import run_http, run_stdio

    settings = Settings(db_path=db, read_only=True)
    if transport == "http":
        run_http(settings, host=host, port=port)
    elif transport == "stdio":
        run_stdio(settings)
    else:
        raise typer.BadParameter(
            f"unknown transport {transport!r}: choose 'stdio' or 'http'",
            param_hint="--transport",
        )


if __name__ == "__main__":
    app()
