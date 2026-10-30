# AGENTS.md — adopting and developing duckdb-code

`duckdb-code` is a frugal, embeddable code-intelligence building block for agentic systems: MCP tools that help an IDE or CLI explore and change a codebase over a local DuckDB file, no external service. Impact analysis and structure explanation over the dependency graph is the first capability. This file is for agents: an agent __adopting__ the package in its own project, and an agent or agentic IDE __working in this repository__. For the product story, the SDK and CLI usage, and the full MCP reference, read [README.md](https://github.com/bernard357/duckdb-code/blob/main/README.md) — this file points there rather than repeating it, so the two never drift.

## Adopting the package

- __Install and run.__ `uvx duckdb-code …` runs it with nothing installed first; `uv tool install duckdb-code` puts it on `PATH`; `pip install duckdb-code` is the alternative. See [Install and run](https://github.com/bernard357/duckdb-code/blob/main/README.md#install-and-run).
- __SDK.__ Open a `CodeStore` on a local `.duckdb` file and call `impact` / `explain` / `report`; the store always opens read-write. See [SDK entry point](https://github.com/bernard357/duckdb-code/blob/main/README.md#sdk-entry-point).
- __Indexing.__ `store.index(folder)` / `duckdb-code index <folder>` is incremental by default (etag-driven; a no-change re-index is a no-op) and also indexes Markdown pages into the same graph. See [Indexing from the CLI](https://github.com/bernard357/duckdb-code/blob/main/README.md#indexing-from-the-cli) and [Indexing a folder of Markdown pages](https://github.com/bernard357/duckdb-code/blob/main/README.md#indexing-a-folder-of-markdown-pages).
- __Keeping the graph fresh.__ `duckdb-code watch <folder>` re-indexes incrementally on every change, and `duckdb-code mcp --index <folder>` folds a startup build into the server — so an agentic IDE queries the working tree as it is now. See [Keeping the graph fresh in an agentic IDE](https://github.com/bernard357/duckdb-code/blob/main/README.md#keeping-the-graph-fresh-in-an-agentic-ide).
- __MCP.__ `duckdb-code mcp` serves four tools — `impact`, `explain`, `report`, and `export` — over stdio and Streamable HTTP, each returning a single JSON object. See [MCP tool](https://github.com/bernard357/duckdb-code/blob/main/README.md#mcp-tool).
- __Semantic search is out of scope.__ For "what text is semantically near this query", use the sibling [`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors); this package answers structural, link-based questions only.

## Working in this repository

### How to implement changes

Prefer implementing single-file or single-module changes inline — edit the files directly against the existing `.venv`, run `make lint` / `make test` / `make self-test` to verify, and commit. Reserve a full multi-step workflow (worktree, planner, implement-and-review loop) for multi-file or genuinely high-risk work (new architecture, cross-cutting refactors, schema or resolver changes). The verification gate is non-negotiable either way: never claim a change works without running the lint/test/self-test gate and, for a behavioral change, an end-to-end check. Flag when a task looks borderline so a reviewed workflow can be chosen deliberately.

### The Makefile is the toolbox

Drive common tasks through `make`; do not call the underlying tools directly. Run `make` for the full list. The essentials:

- `make setup` — check prerequisites (Python 3.12+, `uv`) and install the toolchain into a local `.venv`.
- `make lint` / `make format` — check / apply ruff, mypy, and markdownlint.
- `make test` — run the pytest suite.
- `make self-test` — capability evidence: index the package's own source and run labelled requests, each asserting a structural fact.
- `make build` / `make build-documentation` — build the wheel + sdist / the VitePress docs site.

Invoke venv binaries directly (`.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/ruff`); do not activate the venv.

### Repository layout

The package lives in `src/duckdb_code/`: `store.py` (the `CodeStore` API), `indexer.py` (folder → graph), `cli.py` (the CLI, incl. the `index`, `watch`, and `mcp` verbs), `watcher.py` (the `watch` loop), `mcp_server.py` (the MCP server), `config.py` (`Settings` / `DUCKDB_CODE_*`), `models.py` (pydantic models), `etag.py` (`compute_etag`), and `extractors/` (python, javascript/typescript, markdown). Tests are in `tests/`. See [Project structure](https://github.com/bernard357/duckdb-code/blob/main/README.md#project-structure) for the annotated tree.

### Conventions

- Markdown is linted by markdownlint (`make lint`): use underscores for emphasis (`_text_`), not asterisks.
- Releases are tag-driven: the version comes from the git `vYYMMDD` tag via hatch-vcs, and pushing a tag publishes to PyPI.

## Pointers

- Documentation site: <https://bernard357.github.io/duckdb-code/>
- PyPI project: <https://pypi.org/project/duckdb-code/>
