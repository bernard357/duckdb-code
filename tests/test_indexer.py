"""The indexer: full-build counts, determinism, stamped_at, and the seam."""

import datetime
import shutil
from pathlib import Path

import pytest

from duckdb_code.etag import compute_etag
from duckdb_code.extractors.python import PythonExtractor
from duckdb_code.indexer import _rebuild_file, index_folder
from duckdb_code.store import CodeStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample"
MARKDOWN_FIXTURE = Path(__file__).parent / "fixtures" / "markdown"


def _store(tmp_path) -> CodeStore:
    return CodeStore(db_path=str(tmp_path / "code.duckdb"))


def _copy_fixture(tmp_path) -> Path:
    """Copy the committed fixture into a writable working tree under tmp_path.

    Edits and deletes happen on the copy so the committed fixture is never
    mutated (AGENTS.md: fixtures hold data only, never edit them).
    """
    work = tmp_path / "tree"
    shutil.copytree(FIXTURE, work)
    return work


def _node_ids(store: CodeStore, path: str | None = None) -> set[str]:
    if path is None:
        rows = store._con.execute("SELECT node_id FROM nodes").fetchall()
    else:
        rows = store._con.execute(
            "SELECT node_id FROM nodes WHERE path = ?", [path]
        ).fetchall()
    return {row[0] for row in rows}


def _edge_set(store: CodeStore) -> set[tuple[str, str, str, str, str]]:
    rows = store._con.execute(
        "SELECT src_id, dst_id, kind, path, resolution FROM edges"
    ).fetchall()
    return {tuple(row) for row in rows}


def _table_count(store: CodeStore, table: str) -> int:
    return store._con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_index_folder_counts(tmp_path):
    store = _store(tmp_path)
    try:
        report = index_folder(store, FIXTURE)
    finally:
        pass

    # Source files: 5 python (charge, billing, premium, __init__, leaf) + 2 ts.
    # README.txt and assets/data.txt have no language -> skipped (fix 6: a
    # no-indexable sub-folder contributes nothing and never errors).
    assert report.skipped == 2
    assert report.indexed == report.files - report.skipped
    assert report.languages.get("python", 0) == 5
    assert report.languages.get("typescript", 0) == 2
    assert report.nodes > 0
    assert report.edges > 0
    store.close()


def test_index_folder_is_deterministic(tmp_path):
    store_a = CodeStore(db_path=str(tmp_path / "a.duckdb"))
    store_b = CodeStore(db_path=str(tmp_path / "b.duckdb"))

    report_a = index_folder(store_a, FIXTURE)
    report_b = index_folder(store_b, FIXTURE)
    assert report_a.model_dump() == report_b.model_dump()

    ids_a = [
        row[0]
        for row in store_a._con.execute(
            "SELECT node_id FROM nodes ORDER BY node_id"
        ).fetchall()
    ]
    ids_b = [
        row[0]
        for row in store_b._con.execute(
            "SELECT node_id FROM nodes ORDER BY node_id"
        ).fetchall()
    ]
    assert ids_a == ids_b
    store_a.close()
    store_b.close()


def test_rerun_over_existing_db_is_incremental_noop(tmp_path):
    store = _store(tmp_path)
    first = index_folder(store, FIXTURE)
    assert first.mode == "full"
    second = index_folder(store, FIXTURE)
    # Incremental-by-default: the second run reconciles nothing, so it is a
    # no-op whose whole-graph totals equal the first run's.
    assert second.mode == "incremental"
    assert second.rebuilt == 0
    assert second.deleted == 0
    assert second.files == first.files
    assert second.indexed == first.indexed
    assert second.skipped == first.skipped
    assert second.nodes == first.nodes
    assert second.edges == first.edges
    assert second.languages == first.languages
    store.close()


def test_files_rows_carry_non_null_stamped_at(tmp_path):
    store = _store(tmp_path)
    index_folder(store, FIXTURE)
    rows = store._con.execute("SELECT path, stamped_at FROM files").fetchall()
    assert rows
    assert all(row[1] is not None for row in rows)
    store.close()


