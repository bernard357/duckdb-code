# duckdb-code

__A frugal, embeddable code-intelligence building block for agentic systems — MCP tools that help an IDE or CLI explore and change a codebase over a local DuckDB file, no external service. Impact analysis and structure explanation over the dependency graph is the first capability.__

`duckdb-code` is a python package that exposes the actual structure of a codebase to a local agent, in 3 steps:

1. analyze a folder of source files with tree-sitter and extract nodes and edges
2. store that graph in a single DuckDB database baked next to your agent
3. answer `impact`, `explain`, and `report` questions with a local function call

## When your agent needs structural code questions

Most agents we build at work reason over code they cannot see the shape of. They read files one at a time and guess at the blast radius of a change. The honest requirement is small and structural: given a symbol or a file, what depends on it, what does it depend on, and what is the whole thing shaped like. The reflex answer is large — stand up a code-intelligence service, index into a hosted graph database, wire the agent to it over the network. For one repository, read-mostly, that is a lot of moving parts to own.

For the common case — one repository, packaged with one agent — you can skip the external service entirely. Index the code into a single [DuckDB](https://duckdb.org) file, bake that file into the agent's container image, and answer `impact` / `explain` / `report` with a local function call. No network round-trip. Where [`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors) answers "what text is semantically near this query", `duckdb-code` answers "what code is affected if this symbol or file changes" and "what is the structure of this code".

## Data architecture decisions and facts

A few deliberate choices shape how `duckdb-code` indexes and serves:

- __One DuckDB file is one graph__ — no external graph service and no network hop on the query path.
- __Code and Markdown stay in separate domains__ in the same database: a Markdown link never binds to a code symbol and a code edge never binds to a page, so the two corpora share one file without crosstalk.
- __Incremental, etag-driven re-index__: `index` is incremental by default. A full rebuild runs on an empty database or with `rebuild=True`; otherwise only changed, added, and deleted files are reconciled by comparing each file's stored `etag` against a freshly computed one. The incremental result is identical to a full rebuild of the same on-disk state.
- __Read-write everywhere__: the store always opens read-write, since incremental updates are the rule — the `mcp` server can index its target folder on startup (`--index`) and refresh it in place, rather than serving a frozen file.

## Keeping the graph fresh in an agentic IDE

Because re-indexing is incremental and cheap, the graph can track the working tree as an agent edits it, rather than going stale the moment code changes. Three mechanisms keep it current:

- `duckdb-code index` reconciles only what changed since the last run (etag-driven), so a re-index after an edit is a near-instant no-op when nothing moved.
- `duckdb-code watch <folder>` runs as a long-lived process and re-indexes incrementally on every relevant file change, so the graph stays live with no manual step — the natural fit for an IDE session where the agent is actively editing.
- `duckdb-code mcp --index <folder>` folds a startup incremental build into the MCP server, so a freshly launched agent serves a graph that already reflects the current source.

The upshot: an agent asking `impact` mid-session sees the blast radius of the code as it is now, not as it was at the last manual index.

## Use cases

`duckdb-code` has been designed for the following use cases:

- __Change-impact analysis__ — before touching a symbol, ask `impact` for its transitive reverse-dependency set so the blast radius is scoped before the change is made. Every best-effort edge is flagged rather than silently dropped.
- __Structure explanation__ — ask `explain` for a symbol's defines, fan-in, fan-out, bases, and subclasses, so an agent can describe a symbol in the context of the whole codebase.
- __Markdown knowledge graph__ — index a folder of interlinked Markdown notes and the same `impact` / `explain` / `report` answer "what links here", "a page's structure and links", and "which pages are orphans".
- __Graph viewer__ — `export` the graph as Cytoscape JSON and open the bundled web viewer to navigate it, zoom into one region, and expand a node's neighbourhood. See the [graph viewer guide](graph-viewer-guide.md).

## Quick start

Get `duckdb-code` from the PyPI repository:

```shell
$ pip install duckdb-code             # install once
```

Index a folder of source code from the command line. This produces a single DuckDB file:

```shell
$ duckdb-code index ./my-project      # incremental by default
$ duckdb-code report                  # report on database content
$ duckdb-code impact payments.charge.Charge   # reverse-dependency set
```

Then query the graph from within your python code:

```python
from duckdb_code import CodeStore

store = CodeStore(db_path="code.duckdb")

result = store.impact("payments.charge.Charge")
for hit in result.affected:
    print(hit.distance, hit.resolution, hit.node.qualified_name)

explanation = store.explain("payments.charge.Charge")
report = store.report()
```

`impact` returns the affected set with per-node distance and resolution; `explain` returns a node's defines/fan-in/fan-out/bases/subclasses; `report` introspects the index.

## Documentation

Pick the entry point that matches where you are:

- New to `duckdb-code`? Read the [user guide](duckdb-code-guide.md) for the package overview, the parsers, and the CLI/MCP surfaces.
- Want worked questions against real inputs? See the [examples](examples.md), captured verbatim from `make self-test`.
- Visualizing a graph? See the [graph viewer guide](graph-viewer-guide.md).
- Contributing or developing? See the [developer workbook](developer-workbook.md) for prerequisites, setup, and the full workflow.
- Spreading the package? See [foster adoption](register-package-procedure.md).

## Configuration

Settings resolve with the precedence: explicit argument > `DUCKDB_CODE_*` environment variable > default.

| Setting | Argument | Environment variable | Default |
| --- | --- | --- | --- |
| Database path | `db_path` | `DUCKDB_CODE_DB_PATH` | `code.duckdb` |
| Indexed source root | `root` | `DUCKDB_CODE_ROOT` | `None` |

## Contributing

Contributions are welcome — adding an extractor for a new language is a great first pull request. See [CONTRIBUTING.md](https://github.com/bernard357/duckdb-code/blob/main/CONTRIBUTING.md) for the development setup, project principles, and pull-request guidelines.

## Semantic search lives in the sibling

`duckdb-code` answers structural, link-based questions only. For "what text is semantically near this query" — hybrid vector + exact-token search over a local DuckDB file — use the sibling [`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors) package. The two compose: one indexes meaning, the other indexes structure.

## License

MIT.
