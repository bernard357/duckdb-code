"""Deterministic tool-quality checker: call each MCP tool, assert on its output.

Every case in ``fixtures/benchmarks/tool-checks.yaml`` calls one duckdb-code
MCP tool directly (no model, no agent) on an index of a pinned public corpus
and asserts on the JSON object it returns: expected symbols and paths, list
precision, byte budget, and the bounds of a truncated ``impact``. The corpus
repositories are cloned at their pinned commits under
``~/.cache/duckdb-code/quality/corpus`` and indexed into a cache database next
to it, re-indexed only when the commits, the embedder, or the package source
change.

Calls go in-process through the server's own ``call_tool`` by default, which
returns the same JSON as the stdio transport; ``--stdio`` spawns
``duckdb-code mcp`` and talks MCP over its stdin/stdout instead. ``--offline``
indexes with the hashing embedder and skips the semantic cases.

A check marked ``xfail`` is a known gap tracked by a GitHub issue: its failure
is reported as XFAIL, and its passing as FIXED (remove the marker). The exit
code is non-zero only on a regression (a FAIL) or a tool error (an ERROR).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SUITE = REPO_ROOT / "fixtures" / "benchmarks" / "tool-checks.yaml"
DEFAULT_WORKDIR = Path.home() / ".cache" / "duckdb-code" / "quality"
DEFAULT_SOURCE_ROOT = Path.home() / "Public"

Status = Literal["PASS", "FAIL", "XFAIL", "FIXED", "SKIP", "ERROR"]


class Xfail(BaseModel):
    """A known gap: the issue tracking it and why the check fails today."""

    issue: int
    reason: str


class Check(BaseModel):
    """One assertion on a tool's JSON response (see the fixture header)."""

    type: Literal[
        "contains_all", "excludes_any", "equals_set", "count", "bounded", "max_bytes"
    ]
    field: str = ""
    key: str = ""
    values: list[str] = []
    match: Literal["suffix", "prefix"] = "suffix"
    within: int | None = None
    equals: int | None = None
    min: int | None = None
    max: int | None = None
    truncated: bool | None = None
    totals: dict[str, str] = {}
    where: dict[str, str] = {}
    xfail: Xfail | None = None


class Case(BaseModel):
    """One direct tool call and the checks on its response."""

    id: str
    question: str = ""
    tool: str
    args: dict[str, Any] = {}
    semantic: bool = False
    checks: list[Check]


class Corpus(BaseModel):
    """The repositories to index, each pinned to a commit."""

    github_owner: str
    pinned_commits: dict[str, str]


class Suite(BaseModel):
    """The whole check suite, as loaded from the YAML fixture."""

    version: int
    max_bytes: int
    corpus: Corpus
    cases: list[Case]


class CaseResult(BaseModel):
    """The scored outcome of one case."""

    id: str
    tool: str
    status: Status
    passed: int = 0
    total: int = 0
    bytes: int = 0
    detail: str = ""
    issues: list[int] = []


def load_suite(path: Path) -> Suite:
    """Load and validate the check suite from its YAML fixture."""
    return Suite.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def matches(item: str, value: str, mode: str) -> bool:
    """Whether a response ``item`` matches an expected ``value``."""
    if mode == "prefix":
        return item.startswith(value)
    return item == value or item.endswith("/" + value) or item.endswith("." + value)


def extract(payload: Any, dotted: str) -> Any:
    """Follow a dotted path into JSON; numeric segments index lists."""
    current = payload
    for segment in [s for s in dotted.split(".") if s]:
        if isinstance(current, list) and segment.isdigit():
            index = int(segment)
            current = current[index] if index < len(current) else None
        elif isinstance(current, dict):
            current = current.get(segment)
        else:
            return None
    return current


def _rows_of(payload: Any, check: Check) -> list[Any]:
    """The list at ``field``, keeping the rows that satisfy ``where``."""
    rows = extract(payload, check.field) or []
    return [
        row
        for row in rows
        if all(
            matches(str(extract(row, key)), value, check.match)
            for key, value in check.where.items()
        )
    ]


def _items(payload: Any, check: Check) -> list[str]:
    return [str(extract(row, check.key)) for row in _rows_of(payload, check)]


def _missing(items: list[str], check: Check) -> list[str]:
    return [
        v for v in check.values if not any(matches(i, v, check.match) for i in items)
    ]