def test_cross_file_edges_resolved_in_db(tmp_path):
    store = _store(tmp_path)
    index_folder(store, FIXTURE)
    # Premium inherits Charge: an inherits edge should resolve to the Charge
    # class node, exact (bound via by_local from the import).
    rows = store._con.execute(
        "SELECT resolution FROM edges WHERE kind = 'inherits'"
    ).fetchall()
    assert rows
    store.close()


def test_rebuild_file_rebuilds_one_file_leaving_others_untouched(tmp_path):
    store = _store(tmp_path)
    index_folder(store, FIXTURE)

    other_before = store._con.execute(
        "SELECT node_id, kind, name, qualified_name, path, start_line, end_line, "
        "language FROM nodes WHERE path != ? ORDER BY node_id",
        ["payments/billing.py"],
    ).fetchall()

    rel = "payments/billing.py"
    text = (FIXTURE / rel).read_text(encoding="utf-8")
    extraction = PythonExtractor().extract(text, rel)
    stamp = datetime.datetime.now(datetime.UTC)
    _rebuild_file(store, rel, extraction, "python", compute_etag(text), stamp)

    other_after = store._con.execute(
        "SELECT node_id, kind, name, qualified_name, path, start_line, end_line, "
        "language FROM nodes WHERE path != ? ORDER BY node_id",
        ["payments/billing.py"],
    ).fetchall()
    assert other_before == other_after

    # The rebuilt file's rows are present and keyed to it.
    billing_rows = store._con.execute(
        "SELECT COUNT(*) FROM nodes WHERE path = ?", [rel]
    ).fetchone()
    assert billing_rows[0] == len(extraction.nodes)
    store.close()


def test_index_markdown_folder_counts_and_resolved_links(tmp_path):
    store = _store(tmp_path)
    report = index_folder(store, MARKDOWN_FIXTURE)

    assert report.languages.get("markdown", 0) == 4
    kinds = dict(
        store._con.execute("SELECT kind, COUNT(*) FROM nodes GROUP BY kind").fetchall()
    )
    assert kinds.get("page", 0) > 0
    assert kinds.get("heading", 0) > 0
    assert kinds.get("tag", 0) > 0
    # links_to edges resolved into the edges table, pointing at real page ids.
    links = store._con.execute(
        "SELECT dst_id FROM edges WHERE kind = 'links_to'"
    ).fetchall()
    assert links
    page_ids = {
        row[0]
        for row in store._con.execute(
            "SELECT node_id FROM nodes WHERE kind = 'page'"
        ).fetchall()
    }
    assert all(row[0] in page_ids for row in links)
    # Tag nodes are de-duped corpus-wide: one row per distinct tag name even
    # when the tag is used in several files.
    tag_ids = store._con.execute(
        "SELECT node_id FROM nodes WHERE kind = 'tag'"
    ).fetchall()
    assert len(tag_ids) == len({row[0] for row in tag_ids})
    # tagged edges resolve to those tag nodes.
    tag_id_set = {row[0] for row in tag_ids}
    tagged = store._con.execute(
        "SELECT dst_id FROM edges WHERE kind = 'tagged'"
    ).fetchall()
    assert tagged
    assert all(row[0] in tag_id_set for row in tagged)
    store.close()


def test_index_markdown_is_deterministic(tmp_path):
    store_a = CodeStore(db_path=str(tmp_path / "ma.duckdb"))
    store_b = CodeStore(db_path=str(tmp_path / "mb.duckdb"))
    index_folder(store_a, MARKDOWN_FIXTURE)
    index_folder(store_b, MARKDOWN_FIXTURE)
    assert _node_ids(store_a) == _node_ids(store_b)
    assert _edge_set(store_a) == _edge_set(store_b)
    store_a.close()
    store_b.close()


def test_index_folder_rejects_non_directory(tmp_path):
    store = _store(tmp_path)
    missing = tmp_path / "does-not-exist"
    try:
        raised = False
        try:
            index_folder(store, missing)
        except NotADirectoryError:
            raised = True
        assert raised
    finally:
        store.close()


