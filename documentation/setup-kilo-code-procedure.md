# Set up duckdb-code in Kilo Code

This procedure wires `duckdb-code` into [Kilo Code](https://kilo.ai), the AI coding extension for VS Code, as a local MCP server. Once it is set up, Kilo Code's agent can ask the code graph whole-repo questions instead of opening files one at a time. The server runs on your machine as a child process Kilo Code starts over stdio; there is no network listener, and the index is a single DuckDB file on your disk.

The agent gains twelve read-only tools, grouped the way the server's own routing guide presents them:

| Intent | Tools |
| --- | --- |
| Orient | `overview` (repo map), `search` (find symbols by intent), `find_similar` (code built the same way) |
| Assess a change | `impact` (blast radius), `related_tests`, `context_for_change` (a diff's symbols and neighbours), `links` (HTTP route↔client, Markdown links), `history` (commits, co-change, churn) |
| See the whole repo | `analytics` (cycles, clusters, dead-code candidates, hotspots) |
| Check the index | `index_status` (freshness), `report` (metadata), `export` (the whole graph) |

The steps were checked on Kilo Code 7.8.18 and `duckdb-code` 261102.221350.

- [1. Choose the folder and database](#paths)
- [2. Check that duckdb-code runs](#check)
- [3. Build the index](#index)
- [4. Add the MCP server to Kilo Code](#configure)
- [5. Let the agent call the tools without prompting](#permissions)
- [6. Verify](#verify)
- [7. Keep the index fresh](#fresh)
- [Migrate from duckdb-graph](#migrate)
- [Troubleshooting](#troubleshooting)

## 1. Choose the folder and database {#paths}

Decide two __absolute__ paths: the folder to index and the DuckDB file to build. Kilo Code starts the MCP server with a working directory you do not control, so relative paths may not resolve.

The folder can be a single repository or a tree of several, such as a projects folder. Only these extensions are indexed; everything else is skipped: `.py`, `.pyi`, `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.md`, `.markdown`.

Keep the database out of version control. Inside a repository, use a git-ignored folder:

```shell
$ cd /Users/you/projects/my-app
$ mkdir -p .duckdb-code
$ echo '.duckdb-code/' >> .gitignore
```

For a multi-repository tree, put the database next to the tree rather than inside one of its repositories, for example `/Users/you/projects.duckdb`.

## 2. Check that duckdb-code runs {#check}

With [uv](https://docs.astral.sh/uv/) there is nothing to install: `uvx` fetches the package on first use and caches it. Confirm it resolves and lists its verbs:

```shell
$ uvx duckdb-code --help
```

Pin a version (for example `uvx duckdb-code==261102.221350`) when you want every start to run the same build; leave it unpinned to pick up new releases.

Note the absolute path of `uvx`. VS Code does not always inherit your shell's `PATH`, so the Kilo Code configuration should name it in full:

```shell
$ command -v uvx
/opt/homebrew/bin/uvx
```

If you prefer a persistent install, `uv tool install duckdb-code` (or `pip install duckdb-code` in a virtual environment) gives you a `duckdb-code` executable; use its absolute path from `command -v duckdb-code` instead of `uvx duckdb-code` in the steps below. To run an unreleased build, point uv at a checkout: `uvx --from /path/to/duckdb-code duckdb-code --help`.

## 3. Build the index {#index}

There are two ways to get the graph built, and the right one depends on the folder's size.

__Small folder: let the server index on startup.__ Pass `--index <folder>` in step 4 and skip this step. The server builds the graph when the database is empty and refreshes it incrementally on every later start, so a single repository needs no separate command.

__Large tree: build once, then serve the prebuilt index.__ A multi-repository tree takes long enough to index that you do not want it on every Kilo Code start. Build it from a terminal, then serve it with `--no-startup-index` in step 4:

```shell
$ uvx duckdb-code index /Users/you/projects --db /Users/you/projects.duckdb
```

The first index also downloads the default local embedding model used by `search` and `find_similar`; later runs reuse it. For how indexing time and graph size grow on a large tree, see the [performance workbook](performance-workbook.md).

Either way, check the result:

```shell
$ uvx duckdb-code report --db /Users/you/projects.duckdb
$ uvx duckdb-code index-status --db /Users/you/projects.duckdb
```

`report` shows the files, nodes, and edges per kind; `index-status` shows the commit the index was built from and whether the working tree has changed since.

By default the indexer skips `.git`, the usual dependency, build, and cache folders (`node_modules`, `.venv`/`venv`, `vendor`, `dist`, `build`, `__pycache__`, the `*_cache` folders), files over 1 MB, and anything matched by a `.gitignore` or `.duckdbgraphignore`. Ignore files are honoured in every directory, as git does, so each repository in a tree keeps its own rules. Use `.duckdbgraphignore` (gitignore syntax) to keep something out of the graph that git still tracks, such as generated fixtures. `--no-default-ignores` turns off the built-in folder and size exclusions when you deliberately want a vendored tree; `.gitignore` and `.duckdbgraphignore` still apply.

## 4. Add the MCP server to Kilo Code {#configure}

Kilo Code 7.x reads MCP servers from the `mcp` key of its configuration file. Two scopes are available:

| Scope | File |
| --- | --- |
| Every workspace (global) | `~/.config/kilo/kilo.jsonc` |
| One project | `kilo.jsonc`, `kilo.json`, `.kilo/kilo.jsonc`, or `.kilo/kilo.json` at the project root |

Use the project scope for a database that belongs to one repository, and the global scope for a personal tree you work in from anywhere. Add an entry under `mcp`. For a small folder indexed on startup:

```jsonc
{
  "mcp": {
    "duckdb-code": {
      "type": "local",
      "command": [
        "/opt/homebrew/bin/uvx",
        "duckdb-code",
        "mcp",
        "--db",
        "/Users/you/projects/my-app/.duckdb-code/code.duckdb",
        "--index",
        "/Users/you/projects/my-app"
      ],
      "enabled": true
    }
  }
}
```

For a large tree served from the prebuilt index, replace `--index <folder>` with `--no-startup-index`:

```jsonc
{
  "mcp": {
    "duckdb-code": {
      "type": "local",
      "command": [
        "/opt/homebrew/bin/uvx",
        "duckdb-code",
        "mcp",
        "--db",
        "/Users/you/projects.duckdb",
        "--no-startup-index"
      ],
      "enabled": true
    }
  }
}
```

With a persistent install, make the first element of `command` the absolute `duckdb-code` path and drop the separate `"duckdb-code"` element. Older Kilo Code and Claude/Cursor guides use an `mcpServers` key with `command` and `args`; Kilo Code 7.x uses the `mcp` shape above, with the full command line in one `command` array.

## 5. Let the agent call the tools without prompting {#permissions}

By default Kilo Code asks for permission before each MCP tool call. In the VS Code panel you can approve the prompts one by one; a non-interactive `kilo run` rejects them, and the read-only `ask` agent asks too. Since every `duckdb-code` tool only reads the index, allow them in the same configuration file, once at top level and once for the `ask` agent:

```jsonc
{
  "permission": {
    "duckdb-code_*": "allow"
  },
  "agent": {
    "ask": {
      "permission": {
        "duckdb-code_*": "allow"
      }
    }
  }
}
```

The top-level rule covers the agents that read your permissions as they are, such as `code`. The read-only `ask` agent applies its own rules after yours and sets MCP tools back to "ask", so it needs the rule again at agent level. If your configuration already has a `permission` block, add the line inside it rather than a second block.

The rule allows only the `duckdb-code` tools; an agent that cannot edit files still cannot. To check which rule wins for an agent, run `kilo debug agent <name>`: the last `duckdb-code_*` entry in its permission list is the one that applies.

## 6. Verify {#verify}

Check that Kilo Code sees the server and that it is enabled:

```shell
$ kilo mcp list
```

The `kilo` CLI ships inside the extension (`~/.vscode/extensions/kilocode.kilo-code-<version>/bin/kilo` on macOS) if it is not on your `PATH`. `kilo config check` reports configuration mistakes, and `kilo debug config` prints the resolved configuration after every file has been merged.

The CLI reads the configuration on every run; in VS Code, __reload the window__ (Command Palette → "Developer: Reload Window") after editing it so the panel picks up the change. Then ask the agent a question the graph answers, such as "Using duckdb-code, which commit was the index built from?". Kilo Code records the calls as `duckdb-code_<tool>`, for example `duckdb-code_index_status`, and the answer should match `duckdb-code index-status` from step 3.

## 7. Keep the index fresh {#fresh}

The agent sees the graph as of the last build; `index_status` tells it when that was and whether the working tree has changed since. Re-indexing is incremental (only changed, added, and deleted files are reconciled), so a refresh is cheap.

- __Startup refresh.__ With `--index <folder>`, every server start refreshes the graph. Restart the MCP server (or reload the VS Code window) to pick up edits.
- __Live refresh.__ `duckdb-code watch <folder> --db <database>` re-indexes on every relevant file change, with the same ignore rules as `index`. Run it from a dedicated terminal or a VS Code task.
- __On demand.__ Re-run `duckdb-code index <folder> --db <database>` yourself, from a VS Code task, or from git hooks. The index records the current branch and commit, so a re-index after `git checkout` brings the graph to the new branch.

__A workspace of several repositories.__ When the indexed folder holds several git repositories but is not one itself, such as `~/Public`, the index records each repository it finds. `index_status` then lists them in `repositories`, each with its branch, commit, and dirty flag, and its top-level `dirty` is true as soon as any repository has uncommitted changes. A re-index after a `git checkout` in any of them is enough to bring the graph back in line.

A VS Code task, in `.vscode/tasks.json`, run from the Command Palette:

```json
{
  "version": "2.0.0",
  "tasks": [
    {
      "label": "Reindex code graph",
      "type": "shell",
      "command": "uvx duckdb-code index . --db .duckdb-code/code.duckdb",
      "problemMatcher": []
    }
  ]
}
```

A git hook, saved as `.git/hooks/post-commit`, `post-merge`, and `post-checkout` and made executable:

```shell
#!/bin/sh
uvx duckdb-code index . --db .duckdb-code/code.duckdb
```

DuckDB allows one writer per file. If `watch` owns the database, serve it with `--no-startup-index` so the MCP server does not compete for the writer; if the server indexes on startup, do not also run `watch` on the same file.

## Migrate from duckdb-graph {#migrate}

`duckdb-code` was previously published as `duckdb-graph`. An old configuration has an entry like this:

```jsonc
"duckdb-graph": {
  "type": "local",
  "command": ["/path/to/duckdb-graph", "mcp", "--db", "/path/to/graph.duckdb"],
  "enabled": true
}
```

Set its `enabled` to `false` (or remove the entry), add a `duckdb-code` entry as in step 4, and rebuild the index into a new database file; the old `graph.duckdb` predates the current schema. Environment variables were renamed from `DUCKDB_GRAPH_*` to `DUCKDB_CODE_*` (`DUCKDB_CODE_DB_PATH`, `DUCKDB_CODE_ROOT`, `DUCKDB_CODE_EMBEDDER`). Keep a copy of the configuration file before editing it.

## Troubleshooting {#troubleshooting}

__Kilo Code does not list the server, or it shows as failed.__ Run the configured command in a terminal, for example `/opt/homebrew/bin/uvx duckdb-code mcp --db /Users/you/projects.duckdb --no-startup-index`; it should start and wait for input (Ctrl-C to stop). Check that every path in `command` is absolute, run `kilo config check`, and reload the VS Code window.

__`uvx` or `duckdb-code` not found.__ VS Code started without your shell's `PATH`. Use the absolute path from `command -v uvx` (or `command -v duckdb-code`) as the first element of `command`.

__Tool calls are rejected.__ The permission rule from step 5 is missing, or, with the `ask` agent, present only at top level. Without it, a non-interactive `kilo run` stops with "auto-rejected permission", and the panel prompts on every call. `kilo debug agent <name>` shows which `duckdb-code_*` rule applies.

__The agent never answers, whatever the question.__ Check that Kilo Code's model is reachable before blaming the MCP server: `kilo roll-call "<provider>/<model>"` sends a one-word prompt and reports access. On Amazon Bedrock, a regional inference route (for example `eu.`) can be unavailable to an account while the `global.` route for the same model works.

__Empty or sparse graph.__ The folder is wrong, mostly in unsupported languages, or excluded by an ignore rule. `duckdb-code report` shows the skipped count and the languages found; adjust `.gitignore` / `.duckdbgraphignore`, or use `--no-default-ignores`.

__Stale answers.__ Ask the agent to run `index_status`, or run `duckdb-code index-status` yourself; if the index is behind, refresh it as in step 7.

__"Could not set lock" or a similar database error.__ Two processes are writing the same database. Keep a single writer (step 7).
