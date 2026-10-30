# Graph viewer guide

`duckdb-code` builds a structural graph of code and Markdown. The viewer under
`examples/graph-viewer/` turns an export of that graph into an interactive web
widget so you can navigate it, zoom into one region, and expand a node's
neighbourhood on demand. This is structural visualization — the link and
dependency structure — while semantic and vector search stay with the sibling
[`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors) package.

- [Export then view](#export-then-view)
- [The JSON contract](#the-json-contract)
- [Interactions](#interactions)
- [Per-kind styling](#per-kind-styling)
- [Offline use](#offline-use)

## Export then view {#export-then-view}

The viewer consumes a single JSON file produced by the `export` verb. Index a
folder, export the graph, then open the viewer against the result:

```shell
$ duckdb-code index tests/fixtures/markdown --db code.duckdb
$ duckdb-code export --db code.duckdb --out examples/graph-viewer/graph.json
```

Then serve the viewer folder over HTTP and open it, because the page `fetch`es
`graph.json` and browsers block that over `file://`:

```shell
$ cd examples/graph-viewer
$ python -m http.server 8000
```

Browse to <http://localhost:8000/>. To visualize code instead of Markdown,
point `index` at the source tree (for example `src/duckdb_code`) and export
over the same `graph.json`.

## The JSON contract {#the-json-contract}

The export is the stable Cytoscape data contract. The top level has three keys:

```json
{
  "meta": { "db_path": "...", "nodes_by_kind": {}, "edges_by_kind": {} },
  "nodes": [{ "id": "...", "kind": "...", "name": "...", "path": "..." }],
  "edges": [{ "source": "...", "target": "...", "kind": "...", "resolution": "..." }]
}
```

`meta` is the same introspection the `report` verb emits. Each node and edge
carries the fields Cytoscape needs. The stored model keys are renamed in the
export so the shape drops straight into Cytoscape:

- node `node_id` becomes `id`
- edge `src_id` becomes `source`
- edge `dst_id` becomes `target`

Every `edges[*].source` and `edges[*].target` is the `id` of a node in
`nodes`, so the viewer never references a missing endpoint.

## Interactions {#interactions}

The viewer renders a focused subgraph rather than the whole graph at once:

- __Pan and zoom__: drag the canvas to pan, scroll to zoom into a region.
- __Click to expand__: clicking a node pulls in its forward and reverse
  one-hop neighbours, so the graph grows as you explore instead of dumping
  every node up front.
- __Node-kind and edge-kind filters__: checkboxes in the sidebar show or hide
  elements by kind, letting you isolate, say, only `calls` edges or only `page`
  nodes.
- __Legend__: a legend is built from the kinds present in the loaded export,
  mapping each kind to its shape and colour.

## Per-kind styling {#per-kind-styling}

Nodes and edges are styled per kind, and the scheme covers both domains the
graph understands:

- __Code domain__: `file`, `module`, `class`, `function`, and `import` nodes;
  `calls`, `imports`, `inherits`, and `references` edges.
- __Markdown domain__: `page`, `heading`, and `tag` nodes; `links_to`,
  `embeds`, `tagged`, and `defines` edges.

Each node kind gets a distinct shape and colour and each edge kind a distinct
colour, so a code export and a Markdown export are both legible at a glance.

## Offline use {#offline-use}

The viewer loads Cytoscape.js from a pinned CDN by default. To run without a
network, vendor the library next to `index.html` and point the `<script src>`
at the local copy. The step-by-step note lives in
`examples/graph-viewer/README.md`.