def test_incremental_edit_rebuilds_only_changed_file(tmp_path):
    work = _copy_fixture(tmp_path)
    store = _store(tmp_path)
    index_folder(store, work)
    others_before = _node_ids(store) - _node_ids(store, "payments/billing.py")

    billing = work / "payments" / "billing.py"
    billing.write_text(
        billing.read_text(encoding="utf-8") + "\n\ndef added():\n    return 1\n",
        encoding="utf-8",
    )
    report = index_folder(store, work)

    assert report.mode == "incremental"
    assert report.rebuilt == 1
    assert report.deleted == 0
    # Every other file's node-id set is unchanged; only billing.py's grew.
    others_after = _node_ids(store) - _node_ids(store, "payments/billing.py")
    assert others_after == others_before
    names = store._con.execute(
        "SELECT name FROM nodes WHERE path = ? AND kind = 'function'",
        ["payments/billing.py"],
    ).fetchall()
    assert ("added",) in names
    store.close()


def test_incremental_cross_file_edges_are_re_resolved(tmp_path):
    work = _copy_fixture(tmp_path)
    inc_store = _store(tmp_path)
    index_folder(inc_store, work)

    # Edit charge.py (the file the other modules bind to). Premium(Charge) and
    # billing's `from payments.charge import Charge` bind to symbols in this
    # file, whose node ids shift when a line is inserted above them; a correct
    # incremental run must re-resolve those cross-file edges to the new ids.
    charge = work / "payments" / "charge.py"
    text = charge.read_text(encoding="utf-8")
    charge.write_text("# edited header\n" + text, encoding="utf-8")
    report = index_folder(inc_store, work)
    assert report.mode == "incremental"
    assert report.rebuilt == 1

    full_store = CodeStore(db_path=str(tmp_path / "full.duckdb"))
    index_folder(full_store, work, rebuild=True)

    assert _edge_set(inc_store) == _edge_set(full_store)
    # The binding edges to the changed file are present and resolved.
    premium_inherits = inc_store._con.execute(
        "SELECT resolution FROM edges WHERE kind = 'inherits' "
        "AND path = 'payments/premium.py'"
    ).fetchall()
    assert premium_inherits
    inc_store.close()
    full_store.close()


def test_incremental_delete_drops_rows_and_dangling_edges(tmp_path):
    work = _copy_fixture(tmp_path)
    store = _store(tmp_path)
    index_folder(store, work)

    (work / "payments" / "premium.py").unlink()
    report = index_folder(store, work)

    assert report.mode == "incremental"
    assert report.deleted == 1
    # The deleted file's files/nodes rows are gone.
    assert _node_ids(store, "payments/premium.py") == set()
    assert (
        store._con.execute(
            "SELECT COUNT(*) FROM files WHERE path = ?", ["payments/premium.py"]
        ).fetchone()[0]
        == 0
    )
    # No surviving edge references a node id that no longer exists.
    dangling = store._con.execute(
        "SELECT COUNT(*) FROM edges e "
        "WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.node_id = e.dst_id) "
        "AND NOT EXISTS (SELECT 1 FROM nodes n WHERE n.node_id = e.src_id)"
    ).fetchone()[0]
    assert dangling == 0
    assert _edge_set(store) == _edge_set_of_full_rebuild(tmp_path, work)
    store.close()


def _edge_set_of_full_rebuild(
    tmp_path, work: Path
) -> set[tuple[str, str, str, str, str]]:
    full = CodeStore(db_path=str(tmp_path / "full_cmp.duckdb"))
    index_folder(full, work, rebuild=True)
    edges = _edge_set(full)
    full.close()
    return edges


def test_incremental_no_change_is_a_noop(tmp_path):
    work = _copy_fixture(tmp_path)
    store = _store(tmp_path)
    index_folder(store, work)

    counts_before = {t: _table_count(store, t) for t in ("files", "nodes", "edges")}
    ids_before = _node_ids(store)

    report = index_folder(store, work)

    assert report.mode == "incremental"
    assert report.rebuilt == 0
    assert report.deleted == 0
    counts_after = {t: _table_count(store, t) for t in ("files", "nodes", "edges")}
    assert counts_after == counts_before
    assert _node_ids(store) == ids_before
    store.close()


