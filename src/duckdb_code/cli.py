"""Command-line interface for duckdb-code.

Six verbs: ``index`` a folder of source code, ``report`` on database content,
``impact`` the reverse-dependency set of a target, ``explain`` a target's
structure, ``export`` the whole graph in the viewer's Cytoscape shape, and
``mcp`` to serve the query tools over stdio. All honor the ``DUCKDB_CODE_*``
environment variables and the ``--db`` override. Every verb opens the store
read-write, since incremental updates are the rule for this package.
"""

from pathlib import Path

import typer

from duckdb_code import __version__
from duckdb_code.indexer import IndexReport, index_folder
from duckdb_code.store import CodeStore

app = typer.Typer(
    help="Embeddable code-graph impact analysis over a local DuckDB database.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    """Print the package version and exit, when ``--version`` is passed.

    Eager so it fires before ``no_args_is_help`` handling: ``duckdb-code
    --version`` prints the version and exits 0 without requiring a subcommand.
    """
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the package version and exit.",
        is_eager=True,
        callback=_version_callback,
    ),
) -> None:
    """duckdb-code command-line interface."""


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
    no_default_ignores: bool = typer.Option(
        False,
        "--no-default-ignores",
        help=(
            "Disable the built-in dependency/build-dir and size exclusions; "
            ".gitignore / .duckdbgraphignore are still honored."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Index a folder of source code into the database.

    Incremental-by-default: a full rebuild runs when the database is empty or
    ``--rebuild`` is passed; otherwise only changed, added, and deleted files
    are reconciled against the stored etags. By default the indexer skips
    ``.git``, dependency/build directories, files over 1 MB, and anything
    matched by the folder's ``.gitignore`` / ``.duckdbgraphignore``; pass
    ``--no-default-ignores`` to disable the built-in dir/size exclusions while
    still honoring the ignore files.
    """
    store = CodeStore(db_path=db)
    try:
        report = index_folder(
            store,
            folder,
            recursive=recursive,
            rebuild=rebuild,
            use_default_ignores=not no_default_ignores,
        )
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
def watch(
    folder: str = typer.Argument(..., help="Folder of source code to watch."),
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    recursive: bool = typer.Option(
        True, "--recursive/--no-recursive", help="Recurse into sub-folders."
    ),
    no_default_ignores: bool = typer.Option(
        False,
        "--no-default-ignores",
        help=(
            "Disable the built-in dependency/build-dir and size exclusions; "
            ".gitignore / .duckdbgraphignore are still honored."
        ),
    ),
) -> None:
    """Watch a folder and re-index incrementally when files change.

    Runs until interrupted (Ctrl-C). On each batch of filesystem changes it
    runs the incremental re-index, so the database — and any MCP server serving
    it alongside — reflects the current source without a manual ``index`` run.
    Changes under the default-ignored directories (``.git``, ``node_modules``,
    ``.venv``, caches, …) never trigger a rebuild.
    """
    from duckdb_code.watcher import watch as _watch

    store = CodeStore(db_path=db)

    def _report(report: IndexReport) -> None:
        typer.echo(
            f"Re-indexed: {report.rebuilt} rebuilt, {report.deleted} deleted, "
            f"{report.nodes} nodes, {report.edges} edges."
        )

    # Build the index once up front so watching starts from a current graph.
    initial = index_folder(
        store,
        folder,
        recursive=recursive,
        rebuild=False,
        use_default_ignores=not no_default_ignores,
    )
    typer.echo(
        f"Watching {folder} (initial: {initial.nodes} nodes, "
        f"{initial.edges} edges). Press Ctrl-C to stop."
    )
    try:
        _watch(
            store,
            folder,
            recursive=recursive,
            use_default_ignores=not no_default_ignores,
            on_reindex=_report,
        )
    except KeyboardInterrupt:
        typer.echo("Stopped.")
    finally:
        store.close()


@app.command()
def report(
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Report on database content."""
    store = CodeStore(db_path=db)
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
        if result.branch is not None:
            head = f" @ {result.head_commit[:10]}" if result.head_commit else ""
            typer.echo(f"Branch        : {result.branch}{head}")
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
    store = CodeStore(db_path=db)
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
def search(
    query: str = typer.Argument(..., help="What to look for, in words."),
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    limit: int = typer.Option(10, "--limit", help="Maximum ranked results."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Find symbols by intent (ranked semantic search)."""
    store = CodeStore(db_path=db)
    try:
        result = store.search(query, limit=limit)
    finally:
        store.close()
    if as_json:
        typer.echo(result.model_dump_json())
    else:
        typer.echo(f"Query   : {result.query}")
        typer.echo(f"Results : {len(result.results)}")
        for hit in result.results:
            typer.echo(
                f"  {hit.score:.3f}  {hit.node.kind}  "
                f"{hit.node.qualified_name}  {hit.node.path}"
            )


@app.command()
def find_similar(
    target: str = typer.Argument(..., help="A symbol, file path, or code snippet."),
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    limit: int = typer.Option(10, "--limit", help="Maximum ranked matches."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Find code built like a symbol or snippet (house patterns, duplication)."""
    store = CodeStore(db_path=db)
    try:
        result = store.find_similar(target, limit=limit)
    finally:
        store.close()
    if as_json:
        typer.echo(result.model_dump_json())
    else:
        typer.echo(f"Query   : {result.query}")
        typer.echo(f"Matches : {len(result.results)}")
        for hit in result.results:
            typer.echo(
                f"  {hit.score:.3f}  {hit.node.kind}  "
                f"{hit.node.qualified_name}  {hit.node.path}"
            )


@app.command()
def overview(
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    budget: int = typer.Option(2000, "--budget", help="Rough token budget."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Map the repo: modules, entry points, and most central symbols."""
    store = CodeStore(db_path=db)
    try:
        result = store.overview(budget=budget)
    finally:
        store.close()
    if as_json:
        typer.echo(result.model_dump_json())
    else:
        typer.echo(f"Modules       : {len(result.modules)}")
        for module in result.modules:
            typer.echo(f"  {module.symbol_count:4d}  {module.node.qualified_name}")
        typer.echo(f"Entry points  : {len(result.entry_points)}")
        for node in result.entry_points:
            typer.echo(f"  {node.kind}  {node.qualified_name}  {node.path}")
        typer.echo(f"Central       : {len(result.central_symbols)}")
        for sym in result.central_symbols:
            typer.echo(f"  {sym.rank:.4f}  {sym.node.kind}  {sym.node.qualified_name}")


@app.command()
def explain(
    target: str = typer.Argument(..., help="Symbol or file path."),
    db: str | None = typer.Option(None, "--db", help="DuckDB file path."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Explain the structural neighbourhood of a target."""
    store = CodeStore(db_path=db)
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
    store = CodeStore(db_path=db)
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
    index_root: str | None = typer.Option(
        None,
        "--index",
        help="Source folder to index on startup (sets root); "
        "the graph is refreshed incrementally before serving.",
    ),
    no_startup_index: bool = typer.Option(
        False,
        "--no-startup-index",
        help="Skip the startup index even when a folder is configured.",
    ),
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
    """Serve the code-graph tools as an MCP server.

    Defaults to stdio, the same-container subprocess transport. Pass
    ``--transport http`` to serve Streamable HTTP, which binds loopback
    ``127.0.0.1:8000`` (path ``/mcp``) by default.

    Pass ``--index <folder>`` (or set ``DUCKDB_CODE_ROOT``) to index that
    folder on startup before serving: an incremental build runs — a full build
    when the database is empty, otherwise only changed files are reconciled —
    so the served graph reflects the current source. Progress goes to STDERR,
    since stdout carries the MCP stdio protocol; a failed build aborts startup
    (the server never serves a partial graph). Pass ``--no-startup-index`` to
    skip the build and serve the database as a frozen, prebuilt index. A bare
    ``--db`` with no folder configured also serves the prebuilt index as-is.
    """
    from duckdb_code.config import Settings
    from duckdb_code.indexer import index_folder
    from duckdb_code.mcp_server import run_http, run_stdio

    settings = Settings(db_path=db, root=index_root)
    if settings.root and not no_startup_index:
        store = CodeStore(settings=settings)
        try:
            report = index_folder(store, settings.root, rebuild=False)
        except Exception as exc:  # noqa: BLE001 — surface then fail fast
            typer.echo(f"startup index of {settings.root} failed: {exc}", err=True)
            store.close()
            raise
        store.close()
        typer.echo(
            f"indexed {settings.root}: {report.nodes} nodes, {report.edges} edges",
            err=True,
        )
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
