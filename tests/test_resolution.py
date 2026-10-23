"""The resolution pass as a pure function (no DuckDB)."""

from duckdb_graph.extractors.python import UNRESOLVED
from duckdb_graph.indexer import _resolve_js_specifier, resolve_edges
from duckdb_graph.models import Edge, Node


def _node(kind, qn, path, line=1, language="python"):
    return Node(
        node_id=f"{kind}:{qn}@{path}:{line}",
        kind=kind,
        name=qn.split(".")[-1].split("/")[-1],
        qualified_name=qn,
        path=path,
        start_line=line,
        end_line=line,
        language=language,
    )


def _unresolved(src_id, raw, kind, path):
    return Edge(
        src_id=src_id,
        dst_id=f"{UNRESOLVED}{raw}",
        kind=kind,
        path=path,
        resolution="best_effort",
    )


def _edges_of_kind(edges, kind):
    return [e for e in edges if e.kind == kind]


def test_imported_then_bare_called_name_binds():
    # charge.py defines Charge; billing.py imports it and bare-calls Charge().
    charge_mod = _node("module", "payments.charge", "payments/charge.py")
    charge_cls = _node("class", "payments.charge.Charge", "payments/charge.py", 2)
    billing_mod = _node("module", "payments.billing", "payments/billing.py")
    billing_fn = _node("function", "payments.billing.bill", "payments/billing.py", 2)
    imp = _node(
        "import",
        "payments.billing::import:payments.charge.Charge",
        "payments/billing.py",
    )

    nodes = [charge_mod, charge_cls, billing_mod, billing_fn, imp]
    edges = [
        _unresolved(
            imp.node_id, "payments.charge.Charge", "imports", "payments/billing.py"
        ),
        _unresolved(billing_fn.node_id, "Charge", "calls", "payments/billing.py"),
    ]

    resolved = resolve_edges(nodes, edges)
    calls = _edges_of_kind(resolved, "calls")
    assert len(calls) == 1
    # Phase A bound Charge into by_local, so the bare call resolves exact.
    assert calls[0].dst_id == charge_cls.node_id
    assert calls[0].resolution == "exact"


def test_locally_defined_bare_called_name_binds_via_seed_half():
    mod = _node("module", "m", "m.py")
    helper = _node("function", "m.helper", "m.py", 2)
    caller = _node("function", "m.caller", "m.py", 5)
    nodes = [mod, helper, caller]
    edges = [_unresolved(caller.node_id, "helper", "calls", "m.py")]

    resolved = resolve_edges(nodes, edges)
    calls = _edges_of_kind(resolved, "calls")
    assert len(calls) == 1
    assert calls[0].dst_id == helper.node_id
    assert calls[0].resolution == "exact"


def test_seed_half_is_a_pure_string_test_excluding_methods_and_nested():
    # A method and a nested function have an intervening scope segment, so they
    # are NOT module-local defines; a bare call to them from the module does not
    # resolve exact via by_local (it falls back to a best_effort suffix match).
    mod = _node("module", "m", "m.py")
    cls = _node("class", "m.C", "m.py", 2)
    method = _node("function", "m.C.run", "m.py", 3)
    toplevel = _node("function", "m.run", "m.py", 8)  # same short name "run"
    caller = _node("function", "m.caller", "m.py", 12)
    nodes = [mod, cls, method, toplevel, caller]
    edges = [_unresolved(caller.node_id, "run", "calls", "m.py")]

    resolved = resolve_edges(nodes, edges)
    calls = _edges_of_kind(resolved, "calls")
    # The module-top-level `run` is in the seed half and is a single by_local
    # hit -> exact to it, not to the method.
    assert len(calls) == 1
    assert calls[0].dst_id == toplevel.node_id
    assert calls[0].resolution == "exact"