def test_full_and_incremental_reach_identical_state(tmp_path):
    # Reach final tree state S incrementally from a prior state, and compare to
    # a full rebuild of S: node-id and edge sets must be identical.
    work = _copy_fixture(tmp_path)
    inc_store = _store(tmp_path)
    index_folder(inc_store, work)

    # Mutate: edit billing, delete premium, add a new module.
    billing = work / "payments" / "billing.py"
    billing.write_text(
        billing.read_text(encoding="utf-8") + "\n\ndef extra():\n    return 2\n",
        encoding="utf-8",
    )
    (work / "payments" / "premium.py").unlink()
    (work / "payments" / "refund.py").write_text(
        "from payments.charge import Charge\n\n\n"
        "def refund(amount):\n    return Charge().run(amount)\n",
        encoding="utf-8",
    )
    index_folder(inc_store, work)

    full_store = CodeStore(db_path=str(tmp_path / "full.duckdb"))
    index_folder(full_store, work, rebuild=True)

    assert _node_ids(inc_store) == _node_ids(full_store)
    assert _edge_set(inc_store) == _edge_set(full_store)
    inc_store.close()
    full_store.close()


# Bulk loading (numpy-scanned columns) must store exactly what row-wise
# `executemany` stores: same rows, same values, same types.

_STAMP = datetime.datetime(2026, 11, 2, 20, 0, 0, 123456, tzinfo=datetime.UTC)

_SHAPES = {
    "files": (
        ["path", "language", "etag", "stamped_at"],
        [("a.py", "python", "e1", _STAMP), ("b.md", "markdown", "e2", _STAMP)],
    ),
    "commits": (
        ["sha", "path", "committed_at", "summary", "ordinal"],
        [
            ("abc", "a.py", "2026-11-02T20:44:54+01:00", "one", 0),
            ("def", "b.py", None, "two", 1),
        ],
    ),
    "edges": (
        ["src_id", "dst_id", "kind", "path", "resolution", "confidence"],
        [("a", "b", "calls", "m.py", "exact", 1.0), ("a", "c", "calls", "", "x", 0.55)],
    ),
    "vectors": (
        ["node_id", "embedding"],
        [("n1", [0.25, 0.5, 1.0]), ("n2", [1.0, 0.0, -0.5])],
    ),
    "repositories": (
        ["path", "branch", "head_commit"],
        [("", "main", "abc"), ("x", None, None)],
    ),
}


def _schema_connection():
    import duckdb

    from duckdb_code.store import SCHEMA_DDL

    con = duckdb.connect(":memory:")
    for ddl in SCHEMA_DDL:
        con.execute(ddl)
    return con


def _row_wise_insert(con, table, columns, rows):
    """The reference: what `executemany` stores for the same rows."""
    if rows:
        marks = ", ".join("?" for _ in columns)
        con.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})", rows
        )


def _row_wise_update(con, table, key, column, pairs):
    if pairs:
        con.executemany(
            f"UPDATE {table} SET {column} = ? WHERE {key} = ?",
            [(value, key_value) for key_value, value in pairs],
        )


@pytest.mark.parametrize("table", sorted(_SHAPES))
def test_bulk_insert_stores_what_executemany_stores(table):
    from duckdb_code.indexer import _bulk_insert

    columns, rows = _SHAPES[table]
    reference, bulk = _schema_connection(), _schema_connection()
    _row_wise_insert(reference, table, columns, rows)
    _bulk_insert(bulk, table, columns, rows)
    query = f"SELECT * FROM {table} ORDER BY ALL"
    assert bulk.execute(query).fetchall() == reference.execute(query).fetchall()
    describe = f"DESCRIBE {table}"
    assert bulk.execute(describe).fetchall() == reference.execute(describe).fetchall()


def test_bulk_insert_accepts_no_rows():
    from duckdb_code.indexer import _bulk_insert

    con = _schema_connection()
    _bulk_insert(con, "edges", ["src_id", "dst_id"], [])
    assert con.execute("SELECT count(*) FROM edges").fetchone() == (0,)