def _check_contains_all(payload: Any, check: Check) -> str:
    items = _items(payload, check)
    if check.within is not None:
        items = items[: check.within]
    missing = _missing(items, check)
    scope = f"top {check.within}" if check.within is not None else check.field
    return f"missing from {scope}: {', '.join(missing)}" if missing else ""


def _check_excludes_any(payload: Any, check: Check) -> str:
    hits = [
        item
        for item in _items(payload, check)
        if any(matches(item, v, check.match) for v in check.values)
    ]
    return (
        f"unexpected in {check.field}: {', '.join(sorted(set(hits)))}" if hits else ""
    )


def _check_equals_set(payload: Any, check: Check) -> str:
    items = _items(payload, check)
    missing = _missing(items, check)
    extra = sorted(
        {i for i in items if not any(matches(i, v, check.match) for v in check.values)}
    )
    problems = []
    if missing:
        problems.append(f"missing {', '.join(missing)}")
    if extra:
        shown = ", ".join(extra[:3]) + (" ..." if len(extra) > 3 else "")
        problems.append(f"{len(extra)} extra ({shown})")
    return f"{check.field}: {'; '.join(problems)}" if problems else ""


def _check_count(payload: Any, check: Check) -> str:
    size = len(_rows_of(payload, check))
    if check.equals is not None and size != check.equals:
        return f"{check.field}: {size} items, expected {check.equals}"
    if check.min is not None and size < check.min:
        return f"{check.field}: {size} items, expected at least {check.min}"
    if check.max is not None and size > check.max:
        return f"{check.field}: {size} items, expected at most {check.max}"
    return ""


def _check_bounded(payload: Any, check: Check) -> str:
    limit = payload.get("limit") if isinstance(payload, dict) else None
    if not isinstance(limit, int):
        return "response carries no limit (unbounded)"
    problems = []
    for field, total_field in check.totals.items():
        size = len(payload.get(field) or [])
        total = payload.get(total_field)
        if size > limit:
            problems.append(f"{field} holds {size} > limit {limit}")
        if not isinstance(total, int) or total < size:
            problems.append(f"{total_field} is {total}, below {size} listed")
    if check.truncated is not None and payload.get("truncated") is not check.truncated:
        problems.append(f"truncated is {payload.get('truncated')}")
    return "; ".join(problems)


def _check_max_bytes(size: int, budget: int) -> str:
    return f"{size} bytes over the {budget}-byte budget" if size > budget else ""


_CHECKS = {
    "contains_all": _check_contains_all,
    "excludes_any": _check_excludes_any,
    "equals_set": _check_equals_set,
    "count": _check_count,
    "bounded": _check_bounded,
}


def run_check(check: Check, payload: Any, size: int) -> str:
    """Run one check; return an empty string on success, else the reason."""
    if check.type == "max_bytes":
        return _check_max_bytes(size, check.max or 0)
    return _CHECKS[check.type](payload, check)


def evaluate_case(case: Case, payload: Any, size: int, max_bytes: int) -> CaseResult:
    """Score a case's response: run its checks plus the global byte budget."""
    failures: list[str] = []
    xfailed: list[str] = []
    fixed: list[str] = []
    issues: list[int] = []
    budget = _check_max_bytes(size, max_bytes)
    if budget:
        failures.append(budget)
    for check in case.checks:
        reason = run_check(check, payload, size)
        if check.xfail:
            issues.append(check.xfail.issue)
            if reason:
                xfailed.append(f"{reason} (#{check.xfail.issue})")
            else:
                fixed.append(
                    f"{check.type} now passes, remove xfail #{check.xfail.issue}"
                )
        elif reason:
            failures.append(reason)
    total = len(case.checks) + 1
    passed = total - len(failures) - len(xfailed)
    if failures:
        status: Status = "FAIL"
    elif fixed:
        status = "FIXED"
    elif xfailed:
        status = "XFAIL"
    else:
        status = "PASS"
    detail = "; ".join(failures or fixed or xfailed)
    return CaseResult(
        id=case.id,
        tool=case.tool,
        status=status,
        passed=passed,
        total=total,
        bytes=size,
        detail=detail,
        issues=sorted(set(issues)),
    )


def skip_result(case: Case, reason: str) -> CaseResult:
    """A case not run (a semantic case under --offline)."""
    return CaseResult(id=case.id, tool=case.tool, status="SKIP", detail=reason)


def exit_code(results: list[CaseResult]) -> int:
    """Non-zero only on a regression (FAIL) or a tool error (ERROR)."""
    return 1 if any(r.status in ("FAIL", "ERROR") for r in results) else 0


