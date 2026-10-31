"""duckdb-code: an embeddable code-intelligence building block.

Exposes a simplified interface over a local DuckDB database holding a
tree-sitter-extracted code graph, with reverse-dependency impact analysis and
structure explanation. One DuckDB file is one index.
"""

from importlib.metadata import PackageNotFoundError, version

from duckdb_code.config import Settings
from duckdb_code.etag import compute_etag
from duckdb_code.extractors.base import Extraction, Extractor
from duckdb_code.extractors.javascript import JsTsExtractor
from duckdb_code.extractors.python import PythonExtractor
from duckdb_code.extractors.registry import (
    detect_language,
    register_extractor,
    resolve_extractor,
)
from duckdb_code.indexer import IndexReport, index_folder
from duckdb_code.mcp_server import build_server
from duckdb_code.models import (
    CodeExport,
    CodeExportEdge,
    CodeExportNode,
    Edge,
    Explanation,
    ExplanationResult,
    ImpactNode,
    ImpactResult,
    Node,
    Report,
    SearchHit,
    SearchResult,
)
from duckdb_code.store import CodeStore

__all__ = [
    "CodeExport",
    "CodeExportEdge",
    "CodeExportNode",
    "CodeStore",
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
    "Report",
    "SearchHit",
    "SearchResult",
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
        return version("duckdb-code")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _resolve_version()
