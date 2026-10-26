"""duckdb-graph: an embeddable code-graph building block.

Exposes a simplified interface over a local DuckDB database holding a
tree-sitter-extracted code graph, with reverse-dependency impact analysis and
structure explanation. One DuckDB file is one index.
"""

from importlib.metadata import PackageNotFoundError, version

from duckdb_graph.config import Settings
from duckdb_graph.etag import compute_etag
from duckdb_graph.extractors.base import Extraction, Extractor
from duckdb_graph.extractors.javascript import JsTsExtractor
from duckdb_graph.extractors.python import PythonExtractor
from duckdb_graph.extractors.registry import (
    detect_language,
    register_extractor,
    resolve_extractor,
)
from duckdb_graph.indexer import IndexReport, index_folder
from duckdb_graph.mcp_server import build_server
from duckdb_graph.models import (
    Edge,
    Explanation,
    ExplanationResult,
    GraphExport,
    GraphExportEdge,
    GraphExportNode,
    ImpactNode,
    ImpactResult,
    Node,
    Report,
)
from duckdb_graph.store import GraphStore

__all__ = [
    "GraphStore",
    "Edge",
    "Explanation",
    "ExplanationResult",
    "Extraction",
    "Extractor",
    "GraphExport",
    "GraphExportEdge",
    "GraphExportNode",
    "ImpactNode",
    "ImpactResult",
    "IndexReport",
    "JsTsExtractor",
    "Node",
    "PythonExtractor",
    "Report",
    "Settings",
    "build_server",
    "compute_etag",
    "detect_language",
    "index_folder",
    "register_extractor",
    "resolve_extractor",
    "__version__",
]


def _resolve_version() -> str:
    """Return the installed distribution version, or a sentinel if absent.

    The version is baked in at build time by ``hatch-vcs`` from the most recent
    git tag (a tagged build reports that tag; an untagged working build reports
    a ``0.1.devN+g<commit>`` suffix). Reading the installed metadata — rather
    than shelling out to ``git`` — is what lets ``--version`` work from a
    pip/uvx install that has no git repository. Running from an uninstalled
    source tree raises :class:`PackageNotFoundError`, which degrades to a clear
    sentinel so ``--version`` never crashes.
    """
    try:
        return version("duckdb-graph")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _resolve_version()
