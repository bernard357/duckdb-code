# Changelog

All notable changes to `duckdb-graph` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project version is
tag-driven: the released version comes from the git `vYYMMDD` tag via hatch-vcs.

## Unreleased

No `vYYMMDD` tag has been cut yet, so everything shipped so far sits under
Unreleased pending the first release.

### Added

- Foundation modules and the pluggable extractor registry.
- Python and JavaScript/TypeScript extractors (tree-sitter) and the folder
  indexer that builds the `nodes` and `edges` graph.
- The `GraphStore` API with `impact` (transitive reverse-dependency set),
  `explain` (structural neighbourhood), and `report` (index introspection).
- The `duckdb-graph` CLI and an MCP server over stdio.
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
  packaging and discovery kit aligning `duckdb-graph` with the sibling
  `duckdb-vectors`.
- `duckdb-graph mcp --index <folder>` indexes the folder on startup before
  serving — an incremental build (full on an empty database, otherwise only
  changed files reconciled) so the served graph reflects the current source
  without a separate `index` run. Progress goes to stderr and a failed build
  aborts startup. `--no-startup-index` skips the build to serve a prebuilt
  database as a frozen snapshot.

### Removed

- The read-only store mode: the `read_only` setting, the `DUCKDB_GRAPH_READ_ONLY`
  environment variable, the `GraphStore(read_only=…)` argument, and the
  `ReadOnlyError` write guard. The store now always opens read-write, since
  incremental updates are the rule for this package.