def test_attribute_tail_suffix_match_is_best_effort_not_exact():
    # fix 5: c.run() in bill resolves the tail `run` by a by_qn suffix match. A
    # same-named method on an unrelated class must yield best_effort edges.
    mod = _node("module", "m", "m.py")
    caller = _node("function", "m.bill", "m.py", 2)
    unrelated_cls = _node("class", "other.Thing", "other.py", 1, "python")
    unrelated_method = _node("function", "other.Thing.run", "other.py", 2)
    nodes = [mod, caller, unrelated_cls, unrelated_method]
    # `run` is NOT a module-local define in m, so by_local misses and the suffix
    # match applies.
    edges = [_unresolved(caller.node_id, "run", "calls", "m.py")]

    resolved = resolve_edges(nodes, edges)
    calls = _edges_of_kind(resolved, "calls")
    assert calls
    assert all(e.resolution == "best_effort" for e in calls)
    assert any(e.dst_id == unrelated_method.node_id for e in calls)


def test_zero_in_set_target_is_dropped_not_fabricated():
    mod = _node("module", "m", "m.py")
    caller = _node("function", "m.caller", "m.py", 2)
    nodes = [mod, caller]
    edges = [_unresolved(caller.node_id, "nonexistent", "calls", "m.py")]
    resolved = resolve_edges(nodes, edges)
    assert _edges_of_kind(resolved, "calls") == []


def test_defines_edges_pass_through_unchanged():
    mod = _node("module", "m", "m.py")
    fn = _node("function", "m.f", "m.py", 2)
    defines = Edge(
        src_id=mod.node_id,
        dst_id=fn.node_id,
        kind="defines",
        path="m.py",
        resolution="exact",
    )
    resolved = resolve_edges([mod, fn], [defines])
    assert resolved == [defines]


def test_phase_b_does_not_mutate_by_local():
    # Resolving a bare reference to a locally-defined name twice from two callers
    # must not grow by_local (no duplicate exact bindings leaking in).
    mod = _node("module", "m", "m.py")
    target = _node("function", "m.target", "m.py", 2)
    a = _node("function", "m.a", "m.py", 5)
    b = _node("function", "m.b", "m.py", 8)
    nodes = [mod, target, a, b]
    edges = [
        _unresolved(a.node_id, "target", "references", "m.py"),
        _unresolved(b.node_id, "target", "references", "m.py"),
    ]
    resolved = resolve_edges(nodes, edges)
    refs = _edges_of_kind(resolved, "references")
    # Each caller yields exactly one exact edge to the single target.
    assert len(refs) == 2
    assert all(e.dst_id == target.node_id for e in refs)
    assert all(e.resolution == "exact" for e in refs)


def test_inherits_resolves_via_by_local_exact():
    mod = _node("module", "m", "m.py")
    base = _node("class", "m.Base", "m.py", 2)
    sub = _node("class", "m.Sub", "m.py", 6)
    nodes = [mod, base, sub]
    edges = [_unresolved(sub.node_id, "Base", "inherits", "m.py")]
    resolved = resolve_edges(nodes, edges)
    inherits = _edges_of_kind(resolved, "inherits")
    assert len(inherits) == 1
    assert inherits[0].dst_id == base.node_id
    assert inherits[0].resolution == "exact"


def test_js_ts_edges_are_always_best_effort():
    mod = _node("module", "web/billing", "web/billing.ts", language="typescript")
    fn = _node(
        "function", "web/billing.bill", "web/billing.ts", 2, language="typescript"
    )
    target = _node(
        "function", "web/billing.helper", "web/billing.ts", 6, language="typescript"
    )
    nodes = [mod, fn, target]
    edges = [_unresolved(fn.node_id, "helper", "calls", "web/billing.ts")]
    resolved = resolve_edges(nodes, edges)
    calls = _edges_of_kind(resolved, "calls")
    assert len(calls) == 1
    assert calls[0].dst_id == target.node_id
    # A single by_local hit, but JS/TS is always best_effort.
    assert calls[0].resolution == "best_effort"


def _mod(qn, path):
    return _node("module", qn, path, language="typescript")


def test_js_specifier_resolver_fixed_order():
    modules = {
        "web/charge": "module:web/charge@web/charge.ts:1",
        "web/util/index": "module:web/util/index@web/util/index.ts:1",
    }
    # ./charge from web/billing.ts resolves to web/charge via the appended-ext.
    hit = _resolve_js_specifier("./charge", "web/billing.ts", modules)
    assert hit == "module:web/charge@web/charge.ts:1"

    # ../util resolves to web/util/index via the index fallback.
    hit = _resolve_js_specifier("./util", "web/billing.ts", modules)
    assert hit == "module:web/util/index@web/util/index.ts:1"

    # A specifier with a known extension resolves by exact (extension stripped).
    hit = _resolve_js_specifier("./charge.ts", "web/billing.ts", modules)
    assert hit == "module:web/charge@web/charge.ts:1"


