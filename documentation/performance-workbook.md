# Performance workbook

How `duckdb-code` behaves as a codebase grows, where it breaks, and the
decisions that keep it fast. The numbers here come from indexing the author's
`~/Public` tree (≈8,200 indexable files across a dozen repositories, ignored
directories pruned) — a deliberately large, real-world target used to find the
limits.

- [How to optimise performance?](#how-to-optimise)
- [Graph indexing: resolve edges in SQL, not in a Python loop](#graph-indexing)
- [Graph traversal at scale: recursive SQL or DuckPGQ](#graph-traversal)
- [Measured scale of a large tree](#measured-scale)
- [Does an agent benefit? Kilo Code on ~/Public](#agent-benefit)

## How to optimise performance? {#how-to-optimise}

Three optimizations carry almost all of the speed `duckdb-code` has on a large
tree, in priority order. The first is the one you tune; the other two are built
in. Every figure below is measured on the `~/Public` tree (≈12 repositories);
the sections after this one show the full before/after and the reasoning.

### 1. Avoid indexing what you don't need — tune `.gitignore`

The cheapest work is the work never done, and the single biggest lever is
_what you exclude_. `duckdb-code` skips, by default, the `.git` directory, the
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
- Verify with `duckdb-code report`: the excluded files fold into the
  `skipped` count, and the node/edge totals should drop to the real source.
- Only reach for `--no-default-ignores` when you deliberately want a vendored
  tree in the graph; `.gitignore`/`.duckdbgraphignore` are still honored even
  then.

### 2. Index quickly — batched SQL, not per-row work

Indexing resolves cross-file edges and writes the graph. Both must scale with
`nodes + edges`, never with their product, and must persist in bulk. Two fixes
took a 2,768-file repository from "did not finish in 5+ minutes" to __~65 s__
(see [graph indexing](#graph-indexing)): an indexed last-segment lookup replaced
a per-edge full-table scan, and an edge-fan-out cap replaced a 166k→598k
best-effort edge explosion written one row at a time (~236 s of inserts alone).
Writing then still bound one row at a time through `executemany`, three
quarters of a full `~/Public` build; loading each table as numpy columns in one
statement took that build from __243 s to 31 s__ (see
[bulk loading](#bulk-loading)). You do not configure any of this — it is why a
real codebase indexes in seconds rather than hanging — but the rule generalizes
to any DuckDB-backed tool: resolve set-based, cap noisy fan-out, write columns,
not rows.

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
alone extrapolated to ~236 s. __Fix (first):__ insert nodes and edges with
`executemany`, which removed the per-call overhead but, as measured later, still
binds every row one by one: 74 s for 234,000 edge rows. __Fix (since #42):__
load each table as numpy columns scanned by one `INSERT … SELECT`, 0.23 s for
the same rows (see [bulk loading](#bulk-loading)).

With all three fixes the same repository indexes in __~65 s__ (13 s extract,
fast resolve, bulk write of ~98k edges) instead of never finishing.

__Rules of thumb.__ (1) Resolution cost must scale with `nodes + edges`, never
`nodes × edges` — index the fallback, do not scan it. (2) A best-effort match
that fans out to many candidates is noise; cap it rather than fabricate a dense
sub-graph. (3) Persist as columns (numpy arrays scanned by `INSERT … SELECT`),
never row by row, and `executemany` counts as row by row. A single slow phase
can hide behind another, so bisect by phase before optimizing.

## Bulk loading: columns, not rows {#bulk-loading}

`executemany` reads like a bulk insert, but DuckDB's Python client binds and
inserts each row of it in turn. Measured on 234,000 rows shaped like the
`edges` table (six columns, empty table, same rows checked after each path):

| Path | Time | Extra dependency |
| --- | --- | --- |
| `executemany` | 74.41 s | none |
| multi-row `VALUES`, 1,000 rows per statement | 15.24 s | none |
| CSV file + `COPY` | 0.74 s | none |
| dict of numpy arrays, one `INSERT … SELECT` | 0.23 s | numpy |

__Decision (shipped, #42).__ Every bulk write in the indexer goes through one
helper that builds a numpy column per field and inserts the registered
relation in one statement, and the per-row `UPDATE`s of centrality and churn
became one `UPDATE … FROM`. Typed arrays carry floats and integers; strings,
NULLs, and timezone-aware timestamps travel as object arrays and land in the
same types as before, with no cast. The `embedding` lists are placed one by one
into a 1-D object array, because `np.array` would turn equal-length lists into a
2-D array that DuckDB does not scan as one column; 71,000 vectors of 256 floats
load in 0.91 s instead of 20.9 s. A test builds the fixture index both ways
and checks the stored rows are identical.

A full index of `~/Public`, wall-clock per phase, before and after:

| Phase | `executemany` | numpy columns |
| --- | --- | --- |
| extract (tree-sitter, Markdown) | 20.0 s | 17.3 s |
| resolve + test edges | 1.8 s | 1.5 s |
| write files, nodes, edges | 123.3 s | 0.8 s |
| write docstrings | 3.7 s | 0.0 s |
| centrality | 29.7 s | 0.9 s |
| communities | 25.6 s | 0.5 s |
| embeddings (embed + insert) | 34.3 s | 7.6 s |
| git co-change | 3.6 s | 1.9 s |
| __total__ | __242.9 s__ | __31.2 s__ |
| throughput | 16 files/s, 1,857 rows/s | 128 files/s, 14,476 rows/s |
| peak resident memory | 1,876 MB | 1,714 MB |

The build is now dominated by extraction and the embedding model, both linear
in the codebase, and peak memory went down rather than up, so one statement per
table needs no chunking. At 128 files per second, a tree ten times the size of
`~/Public` indexes in about five minutes; parallel extraction is the next lever.

## Parallel extraction and a large codebase {#parallel-extraction}

Extraction parses every file with tree-sitter or markdown-it. Markdown is
82% of it on `~/Public` (12.0 s for 2,617 files, a 4.0 ms median, against 1.3 ms
for a Python file), and markdown-it is pure Python, so threads would stay
serialized behind the GIL. __Decision (shipped, #43).__ From 500 files on, the
indexer extracts across a pool of spawned processes, one per core up to eight
(`DUCKDB_CODE_WORKERS` overrides, `1` forces the sequential path), in chunks of
64 files. Each worker returns plain tuples, which pickle in 0.12 s for all of
`~/Public` against 0.90 s as models, and the parent merges the chunks in walk
order, so the nodes, edges, and their order are identical to a sequential run;
a test builds the fixture index both ways and compares the stored rows. A pool
takes about 0.7 s to start, which is why trees under 500 files stay sequential.

The large codebase is a workspace of three public repositories cloned at pinned
commits, each with its last 500 commits: home-assistant/core (`81e94416815c`),
microsoft/vscode (`aee59f2f5c11`), and apache/airflow (`51140809d2a7`). That is
46,222 indexed files, 11.6 times `~/Public`, mostly Python and TypeScript.
Wall-clock per phase, on an 8-core Apple M1 Pro:

| Phase | `~/Public` sequential | `~/Public` parallel | Large sequential | Large parallel |
| --- | --- | --- | --- | --- |
| extract | 17.3 s | 5.7 s | 116.7 s | 47.7 s |
| resolve edges | 1.0 s | 1.0 s | 66.8 s | 36.9 s |
| derive test edges | 0.5 s | 0.5 s | 38.0 s | 40.0 s |
| centrality | 0.9 s | 0.9 s | 35.5 s | 38.3 s |
| embeddings | 7.6 s | 6.3 s | 25.1 s | 26.5 s |
| write and the rest | 3.9 s | 3.8 s | 51.3 s | 51.8 s |
| __total__ | __31.2 s__ | __18.2 s__ | __333.4 s__ | __241.2 s__ |
| throughput | 128 files/s | 219 files/s | 139 files/s | 192 files/s |
| peak resident memory | 1.7 GB | 2.4 GB | 5.2 GB | 5.6 GB |

Both large runs store the same 948,430 nodes and 7,432,367 edges. Extraction is
2.4 times faster on the large codebase and 3.0 times on `~/Public`, short of the
core count, presumably because the parent rebuilds every node and edge from the
returned tuples one at a time; peak memory grows by the results held in both
forms. At this size the build is no longer led by extraction but by three
whole-graph passes in Python, edge resolution, test-edge derivation, and
centrality, which take 115 s together on the large codebase against 3 s on
`~/Public`, and by 7.4 million edges, about 160 per file against 60 on
`~/Public`. Those are the next levers, measured on the same corpus.

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

__Git history per repository.__ `~/Public` is not a git repository itself but
holds 17, counting the ones nested under `_obsolete/` and `temp/`. Mining each
one's last 500 commits takes 1.1 s in total, the slowest at 0.28 s for a
repository with a full 500-commit window, so the per-repository `git log`
calls are noise next to the 3 min 46 s full index. `index_status` runs one
`git status` per repository and answers in about a second.

## Does an agent benefit? Kilo Code on ~/Public {#agent-benefit}

The sections above show that `duckdb-code` indexes and queries a large tree quickly. They do not show that a coding agent is better off with it. Its tool descriptions add tokens to every request, and an agent may ignore the tools and fall back on `bash` and file reads. This section measures the effect directly: the same questions put to Kilo Code with and without `duckdb-code`, comparing tokens, latency, and answer quality.

### Method

Each question is asked in two arms that differ only in the MCP servers available:

| Arm | MCP servers |
| --- | --- |
| A (baseline) | none |
| B | `duckdb-code` only, serving a prebuilt index of `~/Public` (`--no-startup-index`, so indexing never counts toward a question's latency) |

Everything else is held constant: the model, the agent, the workspace (`~/Public`), and the global agent instructions. The runs are driven by the `kilo` binary bundled with the VS Code extension (`kilo run`), which is the engine the extension itself runs; a small spot-check in the VS Code panel confirms that the panel behaves the same. Each run sets its MCP servers with a `KILO_CONFIG_CONTENT` overlay, so the global configuration is never edited between runs. The [setup procedure](setup-kilo-code-procedure.md) describes the arm B configuration.

The question set is fixed in advance, and each question has a reference answer and a grading rubric:

- nine structural questions, one per intent `duckdb-code` targets: find by intent, the impact of a change, related tests, similar code across repositories, a repository map, history and co-change, route↔client links, analytics (cycles, dead code), and a cross-repository dependency question;
- one control question that a single file read answers, where the tools should add nothing;
- one overhead probe ("reply OK"), which measures the fixed per-request cost of the tool descriptions.

Each question runs three times per arm, every time in a fresh session. The arms alternate (A, B, A, B…), so provider prompt caching and time-of-day effects fall on both.

Figures come from the session database Kilo Code already writes, opened read-only; nothing is read off the screen. For each session:

- __context tokens__: input plus cache reads and writes, summed over the session's requests. They are reported separately from generated tokens because cache reads dominate the context;
- __generated tokens__: output plus reasoning;
- __cost__, as reported by the provider;
- __latency__: wall-clock time from the question to the last completed answer;
- __steps__: the number of model requests, and the __tool calls__ by name (`duckdb-code_<tool>` versus built-in tools);
- __correctness__: graded 0, 1, or 2 against the rubric, blind to the arm where practical.

Results are reported as the median per question per arm.

### Conditions

| Condition | Value |
| --- | --- |
| Kilo Code | 7.8.18 (VS Code extension and its bundled `kilo` binary) |
| `duckdb-code` | 261102.221350, run through `uvx` (a first pass of one repetition ran on 261101, before #38–#42) |
| Model | `amazon-bedrock/global.anthropic.claude-opus-4-6-v1`, pinned for every run |
| Agent | `ask` (read-only: edits and writes denied, read-only shell commands only), with `duckdb-code_*` allowed in arm B |
| Workspace | `~/Public`: 17 git repositories (13 at the top level, 4 nested under `_obsolete/` and `temp/`) plus personal folders |
| Index | 3,980 files indexed (8,496 seen, 4,516 skipped by ignore rules): 2,617 Markdown, 1,276 Python, 84 TypeScript, 3 JavaScript. 71,624 nodes and 235,613 content edges (308,829 with the derived community and co-change edges), schema v7. Built once in 44 s with the default embedding model, `uvx` start-up included (31 s of indexing and 1.7 GB peak memory in the #42 profile), database 225 MB |
| Hardware | Apple M1 Pro, 16 GB RAM, macOS 14.6.1 |
| Run dates | 2026-11-02, 22:49–23:29 (66 runs, arms interleaved) |

The model is pinned because an auto-router can change the model from one request to the next, and that would swamp the difference between the arms. It runs through Bedrock's global inference route, which can process requests outside the EU; the regional `eu.` route for the same model was not reachable from the account used.

The whole of `~/Public` is indexed and visible to the agent in both arms, including private, client, and personal content. Only public repositories (`duckdb-code`, `duckdb-vectors`, `boringsteps`, `ileap-mcp`) are cited in the questions and reference answers, and the published results carry metrics and grades only, never answer text.

### Success criteria

Fixed before any run:

- On the structural questions, `duckdb-code` counts as a benefit if median context tokens drop by at least 25%, median latency does not rise, and correctness is no worse than the baseline.
- The control question and the overhead probe are reported as costs, not hidden: they show what `duckdb-code` adds when it does not help.
- A cheaper wrong answer is not a gain, so token and latency deltas are only read together with correctness.

### Results

Three repetitions of every question in both arms, 66 runs, none failed; the spend was $21.36 (arm A $13.63, arm B $7.73). Medians per question, with correctness as the median grade:

| Question | Context tokens A | Context tokens B | Δ context | Latency A (s) | Latency B (s) | Δ latency | `duckdb-code` calls B | Grade A | Grade B |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| q01 find by intent | 119,577 | 204,829 | +71% | 27.7 | 36.5 | +32% | 2 | 2 | 2 |
| q02 impact | 286,666 | 231,807 | −19% | 35.6 | 25.8 | −27% | 2 | 2 | 2 |
| q03 related tests | 420,928 | 152,789 | −64% | 45.7 | 24.5 | −46% | 4 | 2 | 2 |
| q04 similar code | 124,031 | 146,752 | +18% | 15.4 | 19.2 | +24% | 0 | 2 | 2 |
| q05 repository map | 392,981 | 376,274 | −4% | 77.6 | 63.3 | −18% | 0 | 2 | 2 |
| q06 history | 612,122 | 100,030 | −84% | 80.7 | 17.2 | −79% | 1 | 2 | 2 |
| q07 links | 194,347 | 252,962 | +30% | 25.2 | 23.1 | −9% | 4 | 2 | 2 |
| q08 cycles | 597,001 | 86,496 | −86% | 59.9 | 13.5 | −77% | 2 | 0 | 2 |
| q09 cross-repository | 475,196 | 431,450 | −9% | 36.0 | 35.1 | −3% | 0 | 2 | 2 |
| q10 control | 190,113 | 121,367 | −36% | 16.5 | 12.3 | −25% | 0 | 2 | 2 |
| q11 overhead probe | 33,650 | 39,912 | +19% | 3.2 | 3.8 | +20% | 0 | 2 | 2 |

Across the nine structural questions (27 runs per arm), the median context drops from 372,864 to 179,035 tokens (−52%), the median latency from 40.8 s to 24.5 s (−40%), and the median number of model requests from 7 to 4. Correctness rises: 27 of 27 answers in arm B score 2, against 23 of 27 in arm A. The four arm A failures are one repository map that described the wrong repository and all three cycle answers, which analyzed import cycles only and missed the method recursion that `analytics cycles` reports.

__Verdict: `duckdb-code` is a benefit on structural questions.__ It clears every criterion fixed before the runs: context tokens down 52% (the bar was 25%), latency down rather than up, and correctness no worse, better on cycles. The gains concentrate where the graph answers what files alone do not: history (−84% context), cycles (−86%), and related tests (−64%).

__Where it costs.__ Find-by-intent (q01, +71%), similar code (q04, +18%), and links (q07, +30%) cost more context in arm B for the same grade: the agent calls `duckdb-code` and then reads the files anyway. The overhead probe puts the fixed price of the twelve tool descriptions at about 6,300 context tokens and 0.6 s per session. In 9 of the 27 structural B runs the agent made no `duckdb-code` call at all, every time on q04, q05, and q09, which are also the questions with the smallest gains; better tool routing is the lever there.

__Limits.__

- One model (Claude Opus 4.6 through Bedrock), one agent configuration, one workspace; three repetitions per arm give medians, not confidence intervals.
- The control question q10 came out 36% cheaper in arm B with no `duckdb-code` call, so differences of that size on a single question can be noise; the structural aggregate is the robust figure.
- Answers were graded by the agent that ran the benchmark, against the fixed rubrics, from shuffled answers with the arm hidden, with a written rationale per question audited by the author. The rubrics check the named items, so incorrect side claims, which appear in both arms, are not penalized.
- A pilot of one repetition on 261101, before #38–#42, is not included: it ran on a different index and tool set.
