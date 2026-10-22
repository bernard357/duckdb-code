"""Self-test: run duckdb-codegraph against its own source as capability evidence.

This dogfoods the tool on a real, multi-module Python codebase (the package's
own ``src/duckdb_codegraph/``) and prints a sequence of labelled *requests* —
list files, list functions, a change-impact traversal, cross-module symbol
context, an index overview, and a most-depended-upon hotspot ranking. Each
request prints a concise, human-readable result and asserts a structural
expectation, so the output reads as evidence of what the graph can answer.

It is deliberately a standalone script, not a pytest case: it points at live
source rather than a hermetic fixture, so it belongs with the ``make self-test``
dogfooding check, not the unit suite. Assertions are structural lower-bounds and
presence checks, never exact counts, so ordinary edits to the package do not
make it flap. The script exits non-zero on the first failed assertion.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from duckdb_codegraph.store import _DEPENDENCY_KINDS, CodeGraphStore

# The package source tree, resolved relative to this script (scripts/ -> repo).
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "src" / "duckdb_codegraph"

# Files known to be in the package source; their absence means the walk or the
# extractor silently dropped a module.
EXPECTED_FILES = ("store.py", "indexer.py", "cli.py", "mcp_server.py")

# Functions known to exist; their absence means the extractor dropped a def.
EXPECTED_FUNCTIONS = ("index_folder", "compute_etag")

# The impact probe: index_folder is a load-bearing internal entry point called
# by both CodeGraphStore.index and _rebuild_file, so its reverse-dependency set
# has stable, known members. Membership is asserted on names; the provenance
# label (exact vs best_effort) is intentionally not asserted.
IMPACT_PROBE = "index_folder"
EXPECTED_DEPENDENTS = ("_rebuild_file", "index")

# The class whose cross-module context the suite prints.
CONTEXT_CLASS = "CodeGraphStore"

HOTSPOT_TOP_N = 5


class CheckFailed(Exception):
    """Raised when a structural assertion about the self-parse graph fails."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def _header(label: str) -> None:
    print(f"\nREQUEST: {label}")


def request_list_files(store: CodeGraphStore) -> str:
    """List the indexed source files (file-kind nodes)."""
    _header("list files")
    rows = store._con.execute(
        "SELECT DISTINCT path FROM nodes WHERE kind = 'file' ORDER BY path"
    ).fetchall()
    paths = [row[0] for row in rows]
    for path in paths:
        print(f"  {path}")
    basenames = {Path(p).name for p in paths}
    missing = [name for name in EXPECTED_FILES if name not in basenames]
    _require(not missing, f"expected files absent from the graph: {missing}")
    return f"{len(paths)} files, all expected modules present"


def request_list_functions(store: CodeGraphStore) -> str:
    """List the indexed functions (function-kind nodes)."""
    _header("list functions")
    rows = store._con.execute(
        "SELECT qualified_name FROM nodes WHERE kind = 'function' "
        "ORDER BY qualified_name"
    ).fetchall()
    qnames = [row[0] for row in rows]
    for qname in qnames[:20]:
        print(f"  {qname}")
    if len(qnames) > 20:
        print(f"  ... and {len(qnames) - 20} more")
    names = {qn.rsplit(".", 1)[-1] for qn in qnames}
    _require(len(qnames) > 0, "no function nodes found")
    missing = [fn for fn in EXPECTED_FUNCTIONS if fn not in names]
    _require(not missing, f"expected functions absent: {missing}")
    return f"{len(qnames)} functions, incl. {', '.join(EXPECTED_FUNCTIONS)}"


def request_impact(store: CodeGraphStore) -> str:
    """Traversal: files and symbols impacted by a change to a symbol."""
    _header(f"impact of a change to {IMPACT_PROBE!r}")
    impact = store.impact(IMPACT_PROBE)
    print(f"  affected files ({len(impact.affected_files)}):")
    for path in impact.affected_files:
        print(f"    {path}")
    print(f"  affected symbols ({len(impact.affected)}):")
    for node in impact.affected:
        print(f"    d={node.distance} {node.node.kind} {node.node.qualified_name}")
    _require(
        len(impact.affected_files) > 0,
        f"impact({IMPACT_PROBE!r}) reported no affected files",
    )
    affected_names = {node.node.name for node in impact.affected}
    hit = affected_names.intersection(EXPECTED_DEPENDENTS)
    _require(
        bool(hit),
        f"impact({IMPACT_PROBE!r}) dependents {sorted(affected_names)} include "
        f"none of {EXPECTED_DEPENDENTS}",
    )
    return (
        f"{len(impact.affected_files)} files, {len(impact.affected)} symbols "
        f"impacted; dependents include {sorted(hit)}"
    )


