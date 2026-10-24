# Communication kit for package discovery

This page is a reusable kit for spreading `duckdb-graph` across both human and
agentic audiences. Beyond automated discovery, we maintain a communication
package and a list of referencing places for `duckdb-graph`. For the full
picture of what you are registering, start from the
[documentation home](index.md).

## One-time operator setup

Three one-time actions bring the release and documentation machinery online.
They are account-level operator steps, performed once by a maintainer with the
right access, not part of a normal pull request:

1. __Create the PyPI project and Trusted Publisher.__ Register the
   `duckdb-graph` project on PyPI, then add a Trusted Publisher pointing at this
   repository's `deploy-pypi` workflow and the `pypi` environment. OIDC Trusted
   Publishing means the workflow publishes with no long-lived API token.
2. __Enable GitHub Pages with the Actions source.__ In the repository settings,
   set Pages to build from GitHub Actions so the `deploy-vitepress` workflow can
   publish `documentation/.vitepress/dist` to the project Pages URL.
3. __Cut the first release.__ Push the first `vYYMMDD` tag to trigger
   `deploy-pypi` and publish the initial version.

## Automated discovery

Two discovery conventions can be served from the project itself, so most venues
can point at them rather than restate the content:

- __`AGENTS.md`__ — an agent-facing Markdown file at the repository top level:
  [AGENTS.md](https://github.com/bernard357/duckdb-graph/blob/main/AGENTS.md),
  usable jointly by crawling agents and by agentic IDEs. It is the agreed
  "README for agents" standard that most coding agents now read. See
  [agents.md](https://agents.md/).

- __`llms.txt`__ — a curated, Markdown index for LLMs served at the root of the
  documentation endpoint, when published. The convention, proposed by Jeremy
  Howard in 2024, is an H1 project name, a one-line blockquote summary, then H2
  sections of annotated Markdown links; it curates rather than dumps, and it
  does not control crawling. See [llmstxt.org](https://llmstxt.org/).

## Working a reference

Working on a new reference to `duckdb-graph` consists of the following steps:

1. Scope a suitable registry or reference where it would make sense to list
   `duckdb-graph`.
2. If this reference is already present in the table below and tagged
   `complete`, stop here. Consider another reference target.
3. __candidate → on-going.__ Open a pull request that updates this page,
   mentioning what you are working on, then do whatever is necessary to register
   `duckdb-graph` at the referencing site. Read the venue's current submission
   rules at its link in the table below (they drift), then prepare the
   submission from the registration package.
4. __on-going.__ The submission is open and awaiting third-party review or
   merge. Record the pull-request or submission URL here next to the venue.
5. __on-going → complete.__ Complete the pull request: update this page, then
   ask for a merge. Verify by loading the public listing, not by trusting the
   merge — confirm the entry resolves and the links in it work.

Keep this page honest: only mark a venue `complete` once its public listing is
live and verified.

## The registration package

Copy-paste these fields into any submission form, catalog entry, or pull request
that helps referencing `duckdb-graph`.

- __Name:__ `duckdb-graph`
- __One-line pitch:__ A frugal, embeddable code-graph building block for agentic
  systems — tree-sitter impact analysis and structure explanation over a local
  DuckDB file, no external service.
- __Description:__ `duckdb-graph` indexes a repository into a single DuckDB file
  and answers structural questions — `impact` (the transitive reverse-dependency
  set of a symbol or file), `explain` (a symbol's structural neighbourhood), and
  `report` (index introspection) — with a local function call. The file bakes
  into the agent's own container image, so there is no external code-graph
  service to run. It parses Python and JavaScript/TypeScript with tree-sitter
  and also indexes Markdown link graphs, and it ships an MCP server (stdio and
  Streamable HTTP) exposing its read-only tools.
- __Keywords:__ duckdb, code-graph, tree-sitter, impact-analysis, static-analysis,
  dependency-graph, agents, mcp, llm, codebase
- __Install:__ `pip install duckdb-graph`
- __MCP tools:__ `duckdb-graph mcp` exposes three read-only tools —
  `impact(target, …)`, `explain(target, …)`, and `report()` — over stdio
  (same-container subprocess) and Streamable HTTP (`--transport http`).
- __License:__ MIT
- __Links:__
  - PyPI: <https://pypi.org/project/duckdb-graph/>
  - Documentation: <https://bernard357.github.io/duckdb-graph/>
  - Repository: <https://github.com/bernard357/duckdb-graph>
  - Agent instructions: [AGENTS.md](https://github.com/bernard357/duckdb-graph/blob/main/AGENTS.md)

## External references

Each venue is a place where the package can be discovered. Use the `candidate` →
`on-going` → `complete` lifecycle. Every `candidate` requires a manual
maintainer or agent action, pending third-party review. Moving to `complete`
requires an accepted review.

| Venue | Type | Status |
| --- | --- | --- |
| [PyPI](https://pypi.org/) | Package metadata | candidate |
| [Official MCP registry](https://registry.modelcontextprotocol.io/) | MCP server catalog | candidate |
| [Docker MCP Catalog](https://github.com/docker/mcp-registry) | MCP server catalog | candidate |
| [awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers) | Curated awesome-list | candidate |
| [awesome-static-analysis](https://github.com/analysis-tools-dev/static-analysis) | Curated awesome-list | candidate |

The candidates listed here are only suggestions. Please add any other reference
you believe is worth the effort to spread `duckdb-graph`.
