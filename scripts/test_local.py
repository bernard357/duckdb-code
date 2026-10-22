"""Self-parse harness: run duckdb-codegraph against its own source.

This dogfoods the tool on a real, multi-module Python codebase (the package's
own ``src/duckdb_codegraph/``) and asserts known structural facts about the
resulting graph. It is deliberately a standalone script, not a pytest case: it
points at live source rather than a hermetic fixture, so it belongs with the
``make test-local`` dogfooding check, not the unit suite.

Assertions are structural lower-bounds and presence checks, never exact counts,
so ordinary edits to the package do not make the harness flap. The script exits
non-zero on the first failed assertion with a clear message.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from duckdb_codegraph.store import CodeGraphStore

# The package source tree, resolved relative to this script (scripts/ -> repo).
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "src" / "duckdb_codegraph"

# Symbols known to exist in the package source; their absence means the
# extractor silently dropped a definition it used to find.
EXPECTED_SYMBOLS = (
    "CodeGraphStore",
    "index_folder",
    "_rebuild_file",
    "compute_etag",
)

# The impact probe: index_folder is a load-bearing internal entry point called
# by both CodeGraphStore.index and _rebuild_file, so its reverse-dependency set
# has stable, known members. Membership is asserted on names; the provenance
# label (exact vs best_effort) is intentionally not asserted.
IMPACT_PROBE = "index_folder"
EXPECTED_DEPENDENTS = ("_rebuild_file", "index")


class CheckFailed(Exception):
    """Raised when a structural assertion about the self-parse graph fails."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def run_checks(store: CodeGraphStore) -> list[str]:
    """Run every structural check, returning a list of human-readable passes.

    Raises :class:`CheckFailed` on the first assertion that does not hold.
    """
    passed: list[str] = []

    store.index(SOURCE_DIR)

    report = store.report()
    node_total = sum(report.nodes_by_kind.values())
    edge_total = sum(report.edges_by_kind.values())
    _require(node_total > 0, "self-parse produced zero nodes")
    _require(edge_total > 0, "self-parse produced zero edges")
    _require("python" in report.languages, "python not among detected languages")
    passed.append(
        f"index built: {node_total} nodes, {edge_total} edges, python present"
    )

    for symbol in EXPECTED_SYMBOLS:
        result = store.explain(symbol)
        _require(
            len(result.resolved) > 0,
            f"expected symbol {symbol!r} not found in the self-parse graph",
        )
    passed.append(f"all expected symbols present: {', '.join(EXPECTED_SYMBOLS)}")

    impact = store.impact(IMPACT_PROBE)
    _require(
        len(impact.affected) > 0,
        f"impact({IMPACT_PROBE!r}) returned an empty reverse-dependency set",
    )
    affected_names = {node.node.name for node in impact.affected}
    hit = affected_names.intersection(EXPECTED_DEPENDENTS)
    _require(
        bool(hit),
        f"impact({IMPACT_PROBE!r}) set {sorted(affected_names)} includes none of "
        f"the expected dependents {EXPECTED_DEPENDENTS}",
    )
    passed.append(
        f"impact({IMPACT_PROBE!r}): {len(impact.affected)} affected, "
        f"includes {sorted(hit)}"
    )

    return passed


def main() -> int:
    _require_source_exists()
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "self_parse.duckdb")
        store = CodeGraphStore(db_path=db_path)
        try:
            passed = run_checks(store)
        finally:
            store.close()
    for line in passed:
        print(f"  PASS  {line}")
    print(f"test-local: {len(passed)} checks passed against {SOURCE_DIR}")
    return 0


def _require_source_exists() -> None:
    if not SOURCE_DIR.is_dir():
        raise CheckFailed(f"package source tree not found at {SOURCE_DIR}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CheckFailed as error:
        print(f"test-local: FAIL — {error}", file=sys.stderr)
        raise SystemExit(1) from error