def request_context(store: CodeGraphStore) -> str:
    """Cross-module context of a symbol via explain."""
    _header(f"cross-module context of {CONTEXT_CLASS!r}")
    result = store.explain(CONTEXT_CLASS)
    _require(
        len(result.explanations) > 0,
        f"explain({CONTEXT_CLASS!r}) resolved to nothing",
    )
    # Prefer the class definition over a same-named import node, so the printed
    # context is the class's real neighbourhood rather than an import re-export.
    exp = next(
        (e for e in result.explanations if e.node.kind == "class"),
        result.explanations[0],
    )
    print(f"  node    : {exp.node.qualified_name} ({exp.node.path})")
    print(f"  defines : {len(exp.defines)}")
    print(f"  fan_in  : {exp.fan_in_count}  fan_out: {exp.fan_out_count}")
    for dep in exp.fan_in[:10]:
        print(f"    used by : {dep.qualified_name} ({dep.path})")
    for base in exp.bases:
        print(f"    base    : {base.qualified_name}")
    for sub in exp.subclasses:
        print(f"    subclass: {sub.qualified_name}")
    _require(
        exp.fan_in_count > 0 or exp.fan_out_count > 0,
        f"{CONTEXT_CLASS!r} has no in-code context (fan_in and fan_out both 0)",
    )
    return (
        f"{CONTEXT_CLASS}: fan_in {exp.fan_in_count}, fan_out "
        f"{exp.fan_out_count}, {len(exp.defines)} definitions"
    )


def request_overview(store: CodeGraphStore) -> str:
    """One-glance profile of the indexed codebase via report()."""
    _header("index overview")
    report = store.report()
    print(f"  languages : {', '.join(report.languages)}")
    print(f"  files     : {report.files_by_language}")
    print(f"  nodes     : {report.nodes_by_kind}")
    print(f"  edges     : {report.edges_by_kind}")
    node_total = sum(report.nodes_by_kind.values())
    edge_total = sum(report.edges_by_kind.values())
    _require(node_total > 0, "overview: zero nodes")
    _require(edge_total > 0, "overview: zero edges")
    _require("python" in report.languages, "overview: python not detected")
    return f"{node_total} nodes, {edge_total} edges across {report.languages}"


def request_hotspots(store: CodeGraphStore) -> str:
    """Rank the most-depended-upon symbols by reverse-dependency fan-in."""
    _header("most-depended-upon symbols (hotspots)")
    placeholders = ", ".join("?" for _ in _DEPENDENCY_KINDS)
    rows = store._con.execute(
        f"""
        SELECT n.qualified_name, n.kind, COUNT(*) AS fan_in
        FROM edges e
        JOIN nodes n ON n.node_id = e.dst_id
        WHERE e.kind IN ({placeholders})
        GROUP BY n.qualified_name, n.kind
        ORDER BY fan_in DESC, n.qualified_name
        LIMIT {HOTSPOT_TOP_N}
        """,
        list(_DEPENDENCY_KINDS),
    ).fetchall()
    for qname, kind, fan_in in rows:
        print(f"  {fan_in:>3}  {kind:<9} {qname}")
    _require(len(rows) > 0, "hotspots: no reverse-dependency edges found")
    return f"top {len(rows)} depended-upon symbols, max fan_in {rows[0][2]}"


REQUESTS = (
    request_list_files,
    request_list_functions,
    request_impact,
    request_context,
    request_overview,
    request_hotspots,
)


def run_requests(store: CodeGraphStore) -> list[str]:
    """Index the package source, then run every request, returning summaries."""
    store.index(SOURCE_DIR)
    return [request(store) for request in REQUESTS]


def main() -> int:
    _require_source_exists()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "self_test.duckdb")
        store = CodeGraphStore(db_path=db_path)
        try:
            summaries = run_requests(store)
        finally:
            store.close()
    print("\n--- summary ---")
    for summary in summaries:
        print(f"  PASS  {summary}")
    print(f"self-test: {len(summaries)} requests passed against {SOURCE_DIR}")
    return 0


def _require_source_exists() -> None:
    if not SOURCE_DIR.is_dir():
        raise CheckFailed(f"package source tree not found at {SOURCE_DIR}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CheckFailed as error:
        print(f"self-test: FAIL — {error}", file=sys.stderr)
        raise SystemExit(1) from error
