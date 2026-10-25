# duckdb-graph

__A frugal, embeddable code-graph building block for agentic systems — tree-sitter impact analysis and structure explanation over a local DuckDB file, no external service.__

`duckdb-graph` is a python package that exposes the actual structure of a
codebase to a local agent, in 3 steps:

1. analyze a folder of source files with tree-sitter and extract nodes and edges
2. store that graph in a single DuckDB database baked next to your agent
3. answer `impact`, `explain`, and `report` questions with a local function call

Where [`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors) answers
"what text is semantically near this query", `duckdb-graph` answers "what
code is affected if this symbol or file changes" and "what is the structure of
this code". The must-be-reliable deliverable is `impact`: a transitive
reverse-dependency set over the edge graph, with every best-effort edge flagged
rather than silently dropped.

## SDK entry point

Open a store against a local `.duckdb` file and query it. Open read-only in the
serving path, when the index is a frozen artifact baked into the container:

```python
from duckdb_graph import GraphStore

store = GraphStore(db_path="graph.duckdb", read_only=True)

result = store.impact("payments.charge.Charge")
for hit in result.affected:
    print(hit.distance, hit.resolution, hit.node.qualified_name)

explanation = store.explain("payments.charge.Charge")
report = store.report()
```

`impact` returns the affected set with per-node distance and resolution;
`explain` returns a node's defines/fan-in/fan-out/bases/subclasses; `report`
introspects the index.

`store.index(folder)` is incremental-by-default: a full rebuild runs when the
database is empty or you pass `rebuild=True`, and otherwise only changed, added,
and deleted files are reconciled by comparing each file's stored `etag` against
a freshly computed one (the `compute_etag` content fingerprint). The global
cross-file resolution is re-run so edges that bound to a changed file are
re-resolved; a no-change re-index is a no-op. The incremental result is
identical to a full rebuild of the same on-disk state.

```python
from duckdb_graph import GraphStore

store = GraphStore(db_path="graph.duckdb")

store.index("./my-project")              # full build on an empty database
store.index("./my-project")              # incremental re-index (etag-driven)
store.index("./my-project", rebuild=True)  # force a full rebuild
```

## Indexing from the CLI

```shell
$ duckdb-graph --version                               # print the version and exit
$ duckdb-graph index ./my-project --db graph.duckdb   # incremental by default
$ duckdb-graph index ./my-project --db graph.duckdb --rebuild  # full rebuild
```

The `index` verb is incremental-by-default: it runs a full build when the
database is empty and reconciles only changed, added, and deleted files on a
re-run. Pass `--rebuild` to force a full rebuild.

## Indexing a folder of Markdown pages

The same graph indexes a folder of Markdown pages, so a knowledge base of
interlinked notes becomes a graph alongside code. Point `index` at a Markdown
folder:

```shell
$ duckdb-graph index ./my-notes --db graph.duckdb
```

Every `.md` / `.markdown` file becomes a `page` node with `heading` and `tag`
nodes, and the links between pages become edges: a relative `[text](other.md)`
link or a `[[Wikilink]]` becomes a `links_to` edge, and an image embed becomes
an `embeds` edge. The same `impact`, `explain`, and `report` queries then answer
knowledge-graph questions:

- __what links here__ — `impact` on a page returns the pages that link to it.
- __a page's structure and its links__ — `explain` on a page returns its inbound
  and outbound links together with its heading structure.
- __orphan pages__ — a page with zero inbound links (empty `impact`) is
  unreferenced by the rest of the corpus.
- __corpus counts__ — `report` surfaces the page, heading, and tag counts and
  the link-edge totals.

Code and Markdown stay in separate domains: a Markdown link never binds to a
code symbol and a code edge never binds to a page, so the two corpora can share
one database without crosstalk.

Semantic / vector search is out of scope for `duckdb-graph` — it answers
structural, link-based questions. For "what text is semantically near this
query", use the sibling
[`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors) package.

## MCP tool