class CheckError(Exception):
    """Raised when the corpus or the index cannot be prepared."""


def _git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise CheckError(f"git {' '.join(args)}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _source_for(repo: str, source_root: Path, owner: str) -> str:
    """A local clone under ``source_root`` when present, else GitHub."""
    local = source_root / repo
    if (local / ".git").exists():
        return str(local)
    return f"https://github.com/{owner}/{repo}.git"


def prepare_corpus(corpus: Corpus, corpus_dir: Path, source_root: Path) -> None:
    """Clone each repository and check out its pinned commit, detached."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    unexpected = sorted(
        p.name for p in corpus_dir.iterdir() if p.name not in corpus.pinned_commits
    )
    if unexpected:
        raise CheckError(f"unexpected entries in {corpus_dir}: {unexpected}")
    for repo, commit in sorted(corpus.pinned_commits.items()):
        dest = corpus_dir / repo
        if not (dest / ".git").exists():
            source = _source_for(repo, source_root, corpus.github_owner)
            print(f"cloning {repo} from {source}", file=sys.stderr)
            _git("clone", "--quiet", "--no-checkout", source, str(dest))
        try:
            _git("cat-file", "-e", f"{commit}^{{commit}}", cwd=dest)
        except CheckError:
            _git("fetch", "--quiet", "origin", cwd=dest)
        _git("checkout", "--quiet", "--force", "--detach", commit, cwd=dest)
        # The cache clone is ours: drop anything a previous run left behind.
        _git("clean", "-fdxq", cwd=dest)
        head = _git("rev-parse", "HEAD", cwd=dest)
        if not head.startswith(commit):
            raise CheckError(f"{repo} is at {head}, expected {commit}")


def _package_digest() -> str:
    """A digest of the installed duckdb_code source, so code changes re-index."""
    import duckdb_code

    package = Path(duckdb_code.__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(str(path.relative_to(package)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def ensure_index(
    corpus: Corpus, corpus_dir: Path, db_path: Path, embedder: str
) -> bool:
    """Index the corpus into ``db_path`` unless the cached index still matches.

    Returns whether a fresh index was built. The stamp next to the database
    records the pinned commits, the embedder, and the package source digest.
    """
    from duckdb_code.store import CodeStore

    stamp_path = db_path.with_suffix(".stamp.json")
    stamp = {
        "commits": dict(sorted(corpus.pinned_commits.items())),
        "embedder": embedder,
        "package": _package_digest(),
    }
    if db_path.exists() and stamp_path.exists():
        if json.loads(stamp_path.read_text(encoding="utf-8")) == stamp:
            return False
    for stale in (db_path, Path(f"{db_path}.wal"), stamp_path):
        stale.unlink(missing_ok=True)
    print(f"indexing {corpus_dir} into {db_path} ({embedder})", file=sys.stderr)
    store = CodeStore(db_path=str(db_path), root=str(corpus_dir), embedder=embedder)
    try:
        store.index(corpus_dir)
    finally:
        store.close()
    stamp_path.write_text(json.dumps(stamp, indent=2), encoding="utf-8")
    return True


def _text_of(outcome: Any) -> tuple[bool, str]:
    is_error = getattr(outcome, "is_error", None)
    if is_error is None:
        is_error = getattr(outcome, "isError", False)
    content = getattr(outcome, "content", None) or []
    text = "".join(getattr(block, "text", "") for block in content)
    return bool(is_error), text


def _score(case: Case, is_error: bool, text: str, max_bytes: int) -> CaseResult:
    size = len(text.encode("utf-8"))
    if is_error:
        return CaseResult(
            id=case.id, tool=case.tool, status="ERROR", bytes=size, detail=text[:200]
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        return CaseResult(
            id=case.id, tool=case.tool, status="ERROR", bytes=size, detail=str(error)
        )
    return evaluate_case(case, payload, size, max_bytes)


async def _call_in_process(
    cases: list[Case], db_path: Path, corpus_dir: Path, embedder: str
) -> list[tuple[bool, str]]:
    from duckdb_code.config import Settings
    from duckdb_code.mcp_server import build_server

    server = build_server(
        Settings(db_path=str(db_path), root=str(corpus_dir), embedder=embedder)
    )
    replies = []
    for case in cases:
        try:
            replies.append(_text_of(await server.call_tool(case.tool, case.args)))
        except Exception as error:  # a crash is scored as ERROR, not raised
            replies.append((True, f"{type(error).__name__}: {error}"))
    return replies


async def _call_over_stdio(
    cases: list[Case], db_path: Path, corpus_dir: Path, embedder: str
) -> list[tuple[bool, str]]:
    import os

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = Path(sys.executable).parent / "duckdb-code"
    params = StdioServerParameters(
        command=str(command),
        args=["mcp", "--db", str(db_path), "--index", str(corpus_dir)]
        + ["--no-startup-index"],
        env={**os.environ, "DUCKDB_CODE_EMBEDDER": embedder},
    )
    replies = []
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for case in cases:
                try:
                    outcome = await session.call_tool(case.tool, case.args)
                    replies.append(_text_of(outcome))
                except Exception as error:  # a closed transport is an ERROR
                    replies.append((True, f"{type(error).__name__}: {error}"))
    return replies


def run_suite(
    suite: Suite,
    workdir: Path,
    *,
    source_root: Path = DEFAULT_SOURCE_ROOT,
    offline: bool = False,
    stdio: bool = False,
    case_ids: list[str] | None = None,
) -> list[CaseResult]:
    """Prepare the pinned corpus and its index, then run and score each case."""
    corpus_dir = workdir / "corpus"
    embedder = "hashing" if offline else "fastembed"
    db_path = workdir / ("code-hashing.duckdb" if offline else "code.duckdb")
    prepare_corpus(suite.corpus, corpus_dir, source_root)
    ensure_index(suite.corpus, corpus_dir, db_path, embedder)
    selected = [c for c in suite.cases if not case_ids or c.id in case_ids]
    runnable = [c for c in selected if not (offline and c.semantic)]
    caller = _call_over_stdio if stdio else _call_in_process
    replies = asyncio.run(caller(runnable, db_path, corpus_dir, embedder))
    scored = {
        case.id: _score(case, is_error, text, suite.max_bytes)
        for case, (is_error, text) in zip(runnable, replies, strict=True)
    }
    return [
        scored.get(case.id) or skip_result(case, "semantic case skipped offline")
        for case in selected
    ]


def _rows(results: list[CaseResult]) -> list[list[str]]:
    return [
        [
            r.id,
            r.tool,
            r.status,
            f"{r.passed}/{r.total}" if r.total else "-",
            str(r.bytes) if r.bytes else "-",
            " ".join(f"#{n}" for n in r.issues) or "-",
            r.detail or "-",
        ]
        for r in results
    ]


_HEADERS = ["case", "tool", "status", "checks", "bytes", "issues", "detail"]


def _summary(results: list[CaseResult]) -> str:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    order = ("PASS", "XFAIL", "FIXED", "SKIP", "FAIL", "ERROR")
    return ", ".join(f"{counts[s]} {s}" for s in order if s in counts)


def render_table(results: list[CaseResult]) -> str:
    """A fixed-width scorecard for the terminal."""
    rows = _rows(results)
    widths = [
        max(len(row[i]) for row in [_HEADERS, *rows]) for i in range(len(_HEADERS) - 1)
    ]
    lines = []
    for row in [_HEADERS, *rows]:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
        lines.append("  ".join([*cells, row[-1]]).rstrip())
    lines.append(f"\n{_summary(results)}")
    return "\n".join(lines)


def render_markdown(results: list[CaseResult]) -> str:
    """The scorecard as a Markdown pipe table."""
    lines = [
        "| " + " | ".join(_HEADERS) + " |",
        "| " + " | ".join("---" for _ in _HEADERS) + " |",
    ]
    for row in _rows(results):
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in row) + " |")
    lines.append("")
    lines.append(f"Summary: {_summary(results)}.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--workdir", type=Path, default=DEFAULT_WORKDIR)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=DEFAULT_SOURCE_ROOT,
        help="clone from <source-root>/<repository> when present, else GitHub",
    )
    parser.add_argument("--offline", action="store_true", help="hashing embedder")
    parser.add_argument("--stdio", action="store_true", help="call over MCP stdio")
    parser.add_argument("--case", action="append", dest="cases", help="case id")
    parser.add_argument("--markdown", type=Path, help="also write a Markdown table")
    options = parser.parse_args(argv)
    try:
        results = run_suite(
            load_suite(options.suite),
            options.workdir,
            source_root=options.source_root,
            offline=options.offline,
            stdio=options.stdio,
            case_ids=options.cases,
        )
    except CheckError as error:
        print(f"check-tool-quality: {error}", file=sys.stderr)
        return 2
    print(render_table(results))
    if options.markdown:
        options.markdown.write_text(render_markdown(results), encoding="utf-8")
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
