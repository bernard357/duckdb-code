# Graph viewer example

A self-contained web viewer that renders a `duckdb-graph` export (`graph.json`)
with [Cytoscape.js](https://js.cytoscape.org/). It is a single `index.html`
plus `app.js` — no build step, no server framework. The viewer shows a focused
subgraph and grows it on demand: click a node to expand its one-hop
neighbourhood (forward and reverse). Node kinds and edge kinds each get a
distinct shape/colour, a legend is built from the kinds present, and per-kind
filter toggles show or hide elements.

The committed `graph.json` here is a small, stable export of the
`tests/fixtures/markdown` corpus, so the viewer renders something out of the
box. This is structural visualization; semantic/vector search is the sibling
[`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors) package.

## Prerequisites

- `duckdb-graph` installed on your PATH (`pip install duckdb-graph`), only if
  you want to regenerate `graph.json`.
- A browser and a local static file server (the viewer `fetch`es `graph.json`,
  which browsers block over `file://`).

## Open it

Serve this folder over HTTP and open the page:

```shell
$ cd examples/graph-viewer
$ python -m http.server 8000
```

Then browse to <http://localhost:8000/>. The committed `graph.json` loads
immediately.

## Regenerate graph.json

The committed export is generated from the Markdown fixture corpus. From the
repository root:

```shell
$ duckdb-graph index tests/fixtures/markdown --db graph.duckdb
$ duckdb-graph export --db graph.duckdb --out examples/graph-viewer/graph.json
```

To view a code graph instead, index the package source and export over the same
file:

```shell
$ duckdb-graph index src/duckdb_graph --db graph.duckdb
$ duckdb-graph export --db graph.duckdb --out examples/graph-viewer/graph.json
```

The `.duckdb` file is throwaway and git-ignored; only `graph.json` is committed.

## CDN versus vendoring

`index.html` loads Cytoscape.js from a pinned CDN URL
(`unpkg.com/cytoscape@3.30.2`). For fully offline use, vendor it next to
`index.html` and point the `<script src>` at the local copy:

```shell
$ curl -L https://unpkg.com/cytoscape@3.30.2/dist/cytoscape.min.js \
    -o examples/graph-viewer/cytoscape.min.js
```

then change the script tag to `<script src="cytoscape.min.js"></script>`. The
viewer makes no other network calls.

## Manual verification

The HTML/JS viewer is not covered by the Python lint or the pytest suite; it is
verified by hand. Serve the folder (above), open the page, and confirm:

- __Pan and zoom__: drag the canvas to pan, scroll to zoom.
- __Click to expand__: clicking a node pulls in its forward and reverse one-hop
  neighbours; the graph grows rather than showing everything at once.
- __Node-kind filters__: toggling a node-kind checkbox hides/shows nodes of
  that kind (and edges touching them).
- __Edge-kind filters__: toggling an edge-kind checkbox hides/shows edges of
  that kind.
- __Legend__: the legend lists every node kind and edge kind present, with its
  shape/colour swatch.
- __Both domains render__: the committed Markdown export shows `page`,
  `heading`, and `tag` nodes with `links_to`/`tagged`/`defines` edges. After
  regenerating from `src/duckdb_graph` (above), a code export shows
  `file`/`module`/`class`/`function`/`import` nodes with
  `calls`/`imports`/`inherits`/`references` edges, each with distinct styling.
