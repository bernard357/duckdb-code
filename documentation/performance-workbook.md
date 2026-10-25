# Performance workbook

How `duckdb-graph` behaves as a codebase grows, where it breaks, and the
decisions that keep it fast. The numbers here come from indexing the author's
`~/Public` tree (≈8,200 indexable files across a dozen repositories, ignored
directories pruned) — a deliberately large, real-world target used to find the
limits.

- [Graph indexing: resolve edges in SQL, not in a Python loop](#graph-indexing)
- [Graph traversal at scale: recursive SQL or DuckPGQ](#graph-traversal)
- [Measured scale of a large tree](#measured-scale)

## Graph indexing: resolve edges in SQL, not in a Python loop {#graph-indexing}

Indexing is two phases: __extraction__ (tree-sitter parses each file into nodes
and *unresolved* edges that carry a raw target string) and __resolution__ (the
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

Once a large graph *exists*, the second question is how `impact` and `explain`
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

__The decision is deferred to measurement, not taste.__ Which one to adopt
should be decided by a __timed comparison on a real full index of `~/Public`__,
not argued in the abstract. The honest sequence is: make the index build scale
(so a full `~/Public` graph can be built at all), then benchmark the current
Python BFS, a `WITH RECURSIVE` implementation, and a DuckPGQ implementation of
the same `impact`/`explain` queries against that graph, and pick based on the
measured latency against the added-dependency cost. `WITH RECURSIVE` wins by
default unless DuckPGQ is decisively faster or materially simpler for the
queries we actually run.

## Measured scale of a large tree {#measured-scale}

The `~/Public` evaluation, with the indexer's default ignore rules applied
(`.git`, `node_modules`, `.venv`, build and cache directories pruned):

```text
tree size            : 4.7 GB on disk
indexable files      : ~8,200  (~5,500 .py, ~2,650 .md, ~220 JS/TS)
heaviest repositories: ~4,100 files and ~2,800 files
projected graph size : ~90,000-110,000 nodes, ~150,000-250,000 edges
```

Projected node and edge counts are extrapolated from the measured small-graph
ratios (roughly 10-13 nodes and 15-30 edges per file); the exact totals await a
completed full index, which is itself gated on the set-based resolver above.
These figures are illustrative of the order of magnitude, not a committed
benchmark — the point is that a personal projects folder already reaches
six-figure graph sizes, which is squarely the scale where the indexing and
traversal decisions above stop being optional.
