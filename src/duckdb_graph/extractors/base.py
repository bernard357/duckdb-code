"""The extractor protocol every language extractor implements."""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from duckdb_graph.models import Edge, Node


class Extraction(BaseModel):
    """The nodes and edges a single extractor produced for one file.

    An extractor sees only its own file in isolation: ``edges`` for
    ``imports``/``inherits``/``calls``/``references`` are unresolved (their
    ``dst_id`` is a placeholder carrying the raw target string) until the
    indexer's resolution pass binds them against the cross-file symbol table.
    """

    nodes: list[Node]
    edges: list[Edge]


@runtime_checkable
class Extractor(Protocol):
    """Extract graph nodes and edges from one source file.

    ``path`` is the stored, folder-relative source path (POSIX separators). An
    implementation parses ``text`` with its grammar and returns the file's
    nodes plus its intra-file ``defines`` edges and unresolved cross-file edges.
    """

    @property
    def language(self) -> str:
        """The detected language name this extractor handles."""
        ...

    def extract(self, text: str, path: str) -> Extraction:
        """Return the nodes and edges for one file."""
        ...
