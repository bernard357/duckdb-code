import { defineConfig } from 'vitepress'
import { withMermaid } from 'vitepress-plugin-mermaid'

// https://vitepress.dev/reference/site-config
//
// A documentation site over the existing documentation/ pages — a curated
// sidebar, local search, and the duckdb-graph house skin. No blog
// machinery: no state filter, no Atom feed, no post components.
export default withMermaid(
  defineConfig({
    title: 'duckdb-graph',
    description:
      'A frugal, embeddable code-graph building block for agentic systems — tree-sitter impact analysis and structure explanation over a local DuckDB file.',
    // Served from the project Pages URL bernard357.github.io/duckdb-graph/.
    // If a custom domain is added later, this flips to '/'.
    base: '/duckdb-graph/',
    lang: 'en-US',
    cleanUrls: true,
    lastUpdated: true,
    // The graph viewer guide links to the local dev server
    // (http://localhost:8000/) you start to serve the exported viewer. That is
    // a runtime URL, not an internal page, so exclude localhost from the
    // dead-link check; every in-site link still resolves.
    ignoreDeadLinks: [/^https?:\/\/localhost/],
    // Pre-bundle Mermaid's CommonJS dependencies so the Vite dev server
    // resolves their ESM interop; without this the dev page can render blank.
    // cytoscape is pre-bundled too for the graph viewer guide. The production
    // (Rollup) build already handles this.
    vite: {
      optimizeDeps: {
        include: ['mermaid', 'dayjs', 'fastdom', 'cytoscape'],
      },
    },
    themeConfig: {
      // https://vitepress.dev/reference/default-theme-config
      nav: [
        { text: 'PyPI', link: 'https://pypi.org/project/duckdb-graph/' },
        {
          text: 'GitHub',
          link: 'https://github.com/bernard357/duckdb-graph',
        },
      ],
      sidebar: [
        {
          text: 'Start here',
          items: [
            { text: 'User guide', link: '/duckdb-graph-guide' },
            { text: 'Examples', link: '/examples' },
            { text: 'Graph viewer', link: '/graph-viewer-guide' },
            { text: 'Developer workbook', link: '/developer-workbook' },
            {
              text: 'Foster adoption',
              link: '/register-package-procedure',
            },
          ],
        },
        {
          text: 'Runbooks',
          items: [
            {
              text: 'Kilo Code MCP setup',
              link: '/setup-kilo-code-runbook',
            },
          ],
        },
      ],
      search: {
        provider: 'local',
      },
      socialLinks: [
        {
          icon: 'github',
          link: 'https://github.com/bernard357/duckdb-graph',
        },
      ],
    },
  }),
)
