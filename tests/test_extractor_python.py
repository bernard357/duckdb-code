"""The Python extractor: node kinds, qualified names, ids, unresolved edges."""

from pathlib import Path

from duckdb_code.extractors.python import PythonExtractor

FIXTURE = Path(__file__).parent / "fixtures" / "sample"


def _extract(rel: str):
    text = (FIXTURE / rel).read_text(encoding="utf-8")
    return PythonExtractor().extract(text, rel)


def _raw_targets(extraction, kind):
    return sorted(e.dst_id.lstrip("?") for e in extraction.edges if e.kind == kind)


def test_node_kinds_and_qualified_names():
    extraction = _extract("payments/charge.py")
    by_kind: dict[str, list[str]] = {}
    for node in extraction.nodes:
        by_kind.setdefault(node.kind, []).append(node.qualified_name)

    assert by_kind["file"] == ["payments/charge.py"]
    assert by_kind["module"] == ["payments.charge"]
    assert set(by_kind["class"]) == {"payments.charge.Base", "payments.charge.Charge"}
    # Methods carry the class scope in the qualified name.
    assert "payments.charge.Charge.run" in by_kind["function"]
    assert "payments.charge.Base.run" in by_kind["function"]
    # The import node is addressable and does not collide with a real symbol.
    assert any("::import:" in qn for qn in by_kind.get("import", []))


def test_module_qualified_name_collapses_init():
    extraction = _extract("payments/__init__.py")
    module = next(n for n in extraction.nodes if n.kind == "module")
    assert module.qualified_name == "payments"


def test_defines_edges_are_exact_and_intra_file():
    extraction = _extract("payments/charge.py")
    defines = [e for e in extraction.edges if e.kind == "defines"]
    assert defines
    assert all(e.resolution == "exact" for e in defines)
    assert all(e.path == "payments/charge.py" for e in defines)


def test_node_ids_are_byte_identical_across_two_runs():
    first = _extract("payments/billing.py")
    second = _extract("payments/billing.py")
    assert [n.node_id for n in first.nodes] == [n.node_id for n in second.nodes]


def test_node_id_shape():
    extraction = _extract("payments/charge.py")
    charge = next(
        n for n in extraction.nodes if n.qualified_name == "payments.charge.Charge"
    )
    expected = f"class:payments.charge.Charge@payments/charge.py:{charge.start_line}"
    assert charge.node_id == expected


def test_unresolved_import_and_call_and_inherit_targets():
    billing = _extract("payments/billing.py")
    # from payments.charge import Charge -> raw target "payments.charge.Charge"
    assert "payments.charge.Charge" in _raw_targets(billing, "imports")
    # bill() calls Charge() and the local charge()
    calls = _raw_targets(billing, "calls")
    assert "Charge" in calls
    assert "charge" in calls

    premium = _extract("payments/premium.py")
    assert "Charge" in _raw_targets(premium, "inherits")


def test_attribute_tail_call_carries_tail():
    # c.run(amount) in billing.bill is an attribute-tail call carrying "run".
    billing = _extract("payments/billing.py")
    assert "run" in _raw_targets(billing, "calls")
