"""The Markdown extractor: page/heading/tag nodes, ids, unresolved edges."""

from pathlib import Path

from duckdb_graph.extractors.markdown import MarkdownExtractor

FIXTURE = Path(__file__).parent / "fixtures" / "markdown"


def _extract(rel: str):
    text = (FIXTURE / rel).read_text(encoding="utf-8")
    return MarkdownExtractor().extract(text, rel)


def _raw_targets(extraction, kind):
    return sorted(e.dst_id.lstrip("?") for e in extraction.edges if e.kind == kind)


def test_single_page_node_qualified_name_is_rel_path():
    extraction = _extract("index.md")
    pages = [n for n in extraction.nodes if n.kind == "page"]
    assert len(pages) == 1
    assert pages[0].qualified_name == "index.md"


def test_page_node_id_shape():
    extraction = _extract("index.md")
    page = next(n for n in extraction.nodes if n.kind == "page")
    assert page.node_id == "page:index.md@index.md:1"


def test_heading_nodes_carry_slug_qualified_name():
    extraction = _extract("index.md")
    headings = {n.qualified_name for n in extraction.nodes if n.kind == "heading"}
    assert "index.md#knowledge-base" in headings
    assert "index.md#getting-started" in headings
    # A repeated heading gets a deterministic suffix.
    assert "index.md#getting-started-1" in headings


def test_defines_edges_page_to_heading_are_exact_and_intra_file():
    extraction = _extract("index.md")
    page = next(n for n in extraction.nodes if n.kind == "page")
    defines = [e for e in extraction.edges if e.kind == "defines"]
    assert defines
    assert all(e.resolution == "exact" for e in defines)
    assert all(e.path == "index.md" for e in defines)
    assert all(e.src_id == page.node_id for e in defines)


def test_node_ids_are_byte_identical_across_two_runs():
    first = _extract("index.md")
    second = _extract("index.md")
    assert [n.node_id for n in first.nodes] == [n.node_id for n in second.nodes]


def test_links_to_targets_include_relative_href_and_wikilink_title():
    extraction = _extract("index.md")
    links = _raw_targets(extraction, "links_to")
    assert "guide.md" in links
    assert "Reference Notes" in links


def test_embeds_target_includes_image_src():
    extraction = _extract("index.md")
    assert "assets/diagram.png" in _raw_targets(extraction, "embeds")


def test_tagged_targets_include_inline_and_front_matter_tags():
    extraction = _extract("index.md")
    tagged = _raw_targets(extraction, "tagged")
    # Inline body tag.
    assert "architecture" in tagged
    # Front-matter tags.
    assert "overview" in tagged
    assert "knowledge-base" in tagged


def test_tag_node_exists_per_distinct_tag():
    extraction = _extract("index.md")
    tag_names = {n.name for n in extraction.nodes if n.kind == "tag"}
    assert {"overview", "knowledge-base", "architecture"} <= tag_names


def test_code_span_tags_and_wikilinks_are_ignored():
    extraction = _extract("index.md")
    assert "not-a-tag" not in {n.name for n in extraction.nodes if n.kind == "tag"}
    assert "not-a-wikilink" not in _raw_targets(extraction, "links_to")


def test_all_edges_originate_from_the_page_except_nothing():
    # links_to / embeds / tagged all originate from the single page node.
    extraction = _extract("index.md")
    page = next(n for n in extraction.nodes if n.kind == "page")
    for edge in extraction.edges:
        if edge.kind in ("links_to", "embeds", "tagged"):
            assert edge.src_id == page.node_id
            assert edge.resolution == "best_effort"