def test_bulk_update_sets_each_keyed_value():
    from duckdb_code.indexer import _bulk_insert, _bulk_update

    con = _schema_connection()
    columns, rows = _SHAPES["files"]
    _bulk_insert(con, "files", columns, rows)
    _bulk_update(con, "files", "path", "churn", [("a.py", 3), ("b.md", 7)])
    _bulk_update(con, "files", "path", "churn", [])
    assert con.execute("SELECT path, churn FROM files ORDER BY path").fetchall() == [
        ("a.py", 3),
        ("b.md", 7),
    ]


def _graph_rows(db_path: str) -> dict[str, list]:
    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    try:
        # `files.stamped_at` is the build time, which differs between builds.
        selects = {
            "nodes": "SELECT * FROM nodes",
            "edges": "SELECT * FROM edges",
            "files": "SELECT * EXCLUDE (stamped_at) FROM files",
            "vectors": "SELECT * FROM vectors",
            "commits": "SELECT * FROM commits",
            "docstrings": "SELECT * FROM docstrings",
        }
        return {
            table: con.execute(f"{select} ORDER BY ALL").fetchall()
            for table, select in selects.items()
        }
    finally:
        con.close()


def test_bulk_loaded_index_matches_a_row_wise_build(tmp_path, monkeypatch):
    import duckdb_code.indexer as indexer

    bulk_db = str(tmp_path / "bulk.duckdb")
    store = CodeStore(db_path=bulk_db, root=str(FIXTURE), embedder="hashing")
    index_folder(store, FIXTURE)
    store.close()

    monkeypatch.setattr(indexer, "_bulk_insert", _row_wise_insert)
    monkeypatch.setattr(indexer, "_bulk_update", _row_wise_update)
    reference_db = str(tmp_path / "reference.duckdb")
    store = CodeStore(db_path=reference_db, root=str(FIXTURE), embedder="hashing")
    index_folder(store, FIXTURE)
    store.close()

    bulk, reference = _graph_rows(bulk_db), _graph_rows(reference_db)
    for table in reference:
        assert bulk[table] == reference[table], table
    assert reference["nodes"] and reference["edges"] and reference["vectors"]


# Parallel extraction (a process pool over chunks of files) must give exactly
# what the sequential walk gives, in the same order.


@pytest.mark.slow
@pytest.mark.parametrize("fixture", [FIXTURE, MARKDOWN_FIXTURE])
def test_parallel_extraction_matches_sequential(fixture):
    from duckdb_code.indexer import _extract_folder

    sequential = _extract_folder(fixture, True, workers=1)
    parallel = _extract_folder(fixture, True, workers=2, min_files=0, chunk_size=1)
    assert parallel == sequential
    rows, nodes, edges, _languages, _skipped, _docstrings = sequential
    assert rows and nodes and edges


@pytest.mark.slow
def test_parallel_build_stores_the_same_rows(tmp_path, monkeypatch):
    import duckdb_code.indexer as indexer

    monkeypatch.setenv("DUCKDB_CODE_WORKERS", "1")
    sequential_db = str(tmp_path / "sequential.duckdb")
    store = CodeStore(db_path=sequential_db, root=str(FIXTURE), embedder="hashing")
    index_folder(store, FIXTURE)
    store.close()

    monkeypatch.setenv("DUCKDB_CODE_WORKERS", "2")
    monkeypatch.setattr(indexer, "_PARALLEL_MIN_FILES", 0)
    parallel_db = str(tmp_path / "parallel.duckdb")
    store = CodeStore(db_path=parallel_db, root=str(FIXTURE), embedder="hashing")
    index_folder(store, FIXTURE)
    store.close()

    assert _graph_rows(parallel_db) == _graph_rows(sequential_db)


def test_worker_count_honours_the_environment(monkeypatch):
    from duckdb_code.indexer import _worker_count

    monkeypatch.setenv("DUCKDB_CODE_WORKERS", "1")
    assert _worker_count() == 1
    monkeypatch.setenv("DUCKDB_CODE_WORKERS", "3")
    assert _worker_count() == 3
    monkeypatch.delenv("DUCKDB_CODE_WORKERS")
    assert 1 <= _worker_count() <= 8


# The large-graph passes (resolution, test edges) must produce exactly what
# their straightforward reference versions produce.


