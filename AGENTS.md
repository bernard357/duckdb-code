# AGENTS.md — adopting and developing duckdb-graph

`duckdb-graph` is a frugal, embeddable code-graph building block for agentic systems: tree-sitter impact analysis and structure explanation over a local DuckDB file, no external service. This file serves two audiences: an agent **adopting** the package in its own project (first half), and an agent or agentic IDE **working in this repository** (second half).

## Install for using the package

```shell
$ pip install duckdb-graph
```

## SDK entry point

Open a store against a local `.duckdb` file and query it. Open read-only in the serving path, when the index is a frozen artifact baked into the container:

```python
from duckdb_graph import GraphStore

store = GraphStore(db_path="graph.duckdb", read_only=True)

result = store.impact("payments.charge.Charge")
for hit in result.affected:
    print(hit.distance, hit.resolution, hit.node.qualified_name)
```

`impact` returns the affected set with per-node distance and resolution; `explain` returns a node's structural neighbourhood; `report` introspects the index.

`store.index(folder)` (and `duckdb-graph index <folder>`) is incremental-by-default: a full rebuild runs when the database is empty or `rebuild=True` / `--rebuild` is passed, and otherwise only changed, added, and deleted files are reconciled. Change detection is etag-driven — each file's stored `etag` is compared against a freshly computed `compute_etag` fingerprint, the changed/added/deleted set is reconciled, and the global cross-file resolution is re-run so edges binding to a changed file are re-resolved. A no-change re-index is a no-op, and the incremental result is identical to a full rebuild of the same on-disk state.

## MCP tool

`duckdb-graph mcp` is an MCP-over-stdio server exposing three read-only tools:

- `impact(target, …)` — transitive reverse-dependency set for a symbol or file.
- `explain(target, …)` — structural neighbourhood of a symbol or file.
- `report()` — introspects the index: database path, schema version, languages, and node/edge counts.

An MCP client spawns `duckdb-graph mcp` as a child process in the same container and talks to it over stdin/stdout; stdio is a same-container transport, not a network protocol.

The server also serves over **Streamable HTTP** with `--transport http`:

```shell
$ duckdb-graph mcp                                   # stdio (default)
$ duckdb-graph mcp --transport http                  # http on 127.0.0.1:8000/mcp
$ duckdb-graph mcp --transport http --host 127.0.0.1 --port 8000
```

HTTP binds loopback `127.0.0.1:8000` at path `/mcp` by default; `--host` and `--port` override it. Serving stays strictly read-only over both transports, which expose the identical `impact`, `explain`, and `report` tools with the same JSON-object shapes. The HTTP transport is the groundwork that will later let a client index a repository in a separate sandbox (for example a GitHub repository); that remote / sandbox indexing is **future work this transport enables**, not something shipped here.

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
│   └── extractors/         # pluggable extractors (python, javascript/typescript)
├── tests/                  # pytest suite
├── pyproject.toml          # metadata, deps, tag-driven version (hatch-vcs)
└── Makefile                # the task toolbox
```

Releases are tag-driven: the version comes from the git `vYYMMDD` tag, and pushing a tag publishes to PyPI.

## Pointers

- Documentation site: <https://bernard357.github.io/duckdb-graph/>
- PyPI project: <https://pypi.org/project/duckdb-graph/>