`duckdb-graph mcp` is an MCP-over-stdio server exposing three read-only
tools:

- `impact(target, …)` — transitive reverse-dependency set for a symbol or file.
- `explain(target, …)` — structural neighbourhood of a symbol or file.
- `report()` — introspects the index: database path, schema version, languages,
  and node/edge counts.

An MCP client spawns `duckdb-graph mcp` as a child process in the same
container and talks to it over stdin/stdout; stdio is a same-container
transport, not a network protocol.

The server also serves over __Streamable HTTP__, selected with
`--transport http`:

```shell
$ duckdb-graph mcp                                   # stdio (default)
$ duckdb-graph mcp --transport http                  # http on 127.0.0.1:8000/mcp
$ duckdb-graph mcp --transport http --host 127.0.0.1 --port 8000
```

HTTP binds loopback `127.0.0.1:8000` at path `/mcp` by default; pass `--host`
and `--port` to override. Serving stays strictly read-only over both
transports, and both expose the identical `impact`, `explain`, and `report`
tools with the same JSON-object shapes. The HTTP transport is the groundwork
that will later let a client index a repository in a separate sandbox (for
example a GitHub repository); that remote / sandbox indexing is __future work
this transport enables__, not something shipped here.

## Setup for working on the package itself

Requirements:

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/) (Python env and builds)

Shell commands:

```shell
$ make setup     # install the Python toolchain into a local venv
$ make test      # run the test suite
$ make lint      # ruff, mypy, markdownlint
```

## The Makefile is the toolbox

Drive common tasks through `make`; do not call the underlying tools directly:

- `make` - list common targets
- `make setup` — check and install the development toolchain
- `make lint` / `make format` — check / apply ruff, mypy, markdownlint.
- `make test` — run the pytest suite.
- `make self-test` — capability evidence: index the package's own source with `duckdb-graph` and run a sequence of labelled requests (list files, list functions, change-impact traversal, cross-module symbol context, index overview, most-depended-upon hotspots), each printing a result and asserting a structural fact. Dogfoods the tool on a real multi-module codebase, distinct from the hermetic `make test` suite.
- `make build` — build the wheel and sdist (version from the git tag via hatch-vcs).

## Project structure

```text
duckdb-graph/
├── src/duckdb_graph/   # the package
│   ├── store.py            # the GraphStore API
│   ├── indexer.py          # folder → DuckDB graph index
│   ├── cli.py              # the duckdb-graph CLI (incl. the mcp verb)
│   ├── mcp_server.py       # the MCP-over-stdio server
│   ├── config.py           # Settings / DUCKDB_GRAPH_* env
│   ├── models.py           # Node / Edge / ImpactResult pydantic models
│   ├── etag.py             # compute_etag content fingerprint
│   └── extractors/         # pluggable extractors (python, javascript/typescript, markdown)
├── tests/                  # pytest suite
├── pyproject.toml          # metadata, deps, tag-driven version (hatch-vcs)
└── Makefile                # the task toolbox
```

`.md` / `.markdown` files index into the same graph as code. Releases are
tag-driven: the version comes from the git `vYYMMDD` tag, and pushing a tag
publishes to PyPI.

## Configuration

Settings resolve with the precedence: explicit argument >
`DUCKDB_GRAPH_*` environment variable > default.

| Setting | Argument | Environment variable | Default |
| --- | --- | --- | --- |
| Database path | `db_path` | `DUCKDB_GRAPH_DB_PATH` | `graph.duckdb` |
| Indexed source root | `root` | `DUCKDB_GRAPH_ROOT` | `None` |
| Read-only | `read_only` | `DUCKDB_GRAPH_READ_ONLY` | `false` |

## Pointers

- Worked examples: [documentation/examples.md](documentation/examples.md) — the `make self-test` requests, explained with observed results.
- Documentation site: <https://bernard357.github.io/duckdb-graph/>
- PyPI project: <https://pypi.org/project/duckdb-graph/>

## License

MIT.
