# Examples

These examples show `duckdb-graph` answering real questions about a real
codebase — the package's own `src/duckdb_graph/` source tree. Every example
is one of the requests run by `make self-test` (see `scripts/self_test.py`), and
the observed results below are captured verbatim from that run, so this page
doubles as living evidence of what the graph can answer.

Each example first builds the graph once, then queries it:

```shell
$ duckdb-graph index src/duckdb_graph --db graph.duckdb
```

The self-test indexes into a throwaway database and composes the public
`index` / `impact` / `explain` / `report` API with direct reads of the `nodes`
and `edges` tables. Observed numbers reflect the source at capture time and
shift as the code evolves; the shapes and relationships are the stable part.

- [Index overview](#index-overview)
- [List files](#list-files)
- [List functions](#list-functions)
- [Impact of a change](#impact-of-a-change)
- [Cross-module symbol context](#cross-module-symbol-context)
- [Most-depended-upon symbols](#most-depended-upon-symbols)

## Index overview {#index-overview}

__What we are looking for.__ A one-glance profile of the indexed codebase: which
languages were detected, how many files per language, and how many graph nodes
and edges exist broken down by kind. This answers "what is in this index, and
does it look complete?" before trusting any deeper query.

__How the tool is used.__ The `report` tool introspects the index.

```shell
$ duckdb-graph report --db graph.duckdb
```

In Python this is `GraphStore(db_path=...).report()`, returning a `Report`
with `languages`, `files_by_language`, `nodes_by_kind`, `edges_by_kind`, and
`roots`.

__Observed results.__

```text
languages : python
files     : {'python': 13}
nodes     : {'class': 16, 'file': 13, 'function': 97, 'import': 81, 'module': 13}
edges     : {'calls': 339, 'defines': 126, 'inherits': 40, 'references': 54}
```

The package is pure Python here: 13 files yield 13 `file` and 13 `module` nodes,
16 classes, 97 functions, and 81 imports — 220 nodes in all. The 559 edges are
dominated by 339 `calls`, with `defines`, `inherits`, and `references` filling
in the structure. Counts like these are the sanity check that indexing ran to
completion.

## List files {#list-files}

__What we are looking for.__ The inventory of source files the indexer actually
ingested — the ground truth of "what got parsed", useful for confirming no
module was skipped.

__How the tool is used.__ Files are the `file`-kind nodes in the graph. The
self-test reads them directly:

```text
SELECT DISTINCT path FROM nodes WHERE kind = 'file' ORDER BY path
```

__Observed results.__

```text
__init__.py
cli.py
config.py
etag.py
extractors/__init__.py
extractors/base.py
extractors/javascript.py
extractors/python.py
extractors/registry.py
indexer.py
mcp_server.py
models.py
store.py
```

All 13 modules are present, including the nested `extractors/` package — the
walk descends into sub-packages, and every expected module (`store.py`,
`indexer.py`, `cli.py`, `mcp_server.py`) is accounted for.

## List functions {#list-functions}

__What we are looking for.__ The inventory of functions and methods the extractor
recognized, by qualified name — evidence that definitions are captured at the
right granularity (module, class, method, nested function) and not silently
dropped.

__How the tool is used.__ Functions are the `function`-kind nodes:

```text
SELECT qualified_name FROM nodes WHERE kind = 'function' ORDER BY qualified_name
```

__Observed results.__ The first entries (97 functions in total):

```text
config.Settings.__init__
config._env
etag.compute_etag
extractors.javascript.JsTsExtractor.__init__
extractors.javascript.JsTsExtractor.extract
extractors.javascript._JsTsBuilder.__init__
extractors.javascript._JsTsBuilder._add_edge
extractors.javascript._JsTsBuilder._add_node
...
```

Qualified names carry the full scope — `config.Settings.__init__` is the method
on the `Settings` class in `config.py`, distinct from a module-level function of
the same name. This is what lets `impact` and `explain` target a precise symbol
rather than a bare name.

## Impact of a change {#impact-of-a-change}

__What we are looking for.__ The headline question: "if I change this symbol,
what else is affected?" — the transitive set of files and symbols that depend on
a target, so a change can be scoped before it is made.

__How the tool is used.__ The `impact` tool returns the reverse-dependency set.

```shell
$ duckdb-graph impact index_folder --db graph.duckdb
```

In Python, `store.impact("index_folder")` returns an `ImpactResult` whose
`affected_files` lists the distinct impacted paths and whose `affected` lists
each impacted symbol with its reverse-hop `distance`.

__Observed results.__

```text
affected files (2):
  cli.py
  store.py
affected symbols (3):
  d=1 module cli
  d=1 module store
  d=1 function store.GraphStore.index
```

Changing `index_folder` reaches two files through three direct (distance 1)
dependents: the `store` and `cli` modules, and `GraphStore.index` — which
is exactly the method that delegates to `index_folder`. A reviewer changing that
function knows up front which files to re-check.

## Cross-module symbol context {#cross-module-symbol-context}

__What we are looking for.__ The full in-code neighbourhood of one symbol: what
it defines, what depends on it (fan-in) and what it depends on (fan-out), and —
for a class — its bases and subclasses. This is "explain this symbol to me in
the context of the whole codebase", spanning modules.

__How the tool is used.__ The `explain` tool returns that neighbourhood.

```shell
$ duckdb-graph explain GraphStore --db graph.duckdb
```

In Python, `store.explain("GraphStore")` returns an `ExplanationResult`; the
self-test prefers the `class`-kind resolution so the context is the class
definition rather than a same-named re-export.

__Observed results.__

```text
node    : store.GraphStore (store.py)
defines : 23
fan_in  : 2  fan_out: 0
  used by : cli (cli.py)
  used by : mcp_server.build_server (mcp_server.py)
```

`GraphStore` is defined in `store.py`, exposes 23 members, and is used from
two other modules — the CLI and the MCP server's `build_server`. The fan-in
crossing module boundaries is the cross-module context: it shows the class's
real consumers without reading every file by hand.

## Most-depended-upon symbols {#most-depended-upon-symbols}

__What we are looking for.__ The hotspots — the symbols the most other code
depends on, ranked by reverse-dependency fan-in. These are the riskiest symbols
to change and the ones worth the most test coverage.

__How the tool is used.__ The ranking counts inbound edges of the
reverse-dependency kinds (`imports`, `calls`, `inherits`, `references` — the
same set `impact` traverses; `defines` is excluded because it is container
structure, not a dependency), grouped by the depended-upon node:

```text
SELECT n.qualified_name, n.kind, COUNT(*) AS fan_in
FROM edges e
JOIN nodes n ON n.node_id = e.dst_id
WHERE e.kind IN ('imports', 'calls', 'inherits', 'references')
GROUP BY n.qualified_name, n.kind
ORDER BY fan_in DESC
LIMIT 5
```

__Observed results.__

```text
 10  import    config::import:pydantic.BaseModel
 10  import    extractors.base::import:pydantic.BaseModel
 10  import    indexer::import:pydantic.BaseModel
 10  import    models::import:pydantic.BaseModel
  7  import    ::import:duckdb_graph.models.Node
```

The most-depended-upon targets are `pydantic.BaseModel` (pulled into every
module that declares models) and the internal `models.Node` — unsurprising for a
package built on pydantic models over a shared node type. A ranking like this
points a maintainer straight at the symbols whose change would ripple widest.
