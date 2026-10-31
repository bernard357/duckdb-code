"""Pluggable text-embedding backends for semantic code search.

The default production backend is ``fastembed`` (a core dependency, local ONNX,
no torch); ``hashing`` is a deterministic, dependency-free fallback for tests
and offline use only. Backends are resolved by a spec string via
:func:`resolve_embedder`.
"""

from duckdb_code.embedders.base import Embedder
from duckdb_code.embedders.registry import register_embedder, resolve_embedder

__all__ = ["Embedder", "register_embedder", "resolve_embedder"]
