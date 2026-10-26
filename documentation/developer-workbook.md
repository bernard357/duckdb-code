# Developer workbook

Questions and answers for contributors to the `duckdb-graph` project:
prerequisites, setup, the Makefile workflow, testing, project principles, how to
add an extractor, and how changes land.

You may also look at the [user guide](duckdb-graph-guide.md) and the
[examples](examples.md) for more information.

## What are good first contributions?

You do not need to understand the whole codebase to help:

- __Add an extractor__ — the main extension point; see
  [below](#how-do-i-add-an-extractor).
- __Improve the docs__ — clarify a page or add a worked example.
- __Report a rough edge__ — a clear issue with a minimal reproduction.

If unsure where to start, open an issue describing what you want to do.

## What principles should a change follow?

Contributions land fastest when they fit the project's shape:

- __Do one thing well.__ This is a structural building block — `impact`,
  `explain`, `report`. Semantic and vector search belong in the sibling
  [`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors), not here.
- __Boring, composable technology.__ Prefer the standard library and DuckDB's
  own features over new dependencies.
- __Framework-agnostic.__ No hard dependency on a specific agent framework.
- __Read-mostly and fast.__ Queries never mutate the graph; writes happen only
  on `index` (including the `mcp` server's optional startup build).

## How do I add an extractor? {#how-do-i-add-an-extractor}

The extractor registry is the main extension point. An extractor parses one
language family with tree-sitter and emits the graph's nodes (`file`, `module`,
`class`, `function`, `import`) and edges (`defines`, `calls`, `inherits`,
`references`, `imports`). See `src/duckdb_graph/extractors/` for the base, the
registry, and the Python and JavaScript/TypeScript examples. Register your
extractor in the registry so the indexer picks it up for the file types it
claims, and keep heavy parser dependencies in step with the project's existing
tree-sitter bindings.

## What are the documentation conventions?

Documentation lives in `documentation/`, GitHub-Flavored Markdown with mermaid
(no ASCII art), ATX sentence-case headers, underscores for bold, and fenced code
blocks. Pages are reachable from the VitePress sidebar in
`documentation/.vitepress/config.mts`; add a sidebar entry when you add a page.

## How do I open a good pull request?

- Keep each pull request focused on a single change.
- Write a clear description: what changed and why.
- Match the surrounding style; Ruff decides formatting.
- Update the README or docstrings when you change behavior.
- Run `make lint`, `make test`, and `make self-test` first; all should pass.
- Be kind in review.

## How do I report an issue?

Open an issue with what you expected and what happened, a minimal snippet or
steps to reproduce, and your Python version plus how the store was configured
(database path, indexed root).

## What are the prerequisites?

Install these before running `make setup`. The first two are genuine
prerequisites `make setup` cannot install for you; it checks they are present
and installs the rest.

| Tool | Why | How to install |
| --- | --- | --- |
| [Python](https://www.python.org/) 3.12+ | Runs the package and the tests | python.org, pyenv, or your package manager |
| [uv](https://docs.astral.sh/uv/) | Creates the venv, installs deps, builds | `curl -LsSf https://astral.sh/uv/install.sh \| sh` or `brew install uv` |
| [Node.js](https://nodejs.org/) + npm | Provides markdownlint-cli for Markdown linting and VitePress for the docs site | nodejs.org, nvm, or your package manager |

Ruff, mypy, and pytest are not prerequisites — `make setup` installs them into
the virtual environment via the `[dev]` extra. There is no Docker or hadolint
step: `duckdb-graph` ships no Dockerfile.

## What does each Makefile target do?

The `Makefile` consolidates the most frequent commands used throughout this
project. Run `make` with no target to list them. The essentials:

- `make setup` — check prerequisites, create the `.venv`, install tooling
- `make lint` — Ruff lint and format check, mypy, and Markdown lint
- `make test` — the pytest suite
- `make self-test` — capability evidence: index the package's own source and run
  labelled requests against it
- `make format` — apply Ruff formatting
- `make build` — build the wheel and sdist with uv
- `make clean` — remove build artifacts and caches

For the documentation site: `make setup-documentation` (npm ci),
`make serve-documentation` (local dev server), `make build-documentation` (the
static site into `documentation/.vitepress/dist`), `make preview-documentation`,
and `make clean-documentation`.

## How do I set up a development environment?

```shell
$ git clone https://github.com/bernard357/duckdb-graph.git
$ cd duckdb-graph
$ make setup   # checks prerequisites, creates the venv, installs tooling
```

`make setup` runs `check-prerequisites`, then `setup-python` (uv venv + the
package with the dev extra) and `setup-markdownlint` (npm). Run
`make setup-python` for just the Python environment if you manage the linters
yourself.

`make setup` is the __contributor__ path: an editable install with the dev
extras (ruff, mypy, pytest) for running the suite and the linters. It is
distinct from the __end-user__ install story, which is `uvx duckdb-graph` (or
`uv tool install duckdb-graph`) and pulls only the runtime dependencies on
demand — see the [README](https://github.com/bernard357/duckdb-graph#install-and-run)
and the [Kilo Code runbook](setup-kilo-code-runbook.md). Reach for `make setup`
when you are changing the package, not when you are only running it.

## What does the lint cover?

```shell
$ make lint        # ruff check + ruff format --check + mypy, markdownlint
```

Python (Ruff lint and format, plus mypy type checking over `src/duckdb_graph`)
and Markdown (markdownlint via `.markdownlint.json`). There is no Dockerfile
lint — the package ships no Dockerfile. Use `make format` to apply Python
formatting. All checks should pass before you open a pull request.

## How do I run the tests?

```shell
$ make test        # pytest -q
$ make self-test   # dogfood: index the package's own source
```

`make test` is the hermetic suite under `tests/`. `make self-test` indexes the
package's own `src/duckdb_graph/` tree into a throwaway database and runs a
sequence of labelled requests, each printing a result and asserting a structural
fact — living evidence that the tool works on a real multi-module codebase. Add
a test for any behavior you change or add.

## How are releases cut?

Releases are tag-driven. The package version comes from the git `vYYMMDD` tag
via hatch-vcs; there is no version string to bump by hand. Pushing a `vYYMMDD`
tag triggers the `deploy-pypi` workflow, which builds the wheel and sdist and
publishes to PyPI over OIDC Trusted Publishing. The documentation site
redeploys to GitHub Pages from the `deploy-vitepress` workflow on push to
`main`.
