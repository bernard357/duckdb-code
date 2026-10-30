"""Extension detection and extractor resolution."""

import pytest

from duckdb_code.extractors.registry import (
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
    assert detect_language("a/b/c.md") == "markdown"
    assert detect_language("a/b/c.markdown") == "markdown"


def test_detect_language_is_case_insensitive():
    assert detect_language("A/B/C.PY") == "python"


def test_detect_language_unknown_returns_none():
    # README.md now routes to the Markdown extractor.
    assert detect_language("README.md") == "markdown"
    assert detect_language("noextension") is None


def test_resolve_markdown_extractor():
    extractor = resolve_extractor("markdown")
    assert extractor.language == "markdown"


def test_resolve_extractor_unknown_raises_listing_names():
    with pytest.raises(ValueError) as excinfo:
        resolve_extractor("cobol")
    message = str(excinfo.value)
    assert "cobol" in message
    assert "python" in message
    assert "javascript" in message
    assert "typescript" in message
    assert "markdown" in message


def test_register_extension_normalizes(monkeypatch):
    monkeypatch.setitem(EXTENSION_LANGUAGE, ".pyx", "python")
    register_extension("PYX", "python")
    assert EXTENSION_LANGUAGE[".pyx"] == "python"
    assert detect_language("mod.PYX") == "python"
