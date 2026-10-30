"""The JS/TS extractor (best-effort): file/module/defines/imports."""

from pathlib import Path

from duckdb_code.extractors.javascript import JsTsExtractor

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


def _extract(language: str, rel: str):
    text = (FIXTURE / rel).read_text(encoding="utf-8")
    return JsTsExtractor(language).extract(text, rel)


def test_ts_emits_file_module_and_defines():
    extraction = _extract("typescript", "web/charge.ts")
    kinds = {n.kind for n in extraction.nodes}
    assert "file" in kinds
    assert "module" in kinds
    assert "class" in kinds

    module = next(n for n in extraction.nodes if n.kind == "module")
    # JS/TS module qualified name keeps slashes, drops the extension.
    assert module.qualified_name == "web/charge"

    charge = next(
        n for n in extraction.nodes if n.kind == "class" and n.name == "Charge"
    )
    assert charge.qualified_name == "web/charge.Charge"

    defines = [e for e in extraction.edges if e.kind == "defines"]
    assert defines
    assert all(e.resolution == "exact" for e in defines)


def test_ts_import_is_unresolved_and_best_effort():
    extraction = _extract("typescript", "web/billing.ts")
    imports = [e for e in extraction.edges if e.kind == "imports"]
    assert len(imports) == 1
    assert imports[0].resolution == "best_effort"
    # Raw specifier is carried for the indexer's specifier resolver.
    assert imports[0].dst_id.lstrip("?") == "./charge"


def test_js_arrow_lexical_declaration_defines_a_function():
    src = "export const add = (a, b) => a + b;\n"
    extraction = JsTsExtractor("javascript").extract(src, "util/add.js")
    functions = [n for n in extraction.nodes if n.kind == "function"]
    assert [n.name for n in functions] == ["add"]
    assert functions[0].qualified_name == "util/add.add"


def test_js_class_extends_emits_best_effort_inherits():
    src = "class Button extends Base {}\n"
    extraction = JsTsExtractor("javascript").extract(src, "ui/button.js")
    inherits = [e for e in extraction.edges if e.kind == "inherits"]
    assert len(inherits) == 1
    assert inherits[0].resolution == "best_effort"
    assert inherits[0].dst_id.lstrip("?") == "Base"
