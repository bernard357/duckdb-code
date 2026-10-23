"""Resolve a language name to an :class:`Extractor` instance.

The registry is keyed by *language* (``"python"``, ``"javascript"``,
``"typescript"``); the indexer detects a file's language from its extension via
:func:`detect_language` and resolves the matching extractor. Factories defer the
heavy ``tree_sitter_language_pack`` import (and the extractor modules
themselves) to call time, so an unused grammar is never loaded and the registry
module imports cleanly even before those extractor modules land.

Contributors add a language by registering an extractor factory and, optionally,
new extensions via :func:`register_extension`, keeping the core free of heavy
dependencies.
"""

from collections.abc import Callable
from pathlib import Path

from duckdb_graph.extractors.base import Extractor

ExtractorFactory = Callable[[], Extractor]

_REGISTRY: dict[str, ExtractorFactory] = {}

EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".md": "markdown",
    ".markdown": "markdown",
}


def register_extractor(name: str, factory: ExtractorFactory) -> None:
    """Register an extractor factory under a language ``name``."""
    _REGISTRY[name] = factory


def resolve_extractor(name: str) -> Extractor:
    """Resolve a language name to an extractor instance.

    Raises ``ValueError`` for an unknown language, listing the registered names
    to aid discovery.
    """
    factory = _REGISTRY.get(name)
    if factory is None:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(f"Unknown language '{name}'. Registered extractors: {known}.")
    return factory()


def register_extension(ext: str, language: str) -> None:
    """Map a file extension to a language name, normalizing the extension.

    The extension is lowercased and given a leading dot if missing, so
    ``register_extension("PY", "python")`` and ``register_extension(".py",
    "python")`` are equivalent.
    """
    normalized = ext.lower()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    EXTENSION_LANGUAGE[normalized] = language


def detect_language(path: str) -> str | None:
    """Return the language for a path's extension, or ``None`` if unknown.

    A ``None`` result is the "no registered extractor" signal: the indexer
    counts the file as skipped and never errors.
    """
    return EXTENSION_LANGUAGE.get(Path(path).suffix.lower())


def _python_factory() -> Extractor:
    from duckdb_graph.extractors.python import PythonExtractor

    return PythonExtractor()


def _js_factory() -> Extractor:
    from duckdb_graph.extractors.javascript import JsTsExtractor

    return JsTsExtractor("javascript")


def _ts_factory() -> Extractor:
    from duckdb_graph.extractors.javascript import JsTsExtractor

    return JsTsExtractor("typescript")


def _markdown_factory() -> Extractor:
    from duckdb_graph.extractors.markdown import MarkdownExtractor

    return MarkdownExtractor()


register_extractor("python", _python_factory)
register_extractor("javascript", _js_factory)
register_extractor("typescript", _ts_factory)
register_extractor("markdown", _markdown_factory)
