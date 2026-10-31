"""Shared pytest configuration.

Force the dependency-free ``hashing`` embedder for the whole suite so indexing
never attempts a fastembed model download in CI or offline. Production defaults
to ``fastembed`` (see ``Settings``); the semantic quality of the real backend
is not what the suite asserts — only that the vectors table is populated with
stable, correctly shaped vectors.
"""

import os

os.environ.setdefault("DUCKDB_CODE_EMBEDDER", "hashing")
