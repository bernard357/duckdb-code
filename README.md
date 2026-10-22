# duckdb-codegraph

__A frugal, embeddable code-graph building block for agentic systems — tree-sitter impact analysis and structure explanation over a local DuckDB file, no external service.__

`duckdb-codegraph` is a python package that exposes the actual structure of a
codebase to a local agent, in 3 steps:

1. analyze a folder of source files with tree-sitter and extract nodes and edges
2. store that graph in a single DuckDB database baked next to your agent
3. answer `impact`, `explain`, and `report` questions with a local function call

Where [`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors) answers
"what text is semantically near this query", `duckdb-codegraph` answers "what
code is affected if this symbol or file changes" and "what is the structure of
this code". The must-be-reliable deliverable is `impact`: a transitive
reverse-dependency set over the edge graph, with every best-effort edge flagged
rather than silently dropped.

## SDK entry point

Open a store against a local `.duckdb` file and query it. Open read-only in the
serving path, when the index is a frozen artifact baked into the container:

```python
from duckdb_codegraph import CodeGraphStore

store = CodeGraphStore(db_path="codegraph.duckdb", read_only=True)

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
from duckdb_codegraph import CodeGraphStore

store = CodeGraphStore(db_path="codegraph.duckdb")

store.index("./my-project")              # full build on an empty database
store.index("./my-project")              # incremental re-index (etag-driven)
store.index("./my-project", rebuild=True)  # force a full rebuild
```

## Indexing from the CLI

```shell
$ duckdb-codegraph index ./my-project --db codegraph.duckdb   # incremental by default
$ duckdb-codegraph index ./my-project --db codegraph.duckdb --rebuild  # full rebuild
```

The `index` verb is incremental-by-default: it runs a full build when the
database is empty and reconciles only changed, added, and deleted files on a
re-run. Pass `--rebuild` to force a full rebuild.

## MCP tool

`duckdb-codegraph mcp` is an MCP-over-stdio server exposing three read-only
tools:

- `impact(target, …)` — transitive reverse-dependency set for a symbol or file.
- `explain(target, …)` — structural neighbourhood of a symbol or file.
- `report()` — introspects the index: database path, schema version, languages,
  and node/edge counts.

An MCP client spawns `duckdb-codegraph mcp` as a child process in the same
container and talks to it over stdin/stdout; stdio is a same-container
transport, not a network protocol.

The server also serves over __Streamable HTTP__, selected with
`--transport http`:

```shell
$ duckdb-codegraph mcp                                   # stdio (default)
$ duckdb-codegraph mcp --transport http                  # http on 127.0.0.1:8000/mcp
$ duckdb-codegraph mcp --transport http --host 127.0.0.1 --port 8000
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
- `make self-test` — capability evidence: index the package's own source with `duckdb-codegraph` and run a sequence of labelled requests (list files, list functions, change-impact traversal, cross-module symbol context, index overview, most-depended-upon hotspots), each printing a result and asserting a structural fact. Dogfoods the tool on a real multi-module codebase, distinct from the hermetic `make test` suite.
- `make build` — build the wheel and sdist (version from the git tag via hatch-vcs).

## Project structure

```text
duckdb-codegraph/
├── src/duckdb_codegraph/   # the package
│   ├── store.py            # the CodeGraphStore API
│   ├── indexer.py          # folder → DuckDB graph index
│   ├── cli.py              # the duckdb-codegraph CLI (incl. the mcp verb)
│   ├── mcp_server.py       # the MCP-over-stdio server
│   ├── config.py           # Settings / DUCKDB_CODEGRAPH_* env
│   ├── models.py           # Node / Edge / ImpactResult pydantic models
│   ├── etag.py             # compute_etag content fingerprint
│   └── extractors/         # pluggable extractors (python, javascript/typescript)
├── tests/                  # pytest suite
├── pyproject.toml          # metadata, deps, tag-driven version (hatch-vcs)
└── Makefile                # the task toolbox
```

Releases are tag-driven: the version comes from the git `vYYMMDD` tag, and
pushing a tag publishes to PyPI.

## Configuration

Settings resolve with the precedence: explicit argument >
`DUCKDB_CODEGRAPH_*` environment variable > default.

| Setting | Argument | Environment variable | Default |
| --- | --- | --- | --- |
| Database path | `db_path` | `DUCKDB_CODEGRAPH_DB_PATH` | `codegraph.duckdb` |
| Indexed source root | `root` | `DUCKDB_CODEGRAPH_ROOT` | `None` |
| Read-only | `read_only` | `DUCKDB_CODEGRAPH_READ_ONLY` | `false` |

## Pointers

- Documentation site: <https://bernard357.github.io/duckdb-codegraph/>
- PyPI project: <https://pypi.org/project/duckdb-codegraph/>

## License

MIT.
