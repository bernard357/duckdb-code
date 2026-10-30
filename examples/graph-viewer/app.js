// Self-contained Cytoscape.js viewer for a duckdb-code export (graph.json).
//
// The export carries the Cytoscape data contract from FEAT-001:
//   nodes: [{ id, kind, name, qualified_name, path, language, start_line,
//             end_line }]
//   edges: [{ source, target, kind, resolution }]
// so each element maps straight into Cytoscape's { data: { ... } } shape.
//
// Rather than dumping the whole graph, the viewer renders a focused subgraph:
// it seeds with a few nodes and grows on demand — clicking a node pulls in its
// forward and reverse one-hop neighbours.

// Per-kind node styling (shape + colour), covering both the code domain
// (file/module/class/function/import) and the Markdown domain
// (page/heading/tag).
const NODE_STYLES = {
  file: { color: "#2563eb", shape: "round-rectangle" },
  module: { color: "#0ea5e9", shape: "round-rectangle" },
  class: { color: "#7c3aed", shape: "hexagon" },
  function: { color: "#16a34a", shape: "ellipse" },
  import: { color: "#9ca3af", shape: "diamond" },
  page: { color: "#d97706", shape: "round-rectangle" },
  heading: { color: "#db2777", shape: "ellipse" },
  tag: { color: "#0d9488", shape: "triangle" },
};
const DEFAULT_NODE_STYLE = { color: "#6b7280", shape: "ellipse" };

// Per-kind edge styling (colour), covering both domains.
const EDGE_STYLES = {
  calls: "#16a34a",
  imports: "#9ca3af",
  inherits: "#7c3aed",
  references: "#2563eb",
  links_to: "#d97706",
  embeds: "#db2777",
  tagged: "#0d9488",
  defines: "#64748b",
};
const DEFAULT_EDGE_COLOR = "#94a3b8";

const nodeStyle = (kind) => NODE_STYLES[kind] || DEFAULT_NODE_STYLE;
const edgeColor = (kind) => EDGE_STYLES[kind] || DEFAULT_EDGE_COLOR;

// How many seed nodes to show before any expansion.
const SEED_LIMIT = 8;