def test_js_specifier_bare_never_resolves():
    modules = {"web/charge": "module:web/charge@web/charge.ts:1"}
    assert _resolve_js_specifier("react", "web/billing.ts", modules) is None
    assert _resolve_js_specifier("lodash/map", "web/billing.ts", modules) is None


def _page(path, stem):
    # Mirror the real MarkdownExtractor: a page node's qualified_name is the
    # stored POSIX path and its `name` is the file STEM (no extension), the
    # field the indexer derives the title/slug from.
    return Node(
        node_id=f"page:{path}@{path}:1",
        kind="page",
        name=stem,
        qualified_name=path,
        path=path,
        start_line=1,
        end_line=1,
        language="markdown",
    )


def test_markdown_relative_path_links_to_binds_exact():
    # index.md links to guide.md by relative path; the edge binds to the guide
    # page node, resolution exact.
    index = _page("index.md", "index")
    guide = _page("guide.md", "guide")
    nodes = [index, guide]
    edges = [_unresolved(index.node_id, "guide.md", "links_to", "index.md")]

    resolved = resolve_edges(nodes, edges)
    links = _edges_of_kind(resolved, "links_to")
    assert len(links) == 1
    assert links[0].dst_id == guide.node_id
    assert links[0].resolution == "exact"


def test_markdown_relative_path_normalizes_dotdot():
    # docs/guide.md links to ../index.md -> index.md at the tree root.
    index = _page("index.md", "index")
    guide = _page("docs/guide.md", "guide")
    nodes = [index, guide]
    edges = [_unresolved(guide.node_id, "../index.md", "links_to", "docs/guide.md")]

    resolved = resolve_edges(nodes, edges)
    links = _edges_of_kind(resolved, "links_to")
    assert len(links) == 1
    assert links[0].dst_id == index.node_id
    assert links[0].resolution == "exact"


def test_markdown_wikilink_title_binds_by_slug_exact_on_single_page():
    # A wikilink [[Guide]] (raw target "Guide") binds to the page whose stem
    # slug matches, exact on a single page.
    index = _page("index.md", "index")
    guide = _page("guide.md", "guide")
    nodes = [index, guide]
    edges = [_unresolved(index.node_id, "Guide", "links_to", "index.md")]

    resolved = resolve_edges(nodes, edges)
    links = _edges_of_kind(resolved, "links_to")
    assert len(links) == 1
    assert links[0].dst_id == guide.node_id
    assert links[0].resolution == "exact"


def test_markdown_wikilink_best_effort_when_two_pages_share_a_title():
    # Two pages in different folders share the stem "guide"; a wikilink binds to
    # both, best_effort.
    index = _page("index.md", "index")
    guide_a = _page("a/guide.md", "guide")
    guide_b = _page("b/guide.md", "guide")
    nodes = [index, guide_a, guide_b]
    edges = [_unresolved(index.node_id, "Guide", "links_to", "index.md")]

    resolved = resolve_edges(nodes, edges)
    links = _edges_of_kind(resolved, "links_to")
    assert len(links) == 2
    assert {e.dst_id for e in links} == {guide_a.node_id, guide_b.node_id}
    assert all(e.resolution == "best_effort" for e in links)


def test_markdown_external_url_target_is_dropped():
    index = _page("index.md", "index")
    nodes = [index]
    edges = [
        _unresolved(index.node_id, "https://example.com", "links_to", "index.md"),
        _unresolved(index.node_id, "mailto:a@b.co", "links_to", "index.md"),
        _unresolved(index.node_id, "#same-page", "links_to", "index.md"),
    ]
    resolved = resolve_edges(nodes, edges)
    assert _edges_of_kind(resolved, "links_to") == []


def test_markdown_missing_page_path_target_is_dropped():
    index = _page("index.md", "index")
    nodes = [index]
    edges = [_unresolved(index.node_id, "missing.md", "links_to", "index.md")]
    resolved = resolve_edges(nodes, edges)
    assert _edges_of_kind(resolved, "links_to") == []


