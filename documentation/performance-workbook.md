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

__The trap: direct per-edge resolution is O(nodes × edges).__ Resolving each
edge by scanning or re-deriving the candidate node set per edge means the work
grows with the product of the two tables. On small inputs this is invisible; on
a large tree it dominates and then explodes. Measured on this package's own
walk:

```text
178 files   ->  2,329 nodes,  3,176 edges  ->  1.5 s
211 files   ->  1,992 nodes,  6,919 edges  ->  1.9 s
2,768 files (one repo)                      ->  did not finish in 6.5 minutes
~8,200 files (full tree)                    ->  did not finish in 10 minutes
```

The jump from ~2 seconds at a few hundred files to "does not finish" at a few
thousand is the signature of super-linear growth, not a constant-factor
slowdown. The parse step is not the problem — it is the per-edge binding work.

__The fix: resolve with set-based SQL over indexed columns.__ Load the nodes and
the unresolved edges into DuckDB, then bind edges with a small number of SQL
statements — one join per edge *kind* against an indexed symbol table (keyed by
qualified name, by module, by title/slug for Markdown) — rather than a Python
loop that touches the candidate set once per edge. Each statement is a hash join
the engine runs in roughly O(nodes + edges); the whole resolution becomes a
handful of set operations instead of a quadratic sweep. The rows written are
identical — this is a performance refactor of *how* edges are bound, not a
change to *what* the graph contains.

__Rule of thumb.__ Resolution cost must scale with `nodes + edges`, never with
`nodes × edges`. "One SQL statement per edge kind" is the shape that holds; "one
Python pass per edge" is the shape that fails. Separating the node insert and
the edge resolve into distinct, indexed SQL steps is what lets a real codebase
of thousands of files index in seconds rather than never finishing.

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
