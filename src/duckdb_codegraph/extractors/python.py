"""The Python extractor.

FEAT-001 ships the minimal shape the registry factory needs; FEAT-002 fills in
the tree-sitter parse that produces the file's nodes and unresolved edges.
"""

from duckdb_codegraph.extractors.base import Extraction


class PythonExtractor:
    """Extract graph nodes and edges from a single Python source file."""

    @property
    def language(self) -> str:
        return "python"

    def extract(self, text: str, path: str) -> Extraction:
        raise NotImplementedError("PythonExtractor.extract lands in FEAT-002")
