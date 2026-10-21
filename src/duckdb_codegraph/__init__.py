"""duckdb-codegraph: an embeddable code-graph building block.

Exposes a simplified interface over a local DuckDB database holding a
tree-sitter-extracted code graph, with reverse-dependency impact analysis and
structure explanation. One DuckDB file is one index.
"""

from duckdb_codegraph.config import Settings
from duckdb_codegraph.etag import compute_etag
from duckdb_codegraph.extractors.base import Extraction, Extractor
from duckdb_codegraph.extractors.javascript import JsTsExtractor
from duckdb_codegraph.extractors.python import PythonExtractor
from duckdb_codegraph.extractors.registry import (
    detect_language,
    register_extractor,
    resolve_extractor,
)
from duckdb_codegraph.indexer import IndexReport, index_folder
from duckdb_codegraph.models import (
    Edge,
    Explanation,
    ExplanationResult,
    ImpactNode,
    ImpactResult,
    Node,
    Report,
)
from duckdb_codegraph.store import CodeGraphStore, ReadOnlyError

__all__ = [
    "CodeGraphStore",
    "Edge",
    "Explanation",
    "ExplanationResult",
    "Extraction",
    "Extractor",
    "ImpactNode",
    "ImpactResult",
    "IndexReport",
    "JsTsExtractor",
    "Node",
    "PythonExtractor",
    "ReadOnlyError",
    "Report",
    "Settings",
    "compute_etag",
    "detect_language",
    "index_folder",
    "register_extractor",
    "resolve_extractor",
]