def _generated_tree(root: Path, modules: int = 300) -> Path:
    """A tree of ``modules`` modules calling each other, with test files."""
    tree = root / "gen"
    (tree / "pkg").mkdir(parents=True)
    (tree / "tests").mkdir()
    (tree / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    for i in range(modules):
        callee = (i * 7 + 3) % modules
        (tree / "pkg" / f"m{i}.py").write_text(
            f"from pkg.m{callee} import f{callee}\n\n\n"
            f"class C{i}:\n    def run(self):\n        return self.value\n\n\n"
            f"def f{i}(x):\n    return f{callee}(x) + x.value\n",
            encoding="utf-8",
        )
        if i % 5 == 0:
            (tree / "tests" / f"test_m{i}.py").write_text(
                f"from pkg.m{i} import C{i}, f{i}\n\n\n"
                f"def test_f{i}():\n    assert f{i}(1) and C{i}().run()\n",
                encoding="utf-8",
            )
    return tree


def _reference_test_edges(nodes, resolved):
    """The per-edge reference: `_is_test_path` evaluated for every edge."""
    from duckdb_code.indexer import _TEST_REACH_KINDS, _is_test_path
    from duckdb_code.models import Edge

    path_by_id = {node.node_id: node.path for node in nodes}
    seen: set[tuple[str, str]] = set()
    out = []
    for edge in resolved:
        if edge.kind not in _TEST_REACH_KINDS:
            continue
        src_path = path_by_id.get(edge.src_id)
        dst_path = path_by_id.get(edge.dst_id)
        if src_path is None or dst_path is None:
            continue
        if not _is_test_path(src_path) or _is_test_path(dst_path):
            continue
        if (edge.src_id, edge.dst_id) in seen:
            continue
        seen.add((edge.src_id, edge.dst_id))
        out.append(
            Edge(
                src_id=edge.src_id,
                dst_id=edge.dst_id,
                kind="tests",
                path=src_path,
                resolution=edge.resolution,
                confidence=edge.confidence,
            )
        )
    return out


def _validated_bind(edge, dst_id, confidence):
    """The reference bind: a fully validated pydantic Edge."""
    from duckdb_code.models import Edge, resolution_of

    return Edge(
        src_id=edge.src_id,
        dst_id=dst_id,
        kind=edge.kind,
        path=edge.path,
        resolution=resolution_of(confidence),
        confidence=confidence,
    )


@pytest.mark.slow
@pytest.mark.parametrize("which", ["sample", "markdown", "generated"])
def test_resolution_and_test_edges_match_their_references(which, tmp_path, monkeypatch):
    import duckdb_code.indexer as indexer

    folder = {"sample": FIXTURE, "markdown": MARKDOWN_FIXTURE}.get(which) or (
        _generated_tree(tmp_path)
    )
    rows, nodes, edges, _languages, _skipped, _docs = indexer._extract_folder(
        folder, True, workers=1
    )
    repositories = indexer._discover_repositories(folder, rows)
    fast = indexer.resolve_edges(nodes, edges, repositories)
    monkeypatch.setattr(indexer, "_bind", _validated_bind)
    reference = indexer.resolve_edges(nodes, edges, repositories)
    assert fast == reference
    assert indexer.enrich_test_edges(nodes, fast) == _reference_test_edges(nodes, fast)
    if which == "generated":
        assert any(e.kind == "calls" for e in fast)
        assert indexer.enrich_test_edges(nodes, fast)


def test_index_build_restores_the_garbage_collector(tmp_path, monkeypatch):
    import gc

    import duckdb_code.indexer as indexer

    assert gc.isenabled()
    store = CodeStore(db_path=str(tmp_path / "gc.duckdb"), root=str(FIXTURE))
    index_folder(store, FIXTURE)
    assert gc.isenabled()

    def broken(*_args, **_kwargs):
        assert not gc.isenabled(), "the build runs with the collector paused"
        raise RuntimeError("boom")

    monkeypatch.setattr(indexer, "_full_index", broken)
    with pytest.raises(RuntimeError):
        index_folder(store, FIXTURE, rebuild=True)
    assert gc.isenabled()
    store.close()
