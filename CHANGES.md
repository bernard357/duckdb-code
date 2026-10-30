# Changelog

All notable changes to `duckdb-code` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project version is
tag-driven: the released version comes from the git `vYYMMDD` tag via hatch-vcs.

## Unreleased

### Changed

- __BREAKING: renamed the project from `duckdb-graph` to `duckdb-code`__ and
  broadened its framing from a code graph to a DuckDB-backed code-intelligence
  building block whose MCP tools help an IDE or CLI explore and change a
  codebase (the dependency graph is the first capability). Every public
  identifier changed: the PyPI distribution (`duckdb-graph` → `duckdb-code`),
  the import package (`duckdb_graph` → `duckdb_code`), the CLI command
  (`duckdb-graph` → `duckdb-code`), the environment prefix (`DUCKDB_GRAPH_*` →
  `DUCKDB_CODE_*`), the default database filename (`graph.duckdb` →
  `code.duckdb`), and the public classes (`GraphStore` → `CodeStore`,
  `GraphExport`/`GraphExportNode`/`GraphExportEdge` →
  `CodeExport`/`CodeExportNode`/`CodeExportEdge`). `duckdb-code` is published as
  a new PyPI project; the earlier `duckdb-graph` `261021` release is unaffected.

### Added

- Foundation modules and the pluggable extractor registry.
- Python and JavaScript/TypeScript extractors (tree-sitter) and the folder
  indexer that builds the `nodes` and `edges` graph.
- The `CodeStore` API with `impact` (transitive reverse-dependency set),
  `explain` (structural neighbourhood), and `report` (index introspection).
- The `duckdb-code` CLI and an MCP server over stdio.
- Incremental re-indexing by default: etag-driven reconciliation of changed,
  added, and deleted files, identical to a full rebuild of the same on-disk
  state.
- Streamable HTTP transport for the MCP server (`--transport http`).
- A `self-test` capability-evidence suite that dogfoods the tool on the
  package's own source.
- A Markdown extractor emitting `page` / `heading` / `tag` nodes and the
  `links_to` / `embeds` / `tagged` knowledge-graph edges.
- Graph `export` in the Cytoscape JSON shape and a bundled Cytoscape.js web
  viewer under `examples/graph-viewer/`.
- The documentation site (VitePress), CI and release workflows, and the
  packaging and discovery kit aligning `duckdb-code` with the sibling
  `duckdb-vectors`.
- `duckdb-code mcp --index <folder>` indexes the folder on startup before
  serving — an incremental build (full on an empty database, otherwise only
  changed files reconciled) so the served graph reflects the current source
  without a separate `index` run. Progress goes to stderr and a failed build
  aborts startup. `--no-startup-index` skips the build to serve a prebuilt
  database as a frozen snapshot.

### Removed

- The read-only store mode: the `read_only` setting, the `DUCKDB_CODE_READ_ONLY`
  environment variable, the `CodeStore(read_only=…)` argument, and the
  `ReadOnlyError` write guard. The store now always opens read-write, since
  incremental updates are the rule for this package.
