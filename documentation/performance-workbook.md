# Performance workbook

How `duckdb-graph` behaves as a codebase grows, where it breaks, and the
decisions that keep it fast. The numbers here come from indexing the author's
`~/Public` tree (≈8,200 indexable files across a dozen repositories, ignored
directories pruned) — a deliberately large, real-world target used to find the
limits.

- [How to optimise performance?](#how-to-optimise)
- [Graph indexing: resolve edges in SQL, not in a Python loop](#graph-indexing)
- [Graph traversal at scale: recursive SQL or DuckPGQ](#graph-traversal)
- [Measured scale of a large tree](#measured-scale)

## How to optimise performance? {#how-to-optimise}

Three optimizations carry almost all of the speed `duckdb-graph` has on a large
tree, in priority order. The first is the one you tune; the other two are built
in. Every figure below is measured on the `~/Public` tree (≈12 repositories);
the sections after this one show the full before/after and the reasoning.

### 1. Avoid indexing what you don't need — tune `.gitignore`

The cheapest work is the work never done, and the single biggest lever is
_what you exclude_. `duckdb-graph` skips, by default, the `.git` directory, the
usual dependency/build directories (`node_modules`, `.venv`, `vendor`, `dist`,
`build`, `__pycache__`, the `*_cache` dirs), files over 1 MB, and anything
matched by a `.gitignore` or `.duckdbgraphignore` — honored __hierarchically__,
so every directory's ignore file applies to it and its descendants (git
semantics), not only one at the indexed root.

Honoring nested ignore files alone, on `~/Public`, removed __52% of files and
86% of edges__, cutting index time from 524 s to 104 s and the database from
251 MB to 72 MB (see [measured scale](#measured-scale)). That is a larger win
than any engine change here — generated code, build output, vendored trees, and
caches are pure noise in a code graph, and keeping them out makes every later
query faster and the impact/explain signal cleaner.

__How to tune it.__ If a `report` shows a surprising file count, or `impact`
surfaces vendored or generated symbols:

- Make sure each repository under the indexed path has its own `.gitignore`
  (they are now honored); the tool respects them automatically.
- For paths you want excluded from the graph but NOT from git, add a
  `.duckdbgraphignore` (gitignore syntax) in the relevant directory — e.g.
  ignore a large generated-fixtures tree, sample data, or a docs build output
  that git happens to track.
- Verify with `duckdb-graph report`: the excluded files fold into the
  `skipped` count, and the node/edge totals should drop to the real source.
- Only reach for `--no-default-ignores` when you deliberately want a vendored
  tree in the graph; `.gitignore`/`.duckdbgraphignore` are still honored even
  then.

### 2. Index quickly — batched SQL, not per-row work

Indexing resolves cross-file edges and writes the graph. Both must scale with
`nodes + edges`, never with their product, and must persist in bulk. Two fixes
took a 2,768-file repository from "did not finish in 5+ minutes" to __~65 s__
(see [graph indexing](#graph-indexing)): an indexed last-segment lookup replaced
a per-edge full-table scan, and an edge-fan-out cap plus `executemany` bulk
inserts replaced a 166k→598k best-effort edge explosion written one row at a
time (~236 s of inserts alone). You do not configure any of this — it is why a
real codebase indexes in seconds rather than hanging — but the rule generalizes
to any DuckDB-backed tool: resolve set-based, cap noisy fan-out, write in bulk.

### 3. Query with graph techniques in the engine

`impact` and `explain` traverse the graph with a single recursive SQL closure
(`WITH RECURSIVE`), not a per-hop round-trip loop. On the full `~/Public` graph
a deep reverse-dependency query (28,674 affected nodes) runs in __93 ms in SQL
vs 531 ms__ for the former Python breadth-first search — ~5.7× faster, identical
results (see [graph traversal](#graph-traversal)). Pushing the multi-hop walk
into the engine as one query is what keeps change-impact analysis responsive at
six-figure graph sizes. (DuckDB's DuckPGQ graph extension was evaluated and
ruled out — its parser does not activate on the pinned DuckDB; core
`WITH RECURSIVE` wins on both speed and zero added dependency.)

__In short:__ exclude aggressively (your `.gitignore` is the main dial),
re-index after edits (it is incremental and cheap), and trust that traversal is
already engine-native. The payoff compounds: on `~/Public` the combined effect
is an 86% smaller graph built 80% faster, then queried ~5.7× faster still.

## Graph indexing: resolve edges in SQL, not in a Python loop {#graph-indexing}

Indexing is two phases: __extraction__ (tree-sitter parses each file into nodes
and _unresolved_ edges that carry a raw target string) and __resolution__ (the
cross-file pass that binds every unresolved edge to a real node by building a
symbol table and matching targets against it). Extraction is linear in the
number of files and stays cheap. Resolution is where scale is won or lost.

A full index of a 2,768-file repository (`io2e-hermione-edge`) originally did not
finish in over five minutes. Bisecting the pipeline by phase found that neither
the walk nor the parse was the problem — and the cause was not one thing but
three, each masked by the next:

```text
phase        measured on the full 2,768-file repo
-----        -------------------------------------
walk+extract 13 s           (linear, fine)
resolve      1.9 s          BUT exploded 166k edges -> 598k
write        ~236 s (hang)  600k edges inserted one row at a time
```

__Cause 1 — a per-edge full-table scan in the scoped suffix fallback.__ The main
resolution loop was already dict-indexed, but the fallback that matches a bare
name to any symbol with the same final dotted segment scanned the entire symbol
table once per edge — O(scoped_edges × distinct_qualified_names). __Fix:__
precompute an index keyed by last dotted segment, turning the scan into an O(1)
lookup. Resolution dropped to ~2 s.

__Cause 2 — a fan-out explosion of best-effort edges.__ Resolution then finished
fast but emitted 598k edges from 166k inputs — 92% of them `best_effort`. A
common bare name (`run`, `handler`, `__init__`) shares its last segment with
symbols in dozens or hundreds of unrelated modules, and binding to all of them
fabricated a near-complete bipartite blow-up that is noise, not a dependency
signal. The measured fan-out distribution: 2,292 edges each matched >100
symbols, accounting for most of the blow-up. __Fix:__ cap the suffix fan-out —
when a bare-name match binds to more than a threshold of distinct symbols, treat
it as unresolvable and drop the edge. The cap must clear legitimate
moderate-fan-in symbols (a class imported by a handful of modules sits around
6); a threshold of 20 collapses the pathological tail while preserving those.
Edges fell from 598k to ~98k.

__Cause 3 — row-by-row inserts.__ Even the legitimate edge set was written with
one `execute` per row — one round-trip to DuckDB per edge. At 600k edges this
alone extrapolated to ~236 s; `executemany` bulk inserts the same rows far
faster. __Fix:__ bulk-insert nodes and edges with `executemany` (guarded for the
empty-index case, which `executemany` rejects).

With all three fixes the same repository indexes in __~65 s__ (13 s extract,
fast resolve, bulk write of ~98k edges) instead of never finishing.

__Rules of thumb.__ (1) Resolution cost must scale with `nodes + edges`, never
`nodes × edges` — index the fallback, do not scan it. (2) A best-effort match
that fans out to many candidates is noise; cap it rather than fabricate a dense
sub-graph. (3) Persist in bulk (`executemany`/`INSERT ... SELECT`), never one
round-trip per row. A single slow phase can hide behind another, so bisect by
phase before optimizing.

## Graph traversal at scale: recursive SQL or DuckPGQ {#graph-traversal}

Once a large graph _exists_, the second question is how `impact` and `explain`
traverse it. Today the traversal is a Python breadth-first search: a `visited`
set and a frontier, issuing one flat `SELECT ... WHERE dst_id IN (...)` per hop.
That is correct and fine for a single repository, but at full-tree scale — on
the order of 100,000 nodes and 200,000+ edges (see
[measured scale](#measured-scale)) — a deep or wide traversal makes many
round-trips between Python and the database, each materializing an intermediate
frontier, and the per-query latency grows with the depth and branching of the
graph.

At that scale the traversal deserves native support, pushing the whole
multi-hop walk into the engine as a single query. Two options:

- __`WITH RECURSIVE` (core DuckDB, no extension).__ A recursive common table
  expression expresses the reverse-dependency closure directly in SQL. It needs
  no new dependency, runs in one query instead of N round-trips, and keeps the
  package dependency-light. It is the conservative first choice.
- __DuckPGQ (community graph extension).__ Adds property-graph syntax
  (`CREATE PROPERTY GRAPH`, `MATCH`) and native path-finding. It is the fullest
  realization of "leveraging DuckDB's graph capabilities" and can express
  shortest-path and pattern queries ergonomically, but it is a community
  extension — an added dependency and an availability/packaging surface to
  own.

__The verdict, decided by measurement: `WITH RECURSIVE`.__ A full `~/Public`
index (172,805 nodes, 1,211,129 edges) was built as the benchmark substrate and
the three engines were timed on the same reverse-dependency queries:

```text
seed (affected)      python BFS   WITH RECURSIVE
high fan-in (28,674) 531 ms       93 ms          (~5.7x faster)
mid fan-in (3)       19 ms        55 ms
leaf (0)             8 ms         52 ms
```

`WITH RECURSIVE` is dramatically faster on the deep/wide traversal that is
actually slow, and loses only on trivial 0–3-node closures where a ~50 ms fixed
query-setup cost dominates work that is already instant — irrelevant for an
agent-facing tool. Results were identical across all seeds (same affected set).

__DuckPGQ was ruled out, also by measurement.__ The extension installs and loads
on DuckDB 2.0.0 but its SQL/PGQ parser does not activate — `CREATE PROPERTY
GRAPH` and `MATCH` fail with parser errors, because DuckPGQ is not available on
current DuckDB releases (the package is pinned to 2.0.0). Adopting it would mean
pinning DuckDB back several versions and taking a community-extension dependency
whose availability lags DuckDB — a poor trade for a core-only package.

__Two traps the implementation surfaced.__ (1) A recursive CTE that carries
`(node_id, distance)` pairs never terminates on a cyclic graph — Markdown pages
link mutually, so each pass invents a new larger distance for the same node. The
recursion must dedup on `node_id` alone (carry no distance); distance and
resolution are then computed by a bounded BFS over just the reachable subgraph.
(2) Fetching the reachable nodes one id at a time reintroduces the very
N-round-trip cost the recursive closure removes — materialize them with a single
`IN (...)` query. With both, `impact` on the 28,674-node closure runs in ~2.5 s
end-to-end (closure in SQL, then one bounded subgraph pass and one node fetch),
versus not finishing acceptably via per-hop round-trips at this scale.

__Rule of thumb.__ Push the multi-hop closure into the engine as one recursive
query deduped on node identity; keep the small post-pass (distance, resolution,
ordering) in Python over the bounded reachable subgraph; and never fetch the
result set one row at a time.

## Measured scale of a large tree {#measured-scale}

A full index of `~/Public` (4.7 GB on disk, ~12 repositories) measured twice —
once with root-only `.gitignore` handling, then again after nested `.gitignore`
files were honored hierarchically:

```text
                       root-only ignores   nested ignores honored   change
files indexed          8,189               3,932                    -52%
nodes                  172,805             61,811                   -64%
edges                  1,211,129           174,548                  -86%
  calls                607,482             82,203                   -86%
  references           463,582             41,618                   -91%
db size                251 MB              72 MB                    -71%
index time             524 s               104 s                    -80%
```

__The ignore scope is a first-order correctness and performance lever, not a
detail.__ Reading only the indexed root's `.gitignore` meant that, across a
multi-repo tree, every repository's own ignore rules were invisible — so
generated code, build output (`dist/`), vendored trees, and caches (e.g.
`.vitepress/cache`) were parsed into the graph as noise. Honoring each
directory's ignore files (patterns relative to their own directory, git
semantics) removed over half the files and ~86% of the edges, cutting index
time by 80% and the database by 71%. The remaining ~62k-node / ~175k-edge graph
is the real code-and-docs structure of the tree, not an inflated one.

Two lessons generalize: (1) a personal projects folder still reaches a
six-figure graph, which is squarely where the indexing and traversal decisions
above stop being optional; and (2) what you _exclude_ can matter more than any
engine optimization — the ~86% edge reduction here dwarfs the ~5.7x traversal
speed-up, because the cheapest work is the work you never do. The traversal
verdict above (`WITH RECURSIVE`) was measured on the larger pre-fix graph; it
holds regardless, since it is a relative comparison on the same substrate.