async function main() {
  const status = document.getElementById("status");
  let graph;
  try {
    const response = await fetch("graph.json");
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    graph = await response.json();
  } catch (error) {
    status.textContent =
      `Could not load graph.json (${error.message}). Serve this folder over ` +
      `HTTP, e.g. "python -m http.server" from examples/graph-viewer, then ` +
      `open http://localhost:8000/.`;
    return;
  }

  // Index the full export; only a subset is added to the canvas at a time.
  const allNodes = new Map();
  for (const node of graph.nodes) {
    allNodes.set(node.id, node);
  }
  const outgoing = new Map();
  const incoming = new Map();
  for (const edge of graph.edges) {
    if (!outgoing.has(edge.source)) {
      outgoing.set(edge.source, []);
    }
    outgoing.get(edge.source).push(edge);
    if (!incoming.has(edge.target)) {
      incoming.set(edge.target, []);
    }
    incoming.get(edge.target).push(edge);
  }

  const cy = cytoscape({
    container: document.getElementById("cy"),
    style: [
      {
        selector: "node",
        style: {
          "background-color": (ele) => nodeStyle(ele.data("kind")).color,
          shape: (ele) => nodeStyle(ele.data("kind")).shape,
          label: "data(name)",
          "font-size": 10,
          color: "#1b1b1b",
          "text-valign": "bottom",
          "text-margin-y": 3,
          width: 26,
          height: 26,
        },
      },
      {
        selector: "edge",
        style: {
          width: 2,
          "line-color": (ele) => edgeColor(ele.data("kind")),
          "target-arrow-color": (ele) => edgeColor(ele.data("kind")),
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          label: "data(kind)",
          "font-size": 8,
          color: "#555",
          "text-rotation": "autorotate",
        },
      },
      { selector: ".hidden", style: { display: "none" } },
    ],
    layout: { name: "breadthfirst", directed: true, padding: 20 },
  });

  const shownEdges = new Set();

  function addNode(id) {
    const node = allNodes.get(id);
    if (!node || cy.getElementById(id).nonempty()) {
      return;
    }
    cy.add({
      group: "nodes",
      data: {
        id: node.id,
        kind: node.kind,
        name: node.name,
        qualified_name: node.qualified_name,
        path: node.path,
        language: node.language,
        start_line: node.start_line,
        end_line: node.end_line,
      },
    });
  }

  function edgeKey(edge) {
    return `${edge.source}|${edge.target}|${edge.kind}`;
  }

  // Add an edge only when both endpoints are already on the canvas.
  function addEdgeIfBothShown(edge) {
    const key = edgeKey(edge);
    if (shownEdges.has(key)) {
      return;
    }
    if (
      cy.getElementById(edge.source).nonempty() &&
      cy.getElementById(edge.target).nonempty()
    ) {
      cy.add({
        group: "edges",
        data: {
          id: key,
          source: edge.source,
          target: edge.target,
          kind: edge.kind,
          resolution: edge.resolution,
        },
      });
      shownEdges.add(key);
    }
  }

  // Expand one node: pull in its forward and reverse one-hop neighbours.
  function expand(id) {
    const forward = outgoing.get(id) || [];
    const reverse = incoming.get(id) || [];
    for (const edge of forward) {
      addNode(edge.target);
    }
    for (const edge of reverse) {
      addNode(edge.source);
    }
    // Any export edge whose endpoints are now both present becomes visible.
    for (const edge of graph.edges) {
      addEdgeIfBothShown(edge);
    }
    applyFilters();
    cy.layout({ name: "breadthfirst", directed: true, padding: 20 }).run();
  }

  cy.on("tap", "node", (event) => expand(event.target.id()));

  // Filter state: a kind maps to whether it is shown.
  const nodeKindsPresent = new Set(graph.nodes.map((n) => n.kind));
  const edgeKindsPresent = new Set(graph.edges.map((e) => e.kind));
  const nodeFilter = new Map([...nodeKindsPresent].map((k) => [k, true]));
  const edgeFilter = new Map([...edgeKindsPresent].map((k) => [k, true]));

  function applyFilters() {
    cy.nodes().forEach((ele) => {
      const on = nodeFilter.get(ele.data("kind")) !== false;
      ele.toggleClass("hidden", !on);
    });
    cy.edges().forEach((ele) => {
      const on =
        edgeFilter.get(ele.data("kind")) !== false &&
        !ele.source().hasClass("hidden") &&
        !ele.target().hasClass("hidden");
      ele.toggleClass("hidden", !on);
    });
  }

  // Build the node-kind and edge-kind filter toggles.
  function buildFilters(containerId, kinds, filterMap, isEdge) {
    const container = document.getElementById(containerId);
    [...kinds].sort().forEach((kind) => {
      const label = document.createElement("label");
      label.className = "filter";

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.addEventListener("change", () => {
        filterMap.set(kind, checkbox.checked);
        applyFilters();
      });

      const swatch = document.createElement("span");
      swatch.className = isEdge ? "swatch edge" : "swatch";
      swatch.style.background = isEdge ? edgeColor(kind) : nodeStyle(kind).color;

      const text = document.createElement("span");
      text.textContent = kind;

      label.append(checkbox, swatch, text);
      container.append(label);
    });
  }

  buildFilters("node-filters", nodeKindsPresent, nodeFilter, false);
  buildFilters("edge-filters", edgeKindsPresent, edgeFilter, true);

  // Build the legend from the kinds present in the export.
  function buildLegend() {
    const legend = document.getElementById("legend");
    [...nodeKindsPresent].sort().forEach((kind) => {
      const row = document.createElement("div");
      row.className = "filter";
      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = nodeStyle(kind).color;
      const text = document.createElement("span");
      text.textContent = `node: ${kind}`;
      row.append(swatch, text);
      legend.append(row);
    });
    [...edgeKindsPresent].sort().forEach((kind) => {
      const row = document.createElement("div");
      row.className = "filter";
      const swatch = document.createElement("span");
      swatch.className = "swatch edge";
      swatch.style.background = edgeColor(kind);
      const text = document.createElement("span");
      text.textContent = `edge: ${kind}`;
      row.append(swatch, text);
      legend.append(row);
    });
  }

  buildLegend();

  // Seed the canvas: prefer page/file/module nodes as entry points, then fall
  // back to the first few nodes so the graph is never empty.
  const seedOrder = ["page", "file", "module", "class", "function"];
  const seeds = [];
  for (const kind of seedOrder) {
    for (const node of graph.nodes) {
      if (node.kind === kind && !seeds.includes(node.id)) {
        seeds.push(node.id);
      }
    }
  }
  for (const node of graph.nodes) {
    if (!seeds.includes(node.id)) {
      seeds.push(node.id);
    }
  }
  seeds.slice(0, SEED_LIMIT).forEach(addNode);
  for (const edge of graph.edges) {
    addEdgeIfBothShown(edge);
  }
  applyFilters();
  cy.layout({ name: "breadthfirst", directed: true, padding: 20 }).run();

  status.innerHTML =
    `Loaded <strong>${graph.nodes.length}</strong> nodes and ` +
    `<strong>${graph.edges.length}</strong> edges. Showing ` +
    `<strong>${Math.min(SEED_LIMIT, graph.nodes.length)}</strong> seed ` +
    `node(s) — click to expand.`;
}

main();
