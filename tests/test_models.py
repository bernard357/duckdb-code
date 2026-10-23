"""Construction and declared shape of the core data models."""

from duckdb_graph.models import (
    EDGE_KINDS,
    NODE_KINDS,
    RESOLUTIONS,
    Edge,
    Explanation,
    ExplanationResult,
    ImpactNode,
    ImpactResult,
    Node,
    Report,
)


def _node(**overrides) -> Node:
    fields = dict(
        node_id="function:payments.charge.run@payments/charge.py:10",
        kind="function",
        name="run",
        qualified_name="payments.charge.run",
        path="payments/charge.py",
        start_line=10,
        end_line=20,
        language="python",
    )
    fields.update(overrides)
    return Node(**fields)


def test_kind_constants():
    assert NODE_KINDS == (
        "file",
        "module",
        "class",
        "function",
        "import",
        "page",
        "heading",
        "tag",
    )
    assert EDGE_KINDS == (
        "defines",
        "imports",
        "calls",
        "inherits",
        "references",
        "links_to",
        "embeds",
        "tagged",
    )
    assert RESOLUTIONS == ("exact", "best_effort")


def test_markdown_kinds_present():
    assert "page" in NODE_KINDS
    assert "heading" in NODE_KINDS
    assert "tag" in NODE_KINDS
    assert "links_to" in EDGE_KINDS
    assert "embeds" in EDGE_KINDS
    assert "tagged" in EDGE_KINDS


def test_node_fields():
    node = _node()
    assert set(Node.model_fields) == {
        "node_id",
        "kind",
        "name",
        "qualified_name",
        "path",
        "start_line",
        "end_line",
        "language",
    }
    assert node.start_line == 10
    assert node.language == "python"


def test_edge_fields():
    edge = Edge(
        src_id="a",
        dst_id="b",
        kind="calls",
        path="payments/charge.py",
        resolution="exact",
    )
    assert set(Edge.model_fields) == {
        "src_id",
        "dst_id",
        "kind",
        "path",
        "resolution",
    }
    assert edge.resolution == "exact"


def test_impact_node_fields():
    impact_node = ImpactNode(node=_node(), distance=1, resolution="best_effort")
    assert set(ImpactNode.model_fields) == {"node", "distance", "resolution"}
    assert impact_node.distance == 1


def test_impact_result_defaults_and_fields():
    result = ImpactResult(
        target="payments.charge.run",
        resolved=[_node()],
        affected=[ImpactNode(node=_node(), distance=1, resolution="exact")],
        affected_files=["payments/charge.py"],
    )
    assert set(ImpactResult.model_fields) == {
        "target",
        "resolved",
        "ambiguous",
        "affected",
        "affected_files",
    }
    assert result.ambiguous is False


def test_explanation_fields():
    explanation = Explanation(
        node=_node(),
        defines=[],
        fan_out=[],
        fan_in=[],
        bases=[],
        subclasses=[],
        fan_in_count=0,
        fan_out_count=0,
    )
    assert set(Explanation.model_fields) == {
        "node",
        "defines",
        "fan_out",
        "fan_in",
        "bases",
        "subclasses",
        "fan_in_count",
        "fan_out_count",
    }
    assert explanation.fan_in_count == 0


def test_explanation_result_defaults_and_fields():
    result = ExplanationResult(
        target="payments.charge.run",
        resolved=[_node()],
        explanations=[],
    )
    assert set(ExplanationResult.model_fields) == {
        "target",
        "resolved",
        "ambiguous",
        "explanations",
    }
    assert result.ambiguous is False


def test_report_fields():
    report = Report(
        db_path="graph.duckdb",
        schema_version=1,
        languages=["python"],
        files_by_language={"python": 2},
        nodes_by_kind={"function": 3},
        edges_by_kind={"calls": 1},
        roots=["src"],
    )
    assert set(Report.model_fields) == {
        "db_path",
        "schema_version",
        "languages",
        "files_by_language",
        "nodes_by_kind",
        "edges_by_kind",
        "roots",
    }
    assert report.schema_version == 1


def test_impact_node_resolution_docstring_states_seed_origin():
    doc = " ".join((ImpactNode.__doc__ or "").split())
    assert "seed" in doc
    assert "best_effort origin" in doc
    assert "exact origin" in doc
    assert "equal-distance tie" in doc
