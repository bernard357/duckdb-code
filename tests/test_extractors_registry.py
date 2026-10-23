"""Extension detection and extractor resolution."""

import pytest

from duckdb_graph.extractors.registry import (
    EXTENSION_LANGUAGE,
    detect_language,
    register_extension,
    resolve_extractor,
)


def test_detect_language_known_extensions():
    assert detect_language("a/b/c.py") == "python"
    assert detect_language("a/b/c.pyi") == "python"
    assert detect_language("a/b/c.js") == "javascript"
    assert detect_language("a/b/c.jsx") == "javascript"
    assert detect_language("a/b/c.mjs") == "javascript"
    assert detect_language("a/b/c.cjs") == "javascript"
    assert detect_language("a/b/c.ts") == "typescript"
    assert detect_language("a/b/c.tsx") == "typescript"


def test_detect_language_is_case_insensitive():
    assert detect_language("A/B/C.PY") == "python"


def test_detect_language_unknown_returns_none():
    assert detect_language("README.md") is None
    assert detect_language("noextension") is None


def test_resolve_extractor_unknown_raises_listing_names():
    with pytest.raises(ValueError) as excinfo:
        resolve_extractor("cobol")
    message = str(excinfo.value)
    assert "cobol" in message
    assert "python" in message
    assert "javascript" in message
    assert "typescript" in message


def test_register_extension_normalizes(monkeypatch):
    monkeypatch.setitem(EXTENSION_LANGUAGE, ".pyx", "python")
    register_extension("PYX", "python")
    assert EXTENSION_LANGUAGE[".pyx"] == "python"
    assert detect_language("mod.PYX") == "python"
