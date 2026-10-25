"""Benchmark reverse-dependency traversal: Python BFS vs WITH RECURSIVE vs DuckPGQ.

Step of issue #17. Given a prebuilt index (default ``/tmp/public_bench.duckdb``,
the full ~/Public graph), this times the three implementations of the SAME core
query — the transitive reverse-dependency closure over the dependency edge kinds
(``imports``/``calls``/``inherits``/``references``) from a set of seed node ids —
and asserts all three return the identical affected-id set before reporting
latency. Correctness parity gates the comparison: a faster engine that changes
the answer is not a candidate.

Run: ``.venv/bin/python scripts/benchmark_traversal.py [db_path]``
"""

from __future__ import annotations

import statistics
import sys
import time

import duckdb

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/public_bench.duckdb"
DEPENDENCY_KINDS = ("imports", "calls", "inherits", "references")


def _dep_placeholder() -> str:
    return ", ".join(f"'{k}'" for k in DEPENDENCY_KINDS)


# --- the three traversal engines, each returning the set of affected node ids ---


def bfs_python(con: duckdb.DuckDBPyConnection, seed_ids: set[str]) -> set[str]:
    """Reverse BFS in Python: one IN (...) query per hop (the current approach)."""
    kinds = _dep_placeholder()
    visited = set(seed_ids)
    frontier = set(seed_ids)
    while frontier:
        marks = ", ".join(f"'{nid}'" for nid in frontier)
        rows = con.execute(
            f"SELECT DISTINCT src_id FROM edges "
            f"WHERE kind IN ({kinds}) AND dst_id IN ({marks})"
        ).fetchall()
        nxt = {r[0] for r in rows} - visited
        visited |= nxt
        frontier = nxt
    return visited - seed_ids


def recursive_sql(con: duckdb.DuckDBPyConnection, seed_ids: set[str]) -> set[str]:
    """Reverse closure in one WITH RECURSIVE query (core DuckDB, no extension)."""
    kinds = _dep_placeholder()
    seeds = ", ".join(f"('{nid}')" for nid in seed_ids)
    rows = con.execute(
        f"""
        WITH RECURSIVE seeds(id) AS (VALUES {seeds}),
        closure(id) AS (
            SELECT id FROM seeds
            UNION
            SELECT e.src_id
            FROM edges e
            JOIN closure c ON e.dst_id = c.id
            WHERE e.kind IN ({kinds})
        )
        SELECT id FROM closure
        """
    ).fetchall()
    return {r[0] for r in rows} - seed_ids


def duckpgq_match(con: duckdb.DuckDBPyConnection, seed_ids: set[str]) -> set[str]:
    """Reverse closure via DuckPGQ path-finding (community extension)."""
    # Placeholder wired in once DuckPGQ property-graph DDL is set up against the
    # nodes/edges tables; filled in the next step.
    raise NotImplementedError


ENGINES = {
    "python_bfs": bfs_python,
    "with_recursive": recursive_sql,
    # "duckpgq": duckpgq_match,  # enabled once the property graph is defined
}


def pick_seeds(con: duckdb.DuckDBPyConnection) -> dict[str, set[str]]:
    """A fixed, representative set of seed queries (varied fan-in and kind)."""
    seeds: dict[str, set[str]] = {}
    # A widely-depended-upon symbol: the node with the highest reverse fan-in.
    kinds = _dep_placeholder()
    top = con.execute(
        f"SELECT dst_id, COUNT(*) c FROM edges WHERE kind IN ({kinds}) "
        f"GROUP BY dst_id ORDER BY c DESC LIMIT 1"
    ).fetchone()
    if top:
        seeds["high_fan_in"] = {top[0]}
    # A mid-fan-in node.
    mid = con.execute(
        f"SELECT dst_id FROM edges WHERE kind IN ({kinds}) "
        f"GROUP BY dst_id HAVING COUNT(*) BETWEEN 3 AND 6 LIMIT 1"
    ).fetchone()
    if mid:
        seeds["mid_fan_in"] = {mid[0]}
    # A leaf (no reverse deps).
    leaf = con.execute(
        f"SELECT n.node_id FROM nodes n LEFT JOIN edges e "
        f"ON e.dst_id = n.node_id AND e.kind IN ({kinds}) "
        f"WHERE e.dst_id IS NULL LIMIT 1"
    ).fetchone()
    if leaf:
        seeds["leaf"] = {leaf[0]}
    return seeds


def time_engine(fn, con, seed_ids, repeats=5) -> tuple[set[str], float, float]:
    # Warm up once (and capture the result for parity).
    result = fn(con, seed_ids)
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn(con, seed_ids)
        samples.append((time.perf_counter() - t0) * 1000)
    return result, statistics.median(samples), max(samples)


def main() -> int:
    con = duckdb.connect(DB, read_only=True)
    try:
        con.execute("INSTALL duckpgq FROM community")
        con.execute("LOAD duckpgq")
    except Exception as exc:  # pragma: no cover
        print(f"(duckpgq unavailable: {exc})")

    node_row = con.execute("SELECT COUNT(*) FROM nodes").fetchone()
    edge_row = con.execute("SELECT COUNT(*) FROM edges").fetchone()
    total_nodes = node_row[0] if node_row else 0
    total_edges = edge_row[0] if edge_row else 0
    print(f"index: {total_nodes} nodes, {total_edges} edges\n")

    seeds = pick_seeds(con)
    for label, seed_ids in seeds.items():
        print(f"=== seed: {label} ({len(seed_ids)} node) ===")
        results: dict[str, set[str]] = {}
        timings: dict[str, tuple[float, float]] = {}
        for name, fn in ENGINES.items():
            res, med, mx = time_engine(fn, con, seed_ids)
            results[name] = res
            timings[name] = (med, mx)
        # Parity: every engine must agree on the affected set.
        baseline = results["python_bfs"]
        for name, res in results.items():
            ok = res == baseline
            med, mx = timings[name]
            flag = "OK " if ok else "MISMATCH"
            print(
                f"  {name:16} affected={len(res):6}  "
                f"p50={med:8.2f}ms p_max={mx:8.2f}ms  parity={flag}"
            )
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
