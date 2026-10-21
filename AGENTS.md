# AGENTS.md — adopting and developing duckdb-codegraph

`duckdb-codegraph` is a frugal, embeddable code-graph building block for agentic systems: tree-sitter impact analysis and structure explanation over a local DuckDB file, no external service. This file serves two audiences: an agent **adopting** the package in its own project (first half), and an agent or agentic IDE **working in this repository** (second half).

## Install for using the package

```shell
$ pip install duckdb-codegraph
```

## SDK entry point

Open a store against a local `.duckdb` file and query it. Open read-only in the serving path, when the index is a frozen artifact baked into the container:

```python
from duckdb_codegraph import CodeGraphStore

store = CodeGraphStore(db_path="codegraph.duckdb", read_only=True)

result = store.impact("payments.charge.Charge")
for hit in result.affected:
    print(hit.distance, hit.resolution, hit.node.qualified_name)
```

`impact` returns the affected set with per-node distance and resolution; `explain` returns a node's structural neighbourhood; `report` introspects the index.

## MCP tool

`duckdb-codegraph mcp` is an MCP-over-stdio server exposing three read-only tools:

- `impact(target, …)` — transitive reverse-dependency set for a symbol or file.
- `explain(target, …)` — structural neighbourhood of a symbol or file.
- `report()` — introspects the index: database path, schema version, languages, and node/edge counts.

An MCP client spawns `duckdb-codegraph mcp` as a child process in the same container and talks to it over stdin/stdout; stdio is a same-container transport, not a network protocol.

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

Releases are tag-driven: the version comes from the git `vYYMMDD` tag, and pushing a tag publishes to PyPI.

## Pointers

- Documentation site: <https://bernard357.github.io/duckdb-codegraph/>
- PyPI project: <https://pypi.org/project/duckdb-codegraph/>
