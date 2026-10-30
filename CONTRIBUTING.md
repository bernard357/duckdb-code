# Contributing to duckdb-code

Thanks for considering a contribution. `duckdb-code` grows through small,
focused improvements, and new contributors are genuinely welcome — whether you
fix a typo, add a language extractor, or sharpen the docs.

This page helps you decide whether a change fits before you dive in. The full
developer reference — prerequisites, setup, the Makefile workflow, testing, and
the detailed conventions — lives in the
[developer workbook](documentation/developer-workbook.md).

## Does your change fit?

`duckdb-code` is intentionally small, and the best contributions keep it that
way: one clear thing, done well, with a test and a line of docs. A change fits
when it respects the project's shape:

- __Do one thing well.__ This is a structural building block — `impact`,
  `explain`, `report` over a code and Markdown graph. Semantic and vector
  search belong in the sibling
  [`duckdb-vectors`](https://github.com/bernard357/duckdb-vectors), not here.
- __Boring, composable technology.__ Prefer the standard library and DuckDB's
  own features over new dependencies.
- __Framework-agnostic.__ No hard dependency on a specific agent framework.
- __Read-mostly and fast.__ Queries never mutate the graph; writes happen only
  on `index` (including the `mcp` server's optional startup build).

## Good first contributions

You do not need to understand the whole codebase to help:

- __Add an extractor__ — the registry under `src/duckdb_code/extractors/` is
  the main extension point for a new language.
- __Improve the docs__ — clarify a page or add a worked example.
- __Report a rough edge__ — a clear issue with a minimal reproduction.

The [developer workbook](documentation/developer-workbook.md) explains how to do
each of these, how to set up your environment (`make setup`), and how to run the
checks (`make lint`, `make test`, `make self-test`) before you open a pull
request. If you are unsure where to start, open an issue describing what you
want to do.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE) that covers this project.
