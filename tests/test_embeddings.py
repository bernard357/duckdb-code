"""Index-time embedding population (issue #26 / #31 data source).

Uses the dependency-free ``hashing`` backend (forced by conftest) so the test
needs no model download. Asserts the vectors table is populated with
correctly-shaped, stable vectors for the searchable symbols, and that the
embedder registry resolves and rejects as expected.
"""

from pathlib import Path

import pytest

from duckdb_code.embedders import resolve_embedder
from duckdb_code.embedders.hashing import HashingEmbedder
from duckdb_code.indexer import _EMBEDDABLE_KINDS, _embedding_text, index_folder
from duckdb_code.store import CodeStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


def _indexed(tmp_path) -> CodeStore:
    store = CodeStore(
        db_path=str(tmp_path / "code.duckdb"),
        root=str(FIXTURE),
        embedder="hashing",
    )
    index_folder(store, FIXTURE)
    return store


def test_vectors_table_populated_for_searchable_symbols(tmp_path):
    store = _indexed(tmp_path)
    con = store._con
    placeholders = ", ".join(["?"] * len(_EMBEDDABLE_KINDS))
    n_symbols = con.execute(
        f"SELECT COUNT(*) FROM nodes WHERE kind IN ({placeholders})",
        list(_EMBEDDABLE_KINDS),
    ).fetchone()[0]
    n_vectors = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    assert n_symbols > 0
    assert n_vectors == n_symbols  # one vector per searchable symbol
    # Every vector references a real node and has the backend's dimensions.
    dims = int(
        dict(con.execute("SELECT key, value FROM meta").fetchall())[
            "embedding_dimensions"
        ]
    )
    lengths = {
        len(row[0]) for row in con.execute("SELECT embedding FROM vectors").fetchall()
    }
    assert lengths == {dims}
    store.close()


def test_meta_records_backend(tmp_path):
    store = _indexed(tmp_path)
    meta = dict(store._con.execute("SELECT key, value FROM meta").fetchall())
    assert meta["embedder"].startswith("hashing:")
    store.close()


def test_embedding_text_splits_dotted_names():
    text = _embedding_text("function", "Charge", "payments.charge.Charge")
    assert "payments" in text.split()
    assert "charge" in text.split()
    assert text.startswith("function Charge")


def test_resolver_returns_backends_and_rejects_unknown():
    assert isinstance(resolve_embedder("hashing"), HashingEmbedder)
    assert resolve_embedder("hashing:64").dimensions == 64
    with pytest.raises(ValueError, match="Unknown embedding backend"):
        resolve_embedder("nope")


def test_hashing_is_deterministic_and_normalized():
    embedder = HashingEmbedder(dimensions=32)
    a, b = embedder.embed(["def charge payment", "def charge payment"])
    assert a == b  # deterministic
    norm = sum(v * v for v in a) ** 0.5
    assert abs(norm - 1.0) < 1e-9  # L2-normalized


def test_search_ranks_symbols_by_relevance(tmp_path):
    store = _indexed(tmp_path)
    result = store.search("charge payment", limit=5)
    assert result.query == "charge payment"
    assert result.results  # the fixture has payment/charge symbols
    assert len(result.results) <= 5
    # Scores are descending (ranked most-relevant first).
    scores = [hit.score for hit in result.results]
    assert scores == sorted(scores, reverse=True)
    # Every hit carries a real node with path+range (the result contract).
    top = result.results[0]
    assert top.node.path
    assert top.node.start_line >= 1
    store.close()


def test_search_empty_query_and_zero_limit_return_empty(tmp_path):
    store = _indexed(tmp_path)
    assert store.search("", limit=5).results == []
    assert store.search("charge", limit=0).results == []
    store.close()


def test_search_without_embeddings_returns_empty(tmp_path):
    # An index whose vectors table is empty (e.g. embedding backend failed)
    # still answers, with nothing to rank, rather than raising.
    store = _indexed(tmp_path)
    store._con.execute("DELETE FROM vectors")
    assert store.search("charge", limit=5).results == []
    store.close()
