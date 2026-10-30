# duckdb-code

__A frugal, embeddable code-intelligence building block for agentic systems — MCP tools that help an IDE or CLI explore and change a codebase over a local DuckDB file, no external service. Impact analysis and structure explanation over the dependency graph is the first capability.__

`duckdb-code` is a python package that exposes the actual structure of a
codebase to a local agent, in 3 steps:

1. analyze a folder of source files with tree-sitter and extract nodes and edges
2. store that graph in a single DuckDB database baked next to your agent
3. answer `impact`, `explain`, and `report` questions with a local function call

## When your agent needs structural code questions

Most agents reason over code they cannot see the shape of: they read files one
at a time and guess at the blast radius of a change. The honest requirement is
small and structural — given a symbol or a file, what depends on it, what does
it depend on, and what is the whole thing shaped like. The reflex answer is
large: stand up a code-intelligence service, index into a hosted graph
database, wire the agent to it over the network. For one repository,
read-mostly, that is a lot of moving parts to own.

For the common case — one repository, packaged with one agent — you can skip
the external service entirely. Index the code into a single DuckDB file, keep
that file next to the agent, and answer `impact` / `explain` / `report` with a
local function call, no network round-trip. Where
[`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors) answers "what
text is semantically near this query", `duckdb-code` answers "what code is
affected if this symbol or file changes" and "what is the structure of this
code". The must-be-reliable deliverable is `impact`: a transitive
reverse-dependency set over the edge graph, with every best-effort edge flagged
rather than silently dropped.

## Data architecture decisions and facts

A few deliberate choices shape how `duckdb-code` indexes and serves:

- __One DuckDB file is one graph__ — no external graph service and no network
  hop on the query path.
- __Code and Markdown stay in separate domains__ in the same database: a
  Markdown link never binds to a code symbol and a code edge never binds to a
  page, so the two corpora share one file without crosstalk.
- __Incremental, etag-driven re-index__: `index` is incremental by default. A
  full rebuild runs on an empty database or with `rebuild=True`; otherwise only
  changed, added, and deleted files are reconciled by comparing each file's
  stored `etag` against a freshly computed one. The incremental result is
  identical to a full rebuild of the same on-disk state.
- __Read-write everywhere__: the store always opens read-write, since
  incremental updates are the rule — the `mcp` server can index its target
  folder on startup (`--index`) and refresh it in place, rather than serving a
  frozen file.

## Keeping the graph fresh in an agentic IDE

Because re-indexing is incremental and cheap, the graph can track the working
tree as an agent edits it, rather than going stale the moment code changes.
Three mechanisms keep it current:

- `duckdb-code index` reconciles only what changed since the last run
  (etag-driven), so a re-index after an edit is a near-instant no-op when
  nothing moved.
- `duckdb-code watch <folder>` runs as a long-lived process and re-indexes
  incrementally on every relevant file change, so the graph stays live with no
  manual step — the natural fit for an IDE session where the agent is actively
  editing.
- `duckdb-code mcp --index <folder>` folds a startup incremental build into
  the MCP server, so a freshly launched agent serves a graph that already
  reflects the current source.

The upshot: an agent asking `impact` mid-session sees the blast radius of the
code as it is now, not as it was at the last manual index.

## Install and run

With [uv](https://docs.astral.sh/uv/), there is nothing to install first.
Run the tool with `uvx` and uv fetches the package and its dependencies on the
first call and reuses the cache after:

```shell
$ uvx duckdb-code --help                 # run without installing
$ uvx duckdb-code index ./my-project     # index a folder
```

For a persistent `duckdb-code` on your `PATH` (handy when an MCP client config
wants an absolute command path), install it as a uv tool:

```shell
$ uv tool install duckdb-code
$ duckdb-code --help
```

Prefer `pip`? `pip install duckdb-code` into a virtual environment works the
same way.

To run an unreleased build — a git ref or a local checkout — use the same
commands with `--from`:

```shell
$ uvx --from git+https://github.com/bernard357/duckdb-code duckdb-code --help
$ uvx --from /path/to/duckdb-code duckdb-code --help   # from a local clone
```

## SDK entry point

Open a store against a local `.duckdb` file and query it. The store always
opens read-write, since incremental updates are the rule for this package:

```python
from duckdb_code import CodeStore

store = CodeStore(db_path="code.duckdb")

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
from duckdb_code import CodeStore

store = CodeStore(db_path="code.duckdb")

store.index("./my-project")              # full build on an empty database
store.index("./my-project")              # incremental re-index (etag-driven)
store.index("./my-project", rebuild=True)  # force a full rebuild
```

## Indexing from the CLI

```shell
$ duckdb-code --version                               # print the version and exit
$ duckdb-code index ./my-project --db code.duckdb   # incremental by default
$ duckdb-code index ./my-project --db code.duckdb --rebuild  # full rebuild
```

The `index` verb is incremental-by-default: it runs a full build when the
database is empty and reconciles only changed, added, and deleted files on a
re-run. Pass `--rebuild` to force a full rebuild.

By default the indexer skips the noise that should never be part of a code
graph, so it never descends into dependency or build trees:

- the `.git` directory and the common dependency/build/cache directories
  `node_modules`, `.venv`, `venv`, `vendor`, `dist`, `build`, `__pycache__`,
  `.mypy_cache`, `.pytest_cache`, and `.ruff_cache`;
- files larger than 1 MB (1,000,000 bytes — a file of exactly that size is
  kept);
- anything matched by a `.gitignore` or an optional `.duckdbgraphignore`,
  honored hierarchically like git: every directory's ignore files apply to that
  directory and its descendants (patterns relative to the file's own
  directory), so nested ignore files — e.g. a per-repo `.gitignore` under an
  indexed parent — are respected, not only one at the indexed root.

Pass `--no-default-ignores` (CLI) or `use_default_ignores=False` (SDK) to
disable the built-in directory and size exclusions when you do want a vendored
tree in the graph; the `.gitignore` / `.duckdbgraphignore` rules are still
honored. Excluded files that the walk reaches (oversized or ignore-matched) are
counted in the report's combined `skipped` total; whole pruned directories are
never walked and so cost nothing.

## Indexing a folder of Markdown pages

The same graph indexes a folder of Markdown pages, so a knowledge base of
interlinked notes becomes a graph alongside code. Point `index` at a Markdown
folder:

```shell
$ duckdb-code index ./my-notes --db code.duckdb
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

Semantic / vector search is out of scope for `duckdb-code` — it answers
structural, link-based questions. For "what text is semantically near this
query", use the sibling
[`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors) package.

## MCP tool

`duckdb-code mcp` is an MCP-over-stdio server exposing four query tools:

- `impact(target, …)` — transitive reverse-dependency set for a symbol or file.
- `explain(target, …)` — structural neighbourhood of a symbol or file.
- `report()` — introspects the index: database path, schema version, languages,
  and node/edge counts.
- `export(out=…, inline=…)` — returns the whole graph as `{meta, nodes, edges}`
  JSON, writing it to a file and returning a summary by default (inline only for
  a small graph).

These four are the first slice of a larger, LSP-complementary tool contract:
whole-repo, precomputed questions (intent search, one-call impact,
cross-boundary links, history) that a Language Server structurally can't
answer, while point queries (definition, references, hover, rename,
diagnostics) stay with the LSP — every result carries `path + range` for the
hand-off. The full target surface, each tool's JSON shape, and the build order
are in the [MCP tool contract](documentation/mcp-contract-guide.md).

An MCP client spawns `duckdb-code mcp` as a child process in the same
container and talks to it over stdin/stdout; stdio is a same-container
transport, not a network protocol.

The server also serves over __Streamable HTTP__, selected with
`--transport http`:

```shell
$ duckdb-code mcp                                   # stdio (default)
$ duckdb-code mcp --transport http                  # http on 127.0.0.1:8000/mcp
$ duckdb-code mcp --transport http --host 127.0.0.1 --port 8000
```

HTTP binds loopback `127.0.0.1:8000` at path `/mcp` by default; pass `--host`
and `--port` to override. Both transports expose the identical `impact`,
`explain`, and `report` tools with the same JSON-object shapes. The HTTP
transport is the groundwork that will later let a client index a repository in
a separate sandbox (for example a GitHub repository); that remote / sandbox
indexing is __future work this transport enables__, not something shipped here.

Pass `--index <folder>` (or set `DUCKDB_CODE_ROOT`) to index that folder on
startup before serving — an incremental build, so the served graph reflects the
current source without a separate `index` run. Build progress goes to stderr
(stdout carries the MCP stdio protocol) and a failed build aborts startup. Pass
`--no-startup-index`, or a bare `--db` with no folder configured, to serve a
prebuilt database as a frozen snapshot. This is a one-shot startup build, not a
watcher.

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
- `make self-test` — capability evidence: index the package's own source with `duckdb-code` and run a sequence of labelled requests (list files, list functions, change-impact traversal, cross-module symbol context, index overview, most-depended-upon hotspots), each printing a result and asserting a structural fact. Dogfoods the tool on a real multi-module codebase, distinct from the hermetic `make test` suite.
- `make build` — build the wheel and sdist (version from the git tag via hatch-vcs).

## Project structure

```text
duckdb-code/
├── src/duckdb_code/   # the package
│   ├── store.py            # the CodeStore API
│   ├── indexer.py          # folder → DuckDB graph index
│   ├── cli.py              # the duckdb-code CLI (incl. the mcp verb)
│   ├── mcp_server.py       # the MCP-over-stdio server
│   ├── config.py           # Settings / DUCKDB_CODE_* env
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
`DUCKDB_CODE_*` environment variable > default.

| Setting | Argument | Environment variable | Default |
| --- | --- | --- | --- |
| Database path | `db_path` | `DUCKDB_CODE_DB_PATH` | `code.duckdb` |
| Indexed source root | `root` | `DUCKDB_CODE_ROOT` | `None` |

## Pointers

- Worked examples: [documentation/examples.md](https://github.com/bernard357/duckdb-code/blob/main/documentation/examples.md) — the `make self-test` requests, explained with observed results.
- Documentation site: <https://bernard357.github.io/duckdb-code/>
- PyPI project: <https://pypi.org/project/duckdb-code/>

## License

MIT.