def test_markdown_embed_binds_to_a_page_and_dangling_is_dropped():
    index = _page("index.md", "index")
    other = _page("other.md", "other")
    nodes = [index, other]
    edges = [
        _unresolved(index.node_id, "other.md", "embeds", "index.md"),
        _unresolved(index.node_id, "assets/arch.png", "embeds", "index.md"),
    ]
    resolved = resolve_edges(nodes, edges)
    embeds = _edges_of_kind(resolved, "embeds")
    assert len(embeds) == 1
    assert embeds[0].dst_id == other.node_id
    assert embeds[0].resolution == "exact"


def _tag(name):
    # Mirror the real MarkdownExtractor's corpus-global tag node: a
    # path-independent id `tag:{name}`, bare-name qualified_name, synthetic
    # empty path / line 0.
    return Node(
        node_id=f"tag:{name}",
        kind="tag",
        name=name,
        qualified_name=name,
        path="",
        start_line=0,
        end_line=0,
        language="markdown",
    )


def test_markdown_tagged_binds_to_its_tag_node_exact():
    index = _page("index.md", "index")
    tag = _tag("architecture")
    nodes = [index, tag]
    edges = [_unresolved(index.node_id, "architecture", "tagged", "index.md")]
    resolved = resolve_edges(nodes, edges)
    tagged = _edges_of_kind(resolved, "tagged")
    assert len(tagged) == 1
    assert tagged[0].dst_id == tag.node_id
    assert tagged[0].resolution == "exact"


def test_markdown_unknown_tag_target_is_dropped():
    index = _page("index.md", "index")
    nodes = [index]
    edges = [_unresolved(index.node_id, "missing", "tagged", "index.md")]
    resolved = resolve_edges(nodes, edges)
    assert _edges_of_kind(resolved, "tagged") == []


def test_markdown_tagged_never_binds_to_a_code_node():
    # A code node whose name coincidentally equals a tag name must not capture a
    # tagged edge (domain isolation): with no tag node present, it is dropped.
    index = _page("index.md", "index")
    code_fn = _node("function", "architecture", "m.py", 2, language="python")
    nodes = [index, code_fn]
    edges = [_unresolved(index.node_id, "architecture", "tagged", "index.md")]
    resolved = resolve_edges(nodes, edges)
    assert _edges_of_kind(resolved, "tagged") == []


def test_markdown_link_never_binds_to_a_code_node():
    # A code function whose qualified_name coincidentally equals the link raw
    # target must NOT capture a markdown links_to edge (domain isolation).
    index = _page("guide.md", "guide")
    code_mod = _node("module", "guide", "guide.py", language="python")
    code_fn = _node("function", "guide.md", "guide.py", 2, language="python")
    nodes = [index, code_mod, code_fn]
    # No page named "guide.md" other than the source itself; the raw target
    # "guide.md" is a path that matches no OTHER page, so the edge is dropped —
    # it must never bind to code_fn even though its qualified_name is "guide.md".
    edges = [_unresolved(index.node_id, "guide.md", "links_to", "guide.md")]

    resolved = resolve_edges(nodes, edges)
    links = _edges_of_kind(resolved, "links_to")
    # Binds to the page itself (same stored path) — a page node, never the code
    # function — and in this corpus the only page at guide.md is the source.
    assert all(node_kind_of(nodes, e.dst_id) == "page" for e in links)
    assert code_fn.node_id not in {e.dst_id for e in links}


def test_code_edge_never_binds_to_a_page_node():
    # A python call whose bare name equals a page stem must resolve only through
    # the code symbol tables, never to the page node.
    page = _page("helper.md", "helper")
    mod = _node("module", "m", "m.py", language="python")
    caller = _node("function", "m.caller", "m.py", 2, language="python")
    nodes = [page, mod, caller]
    edges = [_unresolved(caller.node_id, "helper", "calls", "m.py")]
    resolved = resolve_edges(nodes, edges)
    calls = _edges_of_kind(resolved, "calls")
    # No code "helper" exists, so the call is dropped — never captured by the
    # page node whose stem is "helper".
    assert calls == []


def node_kind_of(nodes, node_id):
    for n in nodes:
        if n.node_id == node_id:
            return n.kind
    return None
