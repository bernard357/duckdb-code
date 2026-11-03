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
    # c.run(amount) in billing.bill: `c = Charge(...)` types the receiver.
    billing = _extract("payments/billing.py")
    assert "type::i::Charge::run" in _raw_targets(billing, "calls")


_SCOPED_SOURCE = """\
class Walker:
    def walk(self, items):
        links = [item for item in items]
        total = 0
        for first, second in items:
            total += 1
        with open("x") as handle:
            pass
        self.visit(links)
        self.helper.links()
        return links

    def visit(self, value):
        return self.walk(value)


def links():
    return 1
"""


def _raw_from(extraction, src_name, kind):
    src_ids = {n.node_id for n in extraction.nodes if n.name == src_name}
    return sorted(
        e.dst_id.lstrip("?")
        for e in extraction.edges
        if e.kind == kind and e.src_id in src_ids
    )


def test_function_local_names_are_bound_not_references():
    extraction = PythonExtractor().extract(_SCOPED_SOURCE, "m.py")
    references = _raw_from(extraction, "walk", "references")
    # Locals assigned anywhere in the function (assignment, augmented
    # assignment, for targets, with-as) never reference a module symbol.
    for local in ("links", "total", "first", "second", "handle"):
        assert local not in references


def test_self_calls_carry_their_enclosing_class():
    extraction = PythonExtractor().extract(_SCOPED_SOURCE, "m.py")
    assert "self::m.Walker::visit" in _raw_from(extraction, "walk", "calls")
    assert "self::m.Walker::walk" in _raw_from(extraction, "visit", "calls")


def test_attribute_tail_is_kept_even_when_a_local_shares_its_name():
    extraction = PythonExtractor().extract(_SCOPED_SOURCE, "m.py")
    # self.helper.links() calls a `links` attribute, not the local `links`.
    assert "attr::links" in _raw_from(extraction, "walk", "calls")


def test_module_level_function_has_no_self_receiver():
    source = "def run(self):\n    return self.go()\n"
    extraction = PythonExtractor().extract(source, "m.py")
    assert _raw_from(extraction, "run", "calls") == ["attr::go"]


def test_relative_imports_keep_their_level():
    source = (
        "from . import sibling\nfrom .config import Settings\nfrom .. import parent\n"
    )
    extraction = PythonExtractor().extract(source, "pkg/sub/mod.py")
    # One leading dot per level: `.` is the package, `..` its parent.
    assert _raw_targets(extraction, "imports") == [
        "..parent",
        ".config.Settings",
        ".sibling",
    ]


_NESTED_SOURCE = '''\
def _described(server):
    def register(fn):
        return fn
    return register


def build_server(server, app):
    """Build the server and register its tools."""

    @_described(server)
    def search(query):
        """Rank symbols for a query."""
        return lookup(query)

    @app.get("/health")
    def health():
        return "ok"

    return server
'''


def _node_named(extraction, qualified_name):
    return next(n for n in extraction.nodes if n.qualified_name == qualified_name)


def test_decorated_function_nested_in_a_function_is_a_node():
    extraction = PythonExtractor().extract(_NESTED_SOURCE, "m.py")
    inner = _node_named(extraction, "m.build_server.search")
    outer = _node_named(extraction, "m.build_server")
    assert inner.kind == "function"
    defines = {(e.src_id, e.dst_id) for e in extraction.edges if e.kind == "defines"}
    assert (outer.node_id, inner.node_id) in defines
    # The nested tool owns its calls; the enclosing function no longer does.
    assert _raw_from(extraction, "search", "calls") == ["lookup"]
    assert "lookup" not in _raw_from(extraction, "build_server", "calls")


def test_route_decorator_on_a_nested_function_emits_a_route():
    extraction = PythonExtractor().extract(_NESTED_SOURCE, "m.py")
    routes = [n.qualified_name for n in extraction.nodes if n.kind == "route"]
    assert routes == ["GET /health"]


def test_bare_name_decorator_is_used_by_the_enclosing_scope():
    extraction = PythonExtractor().extract(_NESTED_SOURCE, "m.py")
    assert "_described" in _raw_from(extraction, "build_server", "calls")
    # An attribute decorator (`@app.get`) binds by its tail name: not emitted.
    assert "get" not in _raw_from(extraction, "build_server", "calls")


def test_docstrings_are_captured_collapsed_and_capped():
    long_doc = "word " * 200
    source = (
        "class Store:\n"
        '    """Keep items.\n\n    Second   paragraph\n    wraps."""\n\n'
        "    def get(self):\n"
        f'        """{long_doc}"""\n'
        "        return 1\n"
    )
    extraction = PythonExtractor().extract(source, "m.py")
    docs = {
        n.qualified_name: extraction.docstrings[n.node_id]
        for n in extraction.nodes
        if n.node_id in extraction.docstrings
    }
    assert docs["m.Store"] == "Keep items. Second paragraph wraps."
    assert len(docs["m.Store.get"]) == 300
    assert docs["m.Store.get"].startswith("word word")


_TYPED_SOURCE = """\
import json
from pkg.store import CodeStore


def run(path, typed: CodeStore, maybe: CodeStore | None = None):
    store = CodeStore(path)
    store.close()
    typed.index(path)
    maybe.report()
    json.dumps(path)
    CodeStore.default()
    mixed = CodeStore(path)
    mixed = make(path)
    mixed.close()
    self_like = path.parent
    self_like.exists()
    report(url=build(path), match="x")
    return helper.cache_clear()
"""


def test_keyword_argument_names_are_not_references():
    extraction = PythonExtractor().extract(_TYPED_SOURCE, "m.py")
    references = _raw_from(extraction, "run", "references")
    assert "url" not in references and "match" not in references
    assert {"report", "build"} <= set(_raw_from(extraction, "run", "calls"))


def test_typed_receivers_carry_their_type():
    calls = set(
        _raw_from(PythonExtractor().extract(_TYPED_SOURCE, "m.py"), "run", "calls")
    )
    # A local assigned only from CodeStore(...), and annotated parameters,
    # name CodeStore, which the file imports (flag i).
    assert "type::i::CodeStore::close" in calls
    assert "type::i::CodeStore::index" in calls
    assert "type::i::CodeStore::report" in calls
    # An imported module and an imported class used directly.
    assert "type::i::json::dumps" in calls
    assert "type::i::CodeStore::default" in calls
    # A module-level name the file does not import (flag n).
    assert "type::n::helper::cache_clear" in calls


def test_untrusted_receivers_stay_attribute_guesses():
    calls = set(
        _raw_from(PythonExtractor().extract(_TYPED_SOURCE, "m.py"), "run", "calls")
    )
    # `mixed` is assigned from CodeStore(...) and from make(...): not inferred.
    assert "attr::close" in calls
    # `self_like` is assigned from an attribute load, not a constructor.
    assert "attr::exists" in calls
