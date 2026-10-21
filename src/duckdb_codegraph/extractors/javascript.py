"""The JavaScript/TypeScript extractor (best-effort).

FEAT-001 ships the minimal shape the registry factories need; FEAT-002 fills in
the tree-sitter parse that produces the file's nodes and unresolved edges. JS
and TS share one extractor, selecting the grammar by language name.
"""

from duckdb_codegraph.extractors.base import Extraction


class JsTsExtractor:
    """Extract graph nodes and edges from a single JS/TS source file."""

    def __init__(self, language: str) -> None:
        self._language = language

    @property
    def language(self) -> str:
        return self._language

    def extract(self, text: str, path: str) -> Extraction:
        raise NotImplementedError("JsTsExtractor.extract lands in FEAT-002")
