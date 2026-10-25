# duckdb-graph user guide

`duckdb-graph` turns a folder of source code or Markdown into a single DuckDB
graph and answers structural questions about it — what a change affects, how a
symbol is connected, and what the index contains — with a local function call.
This guide is the package overview: what the graph is, how the parsers work, the
query surface, the CLI, the MCP server, and the export-to-viewer flow.

- [What the graph is](#what-the-graph-is)
- [The parsers](#the-parsers)
- [The query surface](#the-query-surface)
- [The CLI](#the-cli)
- [The MCP server](#the-mcp-server)
- [Export then view](#export-then-view)
- [Semantic search is the sibling](#semantic-search-is-the-sibling)

## What the graph is {#what-the-graph-is}

The graph is a set of typed nodes and typed edges stored in two DuckDB tables,
`nodes` and `edges`. A node is a `file`, `module`, `class`, `function`,
`import` (for code), or a `page`, `heading`, `tag` (for Markdown). An edge
records a relationship — `defines`, `calls`, `inherits`, `references`,
`imports` for code, and `links_to`, `embeds`, `tagged` for Markdown. Every node
carries a `qualified_name` that encodes its full scope, so `impact` and
`explain` can target a precise symbol rather than a bare name.

One DuckDB file is one graph. Code and Markdown stay in separate domains inside
it: a Markdown link never binds to a code symbol and a code edge never binds to
a page, so the two corpora share one database without crosstalk.

## The parsers {#the-parsers}

Two parser families feed the same graph:

- __Code (tree-sitter)__ — the Python and JavaScript/TypeScript extractors parse
  source with tree-sitter and emit `file` / `module` / `class` / `function` /
  `import` nodes plus the `defines` / `calls` / `inherits` / `references` /
  `imports` edges between them. Cross-file resolution is a global pass, so a
  call or import binds to the symbol it names across modules. The extractor
  registry under `src/duckdb_graph/extractors/` is the extension point for new
  languages.

- __Markdown (page/heading/tag)__ — every `.md` / `.markdown` file becomes a
  `page` node with its `heading` and `tag` nodes. A relative `[text](other.md)`
  link or a `[[Wikilink]]` becomes a `links_to` edge, and an image embed becomes
  an `embeds` edge, so a knowledge base of interlinked notes becomes a graph
  alongside code.

Indexing is incremental by default. A full build runs on an empty database or
with `--rebuild`; otherwise only changed, added, and deleted files are
reconciled by comparing each file's stored `etag` against a freshly computed
one. The global cross-file resolution re-runs so edges that bound to a changed
file are re-resolved. The incremental result is identical to a full rebuild of
the same on-disk state.

## The query surface {#the-query-surface}

Four read operations answer the structural questions:

- __`index(folder, rebuild=False)`__ — build or incrementally update the graph
  from a folder. The only writable operation.
- __`impact(target, depth=None)`__ — the transitive reverse-dependency set of a
  symbol or file: everything affected if the target changes. Each affected node
  carries its reverse-hop `distance` and the `resolution` that bound it, and
  every best-effort edge is flagged rather than silently dropped.
- __`explain(target)`__ — the structural neighbourhood of a target: what it
  `defines`, its fan-in (what depends on it) and fan-out (what it depends on),
  and — for a class — its bases and subclasses.
- __`report()`__ — introspects the index: database path, schema version,
  languages, roots, and node/edge counts by kind.

The same four verbs answer Markdown knowledge-graph questions: `impact` on a
page is "what links here", `explain` on a page is its inbound/outbound links and
heading structure, and `report` surfaces the page/heading/tag counts. See the
[examples](examples.md) for worked runs with observed results.

## The CLI {#the-cli}

The `duckdb-graph` CLI exposes seven verbs: `index`, `watch`, `report`,
`impact`, `explain`, `export`, and `mcp`. Every serving verb opens the store
read-only; `index` and `watch` open it writable. All honor the
`DUCKDB_GRAPH_*` environment variables and the `--db` override, and `--json`
emits machine-readable output.

```shell
$ duckdb-graph index ./my-project --db graph.duckdb   # incremental by default
$ duckdb-graph index ./my-project --db graph.duckdb --rebuild
$ duckdb-graph index ./my-project --db graph.duckdb --no-default-ignores
$ duckdb-graph watch ./my-project --db graph.duckdb   # re-index on file change
$ duckdb-graph report --db graph.duckdb
$ duckdb-graph impact index_folder --db graph.duckdb
$ duckdb-graph explain GraphStore --db graph.duckdb
```

`watch` runs until interrupted, re-indexing incrementally on every relevant
file change (it respects the same ignore rules as `index`, so dependency-tree
churn never triggers a rebuild). DuckDB is single-writer: run exactly one
writer — one `index` or one `watch` — against a database file at a time; an
`mcp` server reads the same file read-only alongside it.

By default `index` never descends into dependency or build trees: it skips the
`.git` directory and the dependency/build/cache directories `node_modules`,
`.venv`, `venv`, `vendor`, `dist`, `build`, `__pycache__`, `.mypy_cache`,
`.pytest_cache`, and `.ruff_cache`; files larger than 1 MB (1,000,000 bytes —
exactly that size is kept); and anything matched by the folder's `.gitignore`
or an optional `.duckdbgraphignore` (gitignore syntax, relative to the folder;
a missing file is a no-op). Pass `--no-default-ignores` to disable the built-in
directory and size exclusions while still honoring the ignore files.

## The MCP server {#the-mcp-server}

`duckdb-graph mcp` serves three read-only tools to any MCP client — `impact`,
`explain`, and `report` — with the same JSON-object shapes as the SDK. It serves
over two transports:

```shell
$ duckdb-graph mcp                                   # stdio (default)
$ duckdb-graph mcp --transport http                  # http on 127.0.0.1:8000/mcp
$ duckdb-graph mcp --transport http --host 127.0.0.1 --port 8000
```

Over __stdio__, an MCP client spawns `duckdb-graph mcp` as a child process in
the same container and talks to it over stdin/stdout; stdio is a same-container
transport, not a network protocol. Over __Streamable HTTP__ (`--transport
http`), the server binds loopback `127.0.0.1:8000` at path `/mcp` by default;
pass `--host` and `--port` to override. Serving stays strictly read-only over
both transports.

## Export then view {#export-then-view}

`export` writes the whole graph in the Cytoscape JSON shape the bundled viewer
consumes. Index a folder, export the graph, then open the viewer:

```shell
$ duckdb-graph index tests/fixtures/markdown --db graph.duckdb
$ duckdb-graph export --db graph.duckdb --out examples/graph-viewer/graph.json
```

The viewer under `examples/graph-viewer/` turns that export into an interactive
web widget — navigate the graph, zoom into one region, and expand a node's
neighbourhood on demand. The full flow, the JSON contract, and the interactions
are in the [graph viewer guide](graph-viewer-guide.md).

## Semantic search is the sibling {#semantic-search-is-the-sibling}

`duckdb-graph` answers structural, link-based questions only. For "what text is
semantically near this query" — hybrid vector + exact-token search over a local
DuckDB file — use the sibling
[`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors) package. The two
compose: one indexes meaning, the other indexes structure.
