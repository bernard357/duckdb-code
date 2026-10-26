# Set up duckdb-graph as an MCP server for Kilo Code

This runbook wires `duckdb-graph` into [Kilo Code](https://kilo.ai) — the AI
coding extension for VS Code — as a local MCP server, so Kilo Code's agent gains
a structural, queryable understanding of the code in a workspace. Once set up,
the agent can ask the graph real questions instead of grepping blind:

- __what breaks if I change this?__ — `impact(symbol)` returns the transitive
  reverse-dependency set (the files and symbols that depend on a target).
- __explain this symbol/file__ — `explain(target)` returns its definitions,
  fan-in, fan-out, and (for a class) its bases and subclasses.
- __what is in this codebase?__ — `report()` profiles the index (languages,
  node and edge counts).

The server runs locally over stdio — a child process Kilo Code launches on
demand, no network exposure. Semantic / natural-language search is a
different concern handled by the sibling
[`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors); this graph
answers structural, dependency-based questions.

The setup has five stages:

- [1. Identify the workspace](#stage-1)
- [2. Install duckdb-graph locally](#stage-2)
- [3. Index the workspace](#stage-3)
- [4. Configure Kilo Code's MCP](#stage-4)
- [5. Keep the index fresh](#stage-5)
- [Troubleshooting](#troubleshooting)

## 1. Identify the workspace {#stage-1}

The workspace is the folder open in VS Code that you want Kilo Code to
understand — usually your project root. Note its __absolute path__; every later
command uses it.

```shell
# From a terminal inside the project, print the absolute path:
$ pwd
/Users/you/projects/my-app
```

Supported languages are Python, JavaScript/TypeScript, and Markdown. Only these
file extensions are indexed; everything else is skipped (see
[Troubleshooting](#troubleshooting)): `.py`, `.pyi`, `.js`, `.jsx`, `.mjs`,
`.cjs`, `.ts`, `.tsx`, `.md`, `.markdown`.

If VS Code has a __multi-root workspace__ (several folders), index each folder
separately into its own database, or pick the one folder the agent should reason
about.

## 2. Install duckdb-graph locally {#stage-2}

Install from PyPI:

```shell
$ pip install duckdb-graph
```

Or run it without installing, using [uv](https://docs.astral.sh/uv/):

```shell
$ uvx duckdb-graph --help
```

__Installing from a local clone.__ Until the PyPI release is published — or any
time you want to run an unreleased build — install from a checkout of the
repository instead. An editable install links to the source so local edits take
effect without reinstalling:

```shell
$ git clone https://github.com/bernard357/duckdb-graph.git
$ pip install -e duckdb-graph
```

Or run it straight from the clone without installing, via uv:

```shell
$ uvx --from ./duckdb-graph duckdb-graph --help
```

The version resolves from the git tag (via `hatch-vcs`); a clone with no release
tag yet reports a development version like `0.1.devN+g<commit>`, which is
expected and does not affect functionality.

Verify the console script resolves and shows the six verbs
(`index`, `report`, `impact`, `explain`, `export`, `mcp`):

```shell
$ duckdb-graph --help
```

Note the __absolute path__ of the `duckdb-graph` executable — Kilo Code needs it
in stage 4:

```shell
$ which duckdb-graph
/Users/you/.local/bin/duckdb-graph
```

## 3. Index the workspace {#stage-3}

Build the graph into a local DuckDB file. Keep it in a predictable,
git-ignorable folder under the workspace:

```shell
$ cd /Users/you/projects/my-app
$ mkdir -p .duckdb-graph
$ duckdb-graph index . --db .duckdb-graph/graph.duckdb
Indexed 31 files (14 indexed, 17 skipped), 266 nodes, 741 edges.
```

Keep the index out of version control:

```shell
$ echo '.duckdb-graph/' >> .gitignore
```

By default the indexer never walks the noise that should not be in a code
graph, which is why the `.duckdb-graph/` database and the node/edge counts stay
small even on a large repository. It skips the `.git` directory and the common
dependency/build/cache directories (`node_modules`, `.venv`/`venv`, `vendor`,
`dist`, `build`, `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`),
files larger than 1 MB, and anything matched by the workspace `.gitignore` (or
an optional `.duckdbgraphignore`). Those exclusions are counted in the report's
`skipped` total. If you genuinely need a vendored tree in the graph, re-run with
`--no-default-ignores` — the built-in directory and size exclusions are then
off, but `.gitignore` / `.duckdbgraphignore` are still honored.

The counts below are illustrative; your own numbers depend on the workspace.

Verify the graph was built — non-zero node and edge counts across the languages
present:

```shell
$ duckdb-graph report --db .duckdb-graph/graph.duckdb
Database      : .duckdb-graph/graph.duckdb
Schema version: 1
Roots         : .
Languages     : python
  python: 14 files
  node class: 21
  node function: 120
  ...
  edge calls: 471
  ...
```

This explicit `index` run is optional: if you point the MCP server at the
workspace with `--index` (stage 4), it builds the graph on startup for you. Run
`index` here only when you want the database ready before configuring Kilo Code,
or to inspect the `report` output first. Either way the database is built
read-write, and the MCP server keeps it fresh (stage 5).

## 4. Configure Kilo Code's MCP {#stage-4}

Kilo Code launches `duckdb-graph mcp` as a local stdio child process and talks
to it over stdin/stdout. Configure it through the extension UI — this is the
primary path, because Kilo Code's MCP settings file is created and edited by
this UI on first use (there is no file to hand-edit beforehand).

In VS Code: open the __Kilo Code__ panel → __Settings → MCP → Add Server__, and
choose __Local (stdio)__. Enter:

| Field | Value |
| --- | --- |
| Name | `duckdb-graph` |
| Command | the absolute path from `which duckdb-graph` (e.g. `/Users/you/.local/bin/duckdb-graph`) |
| Arguments | `mcp`, `--db`, `/Users/you/projects/my-app/.duckdb-graph/graph.duckdb`, `--index`, `/Users/you/projects/my-app` |

Pass `--index <workspace>` so the server indexes the workspace on startup: the
graph is built when the database is empty and refreshed incrementally
otherwise, so a fresh clone needs no separate `index` run. Omit `--index` (or
add `--no-startup-index`) to serve a prebuilt database as a frozen snapshot.

Use __absolute__ `--db` and `--index` paths: Kilo Code launches the child
process with a working directory you do not control, so a relative path may not
resolve.

The equivalent JSON (Kilo Code writes this for you; shown for reference — the
exact shape varies by Kilo Code version, so prefer the UI and treat this as
illustrative):

```json
{
  "mcp": {
    "duckdb-graph": {
      "type": "local",
      "command": [
        "/Users/you/.local/bin/duckdb-graph",
        "mcp",
        "--db",
        "/Users/you/projects/my-app/.duckdb-graph/graph.duckdb",
        "--index",
        "/Users/you/projects/my-app"
      ]
    }
  }
}
```

As an alternative to the `--db` argument, you can set the database path in the
server's environment instead:

```json
{
  "mcp": {
    "duckdb-graph": {
      "type": "local",
      "command": ["/Users/you/.local/bin/duckdb-graph", "mcp"],
      "env": {
        "DUCKDB_GRAPH_DB_PATH": "/Users/you/projects/my-app/.duckdb-graph/graph.duckdb",
        "DUCKDB_GRAPH_ROOT": "/Users/you/projects/my-app"
      }
    }
  }
}
```

On __Windows__, a local command is wrapped through `cmd`: set the command to
`cmd` and pass `/c`, `duckdb-graph`, `mcp`, `--db`, `<path>`, `--index`,
`<workspace>` as the arguments (per the Kilo Code MCP docs).

__Verify:__ after adding the server, Kilo Code lists `duckdb-graph` under its MCP
servers with the three tools — `impact`, `explain`, `report`. Ask the agent a
question that exercises one, e.g. "use duckdb-graph to show what depends on
`<a symbol in your code>`", and confirm it returns the reverse-dependency set.

See the [Kilo Code MCP documentation](https://kilo.ai/docs/automate/mcp/using-in-kilo-code)
for the current config reference and the Settings → MCP UI. Content was
rephrased for compliance with licensing restrictions.

## 5. Keep the index fresh {#stage-5}

With `--index` configured, the server refreshes the graph each time Kilo Code
starts it. Within a long editing session, restarting the server is the coarse
refresh and `watch` is the live one; re-index after you change files if you are
serving without `--index`. Re-indexing is incremental by default — only
changed, added, and deleted files are reconciled — so it is cheap to re-run:

```shell
$ duckdb-graph index . --db .duckdb-graph/graph.duckdb
Incremental: 1 rebuilt, 0 deleted, 31 files (14 indexed, 17 skipped), ...
```

__Automatic: `duckdb-graph watch` (recommended).__ Run the watcher as a
long-running process; it re-indexes incrementally on every relevant file change,
so the index — and the MCP server reading it — stays current with no manual
step:

```shell
$ duckdb-graph watch . --db .duckdb-graph/graph.duckdb
Watching . (initial: 266 nodes, 741 edges). Press Ctrl-C to stop.
Re-indexed: 1 rebuilt, 0 deleted, 267 nodes, 742 edges.
```

The watcher respects the same ignore rules as `index` — churn under
`node_modules`, `.venv`, `.git`, and the caches never triggers a rebuild. Note
DuckDB is single-writer: run exactly one process against a given database file.
If both `watch` and a `--index` MCP server target the same database, they
contend for the writer — pick one (let the watcher own the database and serve
the MCP server with `--no-startup-index`, or let the MCP server index on
startup and skip the watcher). If you see a lock conflict, ensure only one
process is writing that database. Run the watcher from a dedicated terminal or a VS Code task.

If you prefer an explicit, on-demand refresh instead of a long-running watcher,
there is a staleness window between an edit and the next manual re-index. Two
ways to automate that refresh:

__A VS Code task.__ Add to `.vscode/tasks.json`, then run it from the Command
Palette (or bind a key):

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "Reindex code graph",
      "type": "shell",
      "command": "duckdb-graph index . --db .duckdb-graph/graph.duckdb",
      "problemMatcher": []
    }
  ]
}
```

__A git hook.__ Re-index after each commit, merge, or branch switch — add to
`.git/hooks/post-commit`, `post-merge`, and `post-checkout`, and make them
executable:

```shell
#!/bin/sh
duckdb-graph index . --db .duckdb-graph/graph.duckdb
```

Branch switches are handled either way: `index` records the current branch and
HEAD commit in the database (visible in `duckdb-graph report` as a `Branch`
line), and the incremental re-index reconciles the files that differ between
branches — so after `git checkout` a re-index (manual, watched, or via the
`post-checkout` hook above) brings the graph to the new branch's state.

## Troubleshooting {#troubleshooting}

__`duckdb-graph: command not found`.__ The console script is not on `PATH`.
Activate the environment where you installed it, or use the absolute path from
`which duckdb-graph` (and use that absolute path in the Kilo Code command).

__Kilo Code does not list the tools.__ Re-check the Settings → MCP entry: the
command must be the absolute executable path, the transport Local (stdio), and
the `--db` and `--index` paths absolute. Confirm the same command runs in a
terminal: `duckdb-graph mcp --db <abs-path> --index <abs-workspace>` should
index then start without error (Ctrl-C to stop); the startup `indexed …` line
is printed to stderr.

__Empty or sparse graph.__ Either the indexed path was wrong, the workspace is
mostly in unsupported languages — only `.py`, `.pyi`, `.js`, `.jsx`, `.mjs`,
`.cjs`, `.ts`, `.tsx`, `.md`, `.markdown` are indexed; everything else is
silently skipped — or the source you expected is excluded by an ignore rule
(a `.gitignore` entry, a dependency/build directory, or the 1 MB size cap).
Check `duckdb-graph report --db <path>`: a high "skipped" count or a missing
language explains a thin graph. If real source is being excluded, re-index with
`--no-default-ignores` (which still honors `.gitignore`), or adjust the
workspace `.gitignore` / `.duckdbgraphignore`.

__Stale results.__ The agent sees the graph as of the last build. With
`--index` the server refreshes on startup, so restart it; otherwise re-run
`duckdb-graph index` or run `watch` (stage 5) after changing files.

__The graph is not refreshing mid-session.__ The `--index` build runs once at
startup, not continuously — it is not a watcher. Restart the MCP server to
rebuild, or run `duckdb-graph watch` to keep the graph current live, or pass
`--no-startup-index` to serve a prebuilt database as a frozen snapshot.
