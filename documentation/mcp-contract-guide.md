
# MCP tool contract

The contract between the `duckdb-code` MCP server and the IDE, CLI, or agent
that consumes it. It defines the target tool set, what each tool returns, and —
deliberately — what the server does _not_ do because a Language Server already
does it better. This is the roadmap for the tool surface; each tool's
implementation is tracked as a follow-on issue, and the data every tool reads
is defined by the [indexing strategy](indexing-strategy-workbook.md) — the two
are co-designed and must stay consistent.

- [Division of labour with LSP](#lsp)
- [The shared result contract](#result-contract)
- [Target tools](#tools)
- [Tool-to-data map](#data-map)
- [From today's four tools to the target set](#migration)
- [Non-goals](#non-goals)
- [Build order](#build-order)

## Division of labour with LSP {#lsp}

A Language Server is authoritative and live: it answers "what is at this exact
cursor position?" over the unsaved buffer. `duckdb-code` is the opposite kind
of tool — a whole-repo, precomputed, multi-language, history-aware snapshot
that answers "where in the repo should I look, and what is at stake if I change
this?". The two are complementary, and the contract stays strictly on the side
LSP cannot cover:

- __Whole-repo, not point queries.__ "Where do we retry failed payments?" and
  "what breaks if I change this?" span the repository; LSP is driven by one
  cursor position.
- __One call, not many hops.__ Transitive impact is precomputed, so a blast
  radius is one tool call, not dozens of interactive call-hierarchy round
  trips.
- __Cross-language and cross-boundary.__ Edges an LSP never sees — an HTTP
  route to its client in another language, a config key to the code that reads
  it — are first-class here.
- __History-aware.__ Git co-change and churn are structural signals no live
  language server carries.

Every result carries `path + range`, so the agent can hand a precise follow-up
straight to the LSP. The MCP answers _where and what's at stake_; the LSP
answers _what exactly is here_.

## The shared result contract {#result-contract}

Every tool returns a single JSON object. Every symbol or location in a result
carries, at minimum:

- `path` — repository-relative file path.
- `range` — start/end line (and column where available), so the consumer can
  fetch the exact text from the live buffer or hand off to the LSP.
- `id` — a stable symbol id (the graph node id), usable as the `symbol`
  argument to other tools.
- `resolution` — `exact` or `best_effort`, present wherever a binding is
  heuristic, so a cross-boundary or suffix-matched edge is never silently
  presented as certain.

`resolution` is the agent-facing _projection_ of the numeric per-edge
`confidence` that the [indexing strategy](indexing-strategy-workbook.md) stores
as the single source of truth (for example `exact` at or above 0.9,
`best_effort` below, by the threshold documented there). The raw `confidence`
may also be surfaced. There is one scoring concept, defined in the schema and
projected here — never two that can drift.

## Target tools {#tools}

The target surface is ten tools. Parameters and the shape each returns:

### `search(query, limit=…)`

Intent / ranked-symbol search — "where do we retry failed payments?" when you
do not know the name. The single biggest gap LSP leaves. Returns
`{query, results: [{id, path, range, kind, qualified_name, score}]}`, ranked by
semantic relevance.

### `overview(budget)`

The repo map an agent reads to orient itself before touching anything: modules,
entry points, and the most central symbols, ranked by centrality and sized to a
token `budget`. Returns
`{modules: […], entry_points: [{id, path, range}], central_symbols: [{id, path, range, rank}], budget}`.

### `impact(symbol, depth=…)`

Transitive reverse-dependency set — "what breaks if I change this?" — in one
call, aggregated by module with the affected entry points and the involved
tests. Returns
`{target, resolved: [node], ambiguous, affected: [{node, distance, resolution}], affected_by_module: {…}, affected_entry_points: […], related_tests: […], affected_files: […]}`.

### `links(symbol)`

Cross-boundary edges LSP never sees together: HTTP route↔client, message-queue
producer↔consumer, DI bindings, config/env keys↔readers, SQL tables↔queries and
ORM models, feature flags. Returns
`{symbol, links: [{kind, from, to, path, range, resolution}]}` with each edge's
`resolution`/`confidence`.

### `related_tests(symbol)`

What to run after an edit, by static reachability plus naming convention.
Returns `{symbol, tests: [{id, path, range, reason}]}`.

### `find_similar(symbol | snippet)`

Code built the same way — "how do we usually do this here?" — so an agent
follows house patterns instead of inventing new ones, and so duplication is
visible. Returns `{query, matches: [{id, path, range, score}]}`.

### `history(symbol)`

Git signals: recent commits touching the symbol, co-change partners (files that
usually change with it), and churn. Returns
`{symbol, commits: […], co_change: [{path, together_count}], churn}`.

### `analytics(kind)`

Whole-repo structural views for planning and review: dependency `cycles`,
`clusters`/communities, `dead_code` candidates, and `hotspots`
(complexity × churn). Returns `{kind, results: […]}` shaped per `kind`.

### `context_for_change(diff | branch)`

The touched symbols plus their graph neighbourhood — the right primitive for
code review and PR-description work. Returns
`{changed: [{id, path, range}], neighbourhood: [{id, path, range, relation}]}`.

### `index_status()`

Freshness: the indexed commit and whether the working tree is dirty, read from
the state the indexer records. Returns
`{indexed_commit, dirty, indexed_at, node_count, edge_count}`.

## Tool-to-data map {#data-map}

Each tool reads a data source the [indexing
strategy](indexing-strategy-workbook.md) defines; the schema must provide every
one, and no source should be orphaned:

| Tool | Data source (see indexing strategy) |
| --- | --- |
| `search` | per-symbol embeddings / vector storage |
| `find_similar` | per-symbol embeddings / vector storage |
| `overview` | index-time centrality + the core node/edge graph |
| `impact` | the core node/edge graph |
| `links` | cross-boundary / route edges (with `confidence`) |
| `related_tests` | TESTS edges |
| `history` | git commit + co-change data |
| `analytics` | centrality + Louvain communities (`member_of`) |
| `context_for_change` | the core graph + git diff/branch data |
| `index_status` | the indexed commit + content hash the indexer records |

The sharpest dependency is `search` / `find_similar`: they need embeddings the
current structural schema does not carry, which the indexing strategy's vector
decision provides. No tool is specified here without a data source there.

## From today's four tools to the target set {#migration}

The server ships four tools today; the move to the target set is additive, not
a rewrite:

| Today | Fate | Becomes |
| --- | --- | --- |
| `impact` | __Keep + extend__ | aggregate affected by module, add entry points and related tests |
| `explain` | __Fold__ | structural neighbourhood merges into `overview`/`context_for_change` and the per-symbol detail other tools return |
| `report` | __Reshape__ | split into `index_status` (freshness) and `overview` (repo map) |
| `export` | __Keep__ | the whole-graph JSON dump, unchanged |
| — | __Add__ | `search`, `links`, `related_tests`, `find_similar`, `history`, `analytics`, `context_for_change` |

## Non-goals {#non-goals}

The contract deliberately does __not__ provide `definition`, `references`,
`hover`, `rename`, or `diagnostics`. These are point queries an LSP answers
authoritatively over the live buffer, while this index is a lagging
commit-snapshot — duplicating them would return wrong-because-stale answers and
bloat the index. An agent that needs them uses the `path + range` in a result
to hand off to the LSP.

## Build order {#build-order}

The tools land by value, each as a follow-on implementation issue referencing
this contract and blocked on the [indexing
strategy](indexing-strategy-workbook.md) element it reads. `search` and
`find_similar` have shipped (both ranked over the `vectors` table); the rest
follow:

1. __MVP__ (most value for a small build): `search` (shipped), `overview`,
   `impact`, `related_tests`, `history`.
2. __Differentiators__ (the gap nobody covers well): `links`, `find_similar`
   (shipped).
3. __Remaining__: `analytics`, `context_for_change`, `index_status`, and
   folding `explain` / reshaping `report`.

Each follow-on issue is scoped to one primitive (or a small group that shares a
data source) so its PR stays reviewable; `search`, `find_similar`, and
`history` pull in new data sources (embeddings, git) that the indexing strategy
must land first.
