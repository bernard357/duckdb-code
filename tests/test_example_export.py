"""Guard test for the committed viewer export examples/graph-viewer/graph.json.

The viewer under examples/graph-viewer/ consumes this committed export. This
test fails loudly if the file is missing, mis-shaped, or stale enough that an
edge references a node id that is not present — so a wrong-shaped export cannot
pass silently.
"""

import json
from pathlib import Path

GRAPH_JSON = Path(__file__).parent.parent / "examples" / "graph-viewer" / "graph.json"


def test_example_export_parses_and_has_contract_keys():
    data = json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
    assert set(data) >= {"meta", "nodes", "edges"}
    assert isinstance(data["nodes"], list)
    assert isinstance(data["edges"], list)


def test_example_export_edges_reference_real_nodes():
    data = json.loads(GRAPH_JSON.read_text(encoding="utf-8"))
    node_ids = {node["id"] for node in data["nodes"]}
    for edge in data["edges"]:
        assert edge["source"] in node_ids, edge
        assert edge["target"] in node_ids, edge
