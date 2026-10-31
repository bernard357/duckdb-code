
# Indexing strategy workbook

How `duckdb-code` turns a folder of source into a DuckDB file: the parse/build
pipeline and the storage schema, and the decisions that shape them. This page
is the data-layer counterpart to the MCP tool contract — every tool the
contract exposes must read from a data source defined here, and every data
source here should serve a tool there. It records which lessons from the
Codebase-Memory paper (arXiv:2603.27277, a close tree-sitter -> embedded-DB ->
MCP analogue) we adopt, adapt, or reject against what the package already does.

- [Scope](#scope)
- [The build pipeline](#build-pipeline)
- [Call resolution and the confidence score](#call-resolution)
- [The DuckDB schema](#schema)
- [Semantic search: vector storage](#vectors)
- [Incremental re-index](#incremental)
- [Post-index analytics](#analytics)
- [Known limits](#limits)
- [Lessons adopted, adapted, rejected](#lessons)

## Scope {#scope}

This page covers two concerns only: __parsing__ a codebase and __storing__ the
result in the DuckDB file. The MCP tool surface that reads this data — its
tools, parameters, and the LSP division of labour — is the companion contract,
and the two are co-designed: a schema element here exists to serve a named tool
there, and a tool there must have a data source here. The paper's security /
release-verification and benchmark material is deliberately out of scope.

The guiding principle is unchanged from the rest of the package: __one DuckDB
file is one index__, built read-write, incremental by default, storing
_relationships_ (edges between symbols) rather than source lines — results
carry `path + range` so a consumer can fetch the exact text from the live
buffer (or an LSP) itself.

## The build pipeline {#build-pipeline}

Indexing runs as an ordered set of phases. Phases that only compute (parse,
extract, resolve, enrich) build an in-memory set of nodes and edges; the
database is touched in one bulk write, and the secondary indexes are created
__after__ the bulk load, not before. This "stage in memory, bulk-insert, then
index" shape is what keeps a fresh build fast, and it gives each enrichment a
clean seam to hang off.

| Phase | Output |
| --- | --- |
| 1. Structure | File discovery under the root, honoring ignore rules; `file` / `module` nodes and their containment edges. |
| 2. Extraction | Per-file tree-sitter extraction; `class` / `function` / `import` nodes (and `page` / `heading` / `tag` for Markdown) with name, qualified name, path, line range, language. |
| 3. Resolution | Cross-file binding of `imports` / `calls` / `inherits` / `references` (and Markdown `links_to` / `embeds` / `tagged`) to concrete node ids, each carrying a confidence (see below). |
| 4. Enrichment | Edges that are not pure AST facts: test mappings, config/route cross-boundary links, and git co-change — staged as each backing tool lands (see the schema). |
| 5. Flush | One bulk `INSERT` of nodes and edges, then create the secondary indexes. |
| 6. Post-index | Whole-graph precompute over the stored graph: centrality and communities (see analytics). |

Phases 1–4 are pure and deterministic — `resolve_edges` is already a pure
function over `(nodes, edges)` with no database handle — which keeps them
unit-testable in isolation and makes the incremental path (re-running only the
touched files, then re-resolving) produce a graph byte-identical to a full
rebuild of the same on-disk state.

Deferred index creation is the load-bearing detail of the flush phase: building
the B-tree indexes once, after all rows are present, is far cheaper than
maintaining them across a row-by-row insert.

## Call resolution and the confidence score {#call-resolution}

Resolving a raw callee or reference token to a concrete node is the core
linking problem. The resolver runs a prioritized cascade: exact qualified-name
match first, then module-local and import-scoped matches, then a last-segment
suffix fallback; `defines` edges pass through as exact, and an edge that binds
to zero in-set nodes is dropped rather than fabricated.

Today each edge stores a two-value `resolution` (`exact` | `best_effort`). The
target is to make the resolver assign a __numeric `confidence`__ (0.0–1.0) per
strategy — high for an exact qualified-name or import-map hit, lower for a
module-local or unique-name match, lowest for a suffix or fuzzy fallback — and
to store that number as the source of truth. The agent-facing `resolution`
flag then becomes a _derived projection_ of `confidence` by a documented
threshold (for example `exact` at or above 0.9, `best_effort` below), so there
is one scoring concept, defined here and projected into the tool contract —
never two that can drift.

Type-aware resolution (tracking receiver/expression types for method and
pointer-heavy calls, as the paper does for Go / C / C++) is a deliberate
__future, language-scoped__ enhancement: the package targets Python and
JavaScript/TypeScript today, where name-based resolution covers the common
cases, so the numeric cascade lands first and type passes come only if a
supported language needs them.

## The DuckDB schema {#schema}

The current schema is four tables: `files` (path primary key, language, etag,
stamp), `nodes` (node_id, kind, name, qualified_name, path, line range,
language), `edges` (src_id, dst_id, kind, path, resolution), and `meta`
(key/value). Node and edge kinds are kept as plain strings validated at
extraction time, so a new language or edge kind needs no schema migration.

The planned delta, each column or edge kind annotated with the tool it serves:

| Change | Shape | Serves |
| --- | --- | --- |
| Add `confidence` to `edges` | numeric 0.0–1.0; `resolution` becomes its derived projection | the shared result contract (every tool) |
| `tests` edge kind | symbol/file -> its covering test | `related_tests` |
| cross-boundary / `route` edges | HTTP route<->client, config/env key<->reader, SQL table<->query, DI binding, feature flag; carries `confidence` | `links` |
| git co-change edges | files/symbols that change together, with churn | `history`, `context_for_change` |
| `member_of` + community nodes | community assignment per node | `analytics` |
| centrality column | per-node rank (PageRank or similar) | `overview`, `analytics` |
| vector storage | per-symbol embedding (see below) | `search`, `find_similar` |

Each row lands when its tool is implemented, not all at once; the table is the
contract that keeps the schema and the tools in lock-step. The core node/edge
kinds already present stay as the backbone; the additions are strictly
additive.

__Build status.__ Shipped at schema version 5: the `edges.confidence` column
(with `resolution` derived from it), the `nodes.centrality` column, `member_of`
community edges, `tests` edges, the `vectors` table, git co-change
(`file_changes_with` edges plus per-file `files.churn`) with the recent-commit
`commits` table, and cross-boundary HTTP `route` nodes with `calls_route` edges
— all produced by the deferred-index pipeline and its enrichment / post-index
phases. Git history is the one place the indexer shells out to `git log`
(recent-commit window), degrading to no data when git or a repo is absent. The
route edges cover a limited set of frameworks (Flask/FastAPI, Express, and
fetch/axios clients) matched on exact `METHOD /path` literals — the full
data-layer delta for the tool contract is now in place.

## Semantic search: vector storage {#vectors}

Two contract tools — `search` (intent / ranked-symbol lookup) and
`find_similar` — need _semantic_ similarity, which the structural graph has no
representation for. This is the sharpest gap between parse/store and the tool
contract, so the strategy names a home for it explicitly rather than leaving it
implicit.

Two options, both viable for a frugal single-file design:

- __In-DuckDB vectors.__ A `vectors` table keyed by `node_id` holding a
  per-symbol embedding, queried with a similarity expression. Keeps "one file
  is one index" intact — the embeddings live in the same `.duckdb` file as the
  graph.
- __`duckdb-vectors` companion.__ Reuse the sibling package as the embedding
  index and have `search` / `find_similar` compose the two files. Avoids
  re-implementing embedding storage, at the cost of a second artifact.

__Decision (shipped).__ The in-DuckDB `vectors` table, to preserve the
single-file promise — one embedding per searchable symbol, keyed by `node_id`,
populated in the post-index phase. The embedding backend is pluggable
(`duckdb_code.embedders`, mirroring the sibling `duckdb-vectors`): the
production default is `fastembed` (local ONNX, a core dependency, real semantic
vectors), with a dependency-free `hashing` backend reserved for tests and
offline use — it produces stable, normalized but lexical vectors, never a
production choice. The backend is selected by `DUCKDB_CODE_EMBEDDER` (or the
`embedder` setting), and its id and dimensions are stored in `meta` so a
backend change is detectable. The embedded text is the symbol's kind plus its
identifier with dotted names split into tokens (relationships, not source lines
— so search is intent-over-identifiers).

## Incremental re-index {#incremental}

Re-indexing is incremental by default: each file carries a content `etag` in
the `files` table, and a re-index compares the stored etag against a freshly
computed one, re-parsing only changed, added, and deleted files and then
re-running the (pure) cross-file resolution so edges binding to a changed file
are re-resolved. A no-change re-index is a near-instant no-op, and the result
is identical to a full rebuild of the same state.

The paper uses an XXH3 content hash for the same purpose (~30 GB/s, ~4x faster
than full re-index). Our etag already delivers that _behaviour_; XXH3 is the
reference to reach for only if profiling ever shows the fingerprint itself is a
bottleneck — not a change to make speculatively. The indexer records the
indexed commit and the per-file content hash, which is exactly the state the
`index_status` tool reports.

## Post-index analytics {#analytics}

Whole-graph measures — centrality (PageRank-style) for "most central symbols",
and Louvain-style community detection for clusters — are computed __at index
time__, in the post-index phase, over the stored graph, and persisted
(centrality as a node column, communities as `member_of` edges / community
nodes). The tools that expose them (`overview`, `analytics`) then _read_
precomputed values rather than computing them per query; deciding the compute
site here is what lets those queries stay cheap and keeps the tool layer from
assuming work the schema never produced. A documented row/traversal bound
guards the very largest graphs. Building the tools themselves is tracked in the
contract, not here; this page only reserves the phase and the storage.

## Known limits {#limits}

The index is a __static, commit-snapshot__ view and is honest about what it
cannot answer, which is what justifies handing those questions to a live LSP:

- __Dynamic behaviour is invisible.__ Reflection, dynamic dispatch, and
  runtime-constructed calls are not in the static graph; an edge that only
  exists at runtime will be missing (relevant for Python and JavaScript).
- __Macros / generated code.__ Constructs absent from the tree-sitter AST
  (C-style macros being the paper's worst case) are not represented; the
  package does not target such languages today, but the caveat holds wherever
  code is generated or expanded.
- __Snapshot lag.__ The index reflects the last indexed state, not unsaved
  buffers — the explicit reason point queries (definition, references, hover)
  stay with the LSP.

## Lessons adopted, adapted, rejected {#lessons}

Each indexing lesson from the paper and what we do with it:

| Paper lesson | Decision | Notes |
| --- | --- | --- |
| Multi-phase pipeline, in-memory buffer, bulk load | __Adopt__ | Phase separation + deferred index creation as above. |
| Deferred index creation after bulk insert | __Adopt__ | Create secondary indexes in the flush phase, after rows land. |
| Parallel extraction via a worker pool | __Adapt__ | Phase boundary exists; parallelism is an optimization added when a single-threaded extract is shown too slow. |
| 6-strategy confidence cascade | __Adopt__ | Replace the two-value `resolution` with a stored numeric `confidence`. |
| LSP-style type resolution (Go/C/C++) | __Adapt / defer__ | Future, language-scoped; our targets are Python/JS/TS. |
| Rich typed edge set (TESTS, CONFIGURES, route, co-change) | __Adopt, staged__ | Each edge kind lands with the tool it backs. |
| Cross-boundary route edges with confidence | __Adopt, staged__ | Backs `links`. |
| XXH3 content hashing for incremental | __Adapt__ | Keep the etag seam (same behaviour); XXH3 only if profiled as a bottleneck. |
| Louvain community detection, post-index | __Adopt, staged__ | Index-time precompute; the `analytics` tool reads it. |
| Centrality / PageRank ranking | __Adopt, staged__ | Index-time precompute; `overview` reads it. |
| Store relationships, not source lines | __Adopt__ | Already how the package works; results carry `path + range`. |
| Louvain / query row ceiling | __Adopt__ | Document a row/traversal bound for very large graphs. |
| Embeddings for semantic retrieval | __Adopt, new__ | Not in the paper's graph; required by `search` / `find_similar`, stored per the vectors decision. |
| Single statically linked C binary, 66 grammars | __Reject__ | Out of character for a frugal Python/DuckDB package; we stay Python + tree-sitter for our supported languages. |
| Security / release-verification pipeline | __Reject (out of scope)__ | Not an indexing concern. |
