# Changelog

All notable changes to `duckdb-code` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project version is
tag-driven: the released version comes from the git `vYYMMDD` tag via hatch-vcs.

## Unreleased

### Added

- __Cross-boundary HTTP route edges (limited frameworks).__ The indexer now
  extracts server routes and client calls across languages and links them: a
  `route` node per server route (Python Flask/FastAPI decorators `@app.get` /
  `@router.post` / `@app.route`, and Express `app.<verb>` / `router.<verb>`),
  and a `calls_route` edge from each client call (`fetch("/x")`,
  `axios.<verb>("/x")`) resolved by exact `METHOD /path` match to the matching
  route node. This is the HTTP route↔client link an LSP never sees (a
  TypeScript `fetch` to a Python handler). Scoped to those frameworks and exact
  path literals; unmatched calls are dropped. Also fixes a latent gap:
  decorated Python functions are now extracted.
- __`history` tool and CLI verb.__ Git signals for a symbol or file: the recent
  commits touching its file, the files that usually change with it (co-change
  partners), and its churn — coupling and volatility no static graph shows.
  Reads the `commits` table (schema v5, recent commits per file) plus the
  `file_changes_with` edges and `files.churn`. Exposed as
  `CodeStore.history(target)`, the `history` MCP tool, and
  `duckdb-code history <target>`.
- __Git co-change edges and per-file churn (schema v4).__ The enrichment phase
  now mines recent git history (`git log` over the last 500 commits) for files
  that change together, adding `file_changes_with` edges between file nodes
  (confidence = normalized co-occurrence) and recording per-file `churn` on the
  `files` table. This is the one place the indexer shells out to git; it
  degrades gracefully — no git, no repo, or a git error adds no edges and
  leaves churn zero, so the index still builds. Backs the forthcoming `history`
  and `context_for_change` tools.
- __`related_tests` tool and CLI verb.__ Returns the tests to run after editing
  a symbol or file — the test symbols that reach it via the `tests` edges,
  resolved the same way as `impact`/`explain`. Exposed as
  `CodeStore.related_tests(target)`, the `related_tests` MCP tool, and
  `duckdb-code related-tests <target>`.
- __`overview` tool and CLI verb.__ The repo map an agent reads to orient
  before touching anything: the indexed modules (most symbol-rich first), the
  entry points (symbols nothing depends on — dependency-graph roots), and the
  most central symbols by index-time centrality, each section sized to a token
  `budget`. Exposed as `CodeStore.overview(budget)`, the `overview` MCP tool,
  and `duckdb-code overview`.
- __`find_similar` tool and CLI verb.__ Finds code built like a given symbol or
  snippet — "how do we usually do this here?" and duplication detection. A
  resolved symbol uses its stored embedding (and is excluded from its own
  results); an unresolved string is embedded as a snippet. Exposed as
  `CodeStore.find_similar(target, limit)`, the `find_similar` MCP tool, and
  `duckdb-code find-similar <target>`.
- __`search` tool and CLI verb.__ Ranked semantic search over the indexed
  symbols — "where do we retry failed payments?" without a known name — the
  first tool of the MCP contract. Exposed as `CodeStore.search(query, limit)`,
  the `search` MCP tool, and `duckdb-code search <query>`; ranks the `vectors`
  table by cosine similarity in DuckDB and returns symbols with `path`/`range`.
- __Semantic embeddings and the `vectors` table (schema v3).__ A full index now
  embeds each searchable symbol (modules, classes, functions, pages, headings)
  and stores one vector per node in a `vectors` table keyed by `node_id`,
  backing the forthcoming `search` and `find_similar` tools. Embedding backends
  are pluggable (`duckdb_code.embedders`): the production default is `fastembed`
  (local ONNX, now a core dependency), with a dependency-free `hashing` backend
  for tests and offline use, selected via `DUCKDB_CODE_EMBEDDER` or the
  `embedder` setting. The backend id and dimensions are recorded in `meta`.
- __Index-time graph analytics (schema v2).__ A full index now computes and
  stores a PageRank-style `centrality` per node and weakly-connected-component
  `member_of` community edges in a post-index phase, over the dependency edge
  kinds. These back the forthcoming `overview` and `analytics` tools.
- __`tests` edges.__ The enrichment phase now derives `tests` edges from the
  resolved graph: a symbol in a test file (by `test_*`/`*_test`/`*.test.*`/
  `*.spec.*` naming or a `tests`/`__tests__`/`spec` directory) links to each
  non-test symbol it calls, references, or imports. Backs the forthcoming
  `related_tests` tool and the `impact` extension.

### Changed

- __`impact` returns the one-call blast radius.__ In addition to the affected
  set, the `impact` tool/CLI/SDK result now carries `affected_by_module` (the
  blast radius per module), `affected_entry_points` (the affected
  dependency-graph roots), and `related_tests` (the tests that exercise any
  affected or seed symbol, via `tests` edges) — so a change's scope and the
  tests to run come back in one call. Additive: the existing fields are
  unchanged.
- __Numeric per-edge `confidence` (schema v2).__ Call resolution now assigns a
  numeric confidence (0.0–1.0) per strategy — import map 0.95, same-module
  0.90, suffix 0.55, exact/`defines` 1.0 — stored on `edges.confidence` as the
  source of truth; the existing `exact`/`best_effort` `resolution` is derived
  from it by a documented 0.9 threshold. Schema bumped to version 2
  (`edges.confidence`, `nodes.centrality`); a store opened against a v1 file
  recreates the schema on the next full index.
- __Deferred-index bulk load.__ The full/incremental write path now recreates
  the tables without their secondary indexes, bulk-inserts, then creates the
  indexes — faster fresh indexing, and a clean post-index phase seam.
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
