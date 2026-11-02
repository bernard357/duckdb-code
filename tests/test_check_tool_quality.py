"""Tests for the deterministic tool-quality checker (scripts/check_tool_quality.py).

Unit tests cover the check semantics on hand-built payloads (matching,
contains/excludes/equals/count/bounded/max_bytes), the case status rules
(xfail, FIXED, regression), the exit code, and the committed fixture. An
end-to-end test clones a tiny local git corpus at a pinned commit, indexes it
with the offline hashing embedder, runs a small suite in-process, and checks
that two runs give identical scorecards. No network, no model.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_tool_quality.py"
FIXTURE = REPO / "fixtures" / "benchmarks" / "tool-checks.yaml"
QUESTIONS = REPO / "fixtures" / "benchmarks" / "kilo-questions.yaml"


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("check_tool_quality", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_tool_quality"] = module
    spec.loader.exec_module(module)
    return module


def _case(checker, checks, **extra):
    return checker.Case.model_validate(
        {"id": "c1", "tool": "search", "args": {}, "checks": checks, **extra}
    )


def _results(*names):
    return {"results": [{"node": {"qualified_name": n, "path": n}} for n in names]}


def test_matching_is_suffix_on_path_or_dotted_segments(checker):
    assert checker.matches("a/b/c.py", "b/c.py", "suffix")
    assert checker.matches("x.pkg.mod.fn", "mod.fn", "suffix")
    assert checker.matches("same", "same", "suffix")
    assert not checker.matches("x.pkg.mod.fn_extra", "mod.fn", "suffix")
    assert not checker.matches("ab/c.py", "b/c.py", "suffix")
    assert checker.matches("boringsteps/x.py", "boringsteps/", "prefix")
    assert not checker.matches("duckdb-code/x.py", "boringsteps/", "prefix")


def test_extract_follows_dotted_paths_and_list_indexes(checker):
    payload = {"results": [{"members": [{"name": "a"}]}]}
    assert checker.extract(payload, "results.0.members.0.name") == "a"
    assert checker.extract(payload, "results.3") is None
    assert checker.extract(payload, "missing.key") is None


def test_contains_all_respects_within(checker):
    case = _case(
        checker,
        [
            {
                "type": "contains_all",
                "field": "results",
                "key": "node.path",
                "values": ["c"],
                "within": 2,
            }
        ],
    )
    passing = checker.evaluate_case(case, _results("a", "c", "b"), 10, 1000)
    failing = checker.evaluate_case(case, _results("a", "b", "c"), 10, 1000)
    assert passing.status == "PASS"
    assert failing.status == "FAIL"
    assert "c" in failing.detail


def test_excludes_equals_and_count(checker):
    payload = _results("x/a", "x/b")
    checks = [
        {
            "type": "excludes_any",
            "field": "results",
            "key": "node.path",
            "values": ["y/"],
            "match": "prefix",
        },
        {
            "type": "equals_set",
            "field": "results",
            "key": "node.path",
            "values": ["a", "b"],
        },
        {"type": "count", "field": "results", "equals": 2},
        {"type": "count", "field": "results", "min": 1, "max": 2},
    ]
    result = checker.evaluate_case(_case(checker, checks), payload, 10, 1000)
    # Four declared checks plus the global byte budget.
    assert (result.status, result.passed, result.total) == ("PASS", 5, 5)
    extra = checker.evaluate_case(
        _case(checker, checks[1:2]), _results("x/a", "x/b", "x/c"), 10, 1000
    )
    assert extra.status == "FAIL"


def test_bounded_checks_lists_totals_and_truncated(checker):
    check = {
        "type": "bounded",
        "truncated": True,
        "totals": {"affected": "total_affected", "by_module": "total_modules"},
    }
    good = {
        "limit": 2,
        "truncated": True,
        "affected": [1, 2],
        "total_affected": 9,
        "by_module": {"m": 1},
        "total_modules": 1,
    }
    case = _case(checker, [check])
    assert checker.evaluate_case(case, good, 10, 1000).status == "PASS"
    too_long = {**good, "affected": [1, 2, 3]}
    assert checker.evaluate_case(case, too_long, 10, 1000).status == "FAIL"
    # An unbounded, pre-limit response has no `limit` at all.
    legacy = {"affected": [1, 2, 3]}
    assert checker.evaluate_case(case, legacy, 10, 1000).status == "FAIL"


def test_global_and_per_case_byte_budget(checker):
    case = _case(checker, [{"type": "count", "field": "results", "min": 0}])
    assert checker.evaluate_case(case, _results(), 999, 1000).status == "PASS"
    over = checker.evaluate_case(case, _results(), 1001, 1000)
    assert over.status == "FAIL"
    assert "1001" in over.detail
    tight = _case(checker, [{"type": "max_bytes", "max": 10}])
    assert checker.evaluate_case(tight, _results(), 11, 1000).status == "FAIL"


def test_xfail_failure_is_not_a_regression_and_pass_is_fixed(checker):
    xfail = {"issue": 39, "reason": "known gap"}
    checks = [
        {"type": "count", "field": "results", "min": 1},
        {"type": "count", "field": "results", "equals": 1, "xfail": xfail},
    ]
    case = _case(checker, checks)
    known = checker.evaluate_case(case, _results("a", "b"), 10, 1000)
    assert known.status == "XFAIL"
    assert known.issues == [39]
    fixed = checker.evaluate_case(case, _results("a"), 10, 1000)
    assert fixed.status == "FIXED"
    regressed = checker.evaluate_case(case, _results(), 10, 1000)
    assert regressed.status == "FAIL"


def test_exit_code_fails_only_on_regression_or_error(checker):
    def result(status):
        return checker.CaseResult(id=status, tool="t", status=status)

    calm = [result(s) for s in ("PASS", "XFAIL", "FIXED", "SKIP")]
    assert checker.exit_code(calm) == 0
    assert checker.exit_code([*calm, result("FAIL")]) == 1
    assert checker.exit_code([*calm, result("ERROR")]) == 1


def test_committed_fixture_loads_and_pins_the_benchmark_commits(checker):
    suite = checker.load_suite(FIXTURE)
    questions = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    assert suite.corpus.pinned_commits == questions["pinned_commits"]
    ids = [case.id for case in suite.cases]
    assert ids == [f"q0{n}" for n in range(1, 10)] + ["imports", "size"]
    assert suite.max_bytes == 131072
    # Each known gap names the issue that tracks it and why it fails today;
    # the set shrinks as issues land, so it is not pinned here.
    markers = [check.xfail for case in suite.cases for check in case.checks]
    assert all(m.issue > 0 and m.reason for m in markers if m is not None)


def test_offline_skips_semantic_cases(checker):
    case = _case(checker, [{"type": "count", "field": "results"}], semantic=True)
    result = checker.skip_result(case, "offline")
    assert result.status == "SKIP"
    assert checker.exit_code([result]) == 0


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _tiny_repository(root: Path) -> str:
    """A one-repository source tree with a call chain and a test."""
    repo = root / "alpha"
    (repo / "alpha").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "alpha" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "alpha" / "core.py").write_text(
        "def helper(value):\n    return value + 1\n\n\n"
        "def run(value):\n    return helper(value)\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_core.py").write_text(
        "from alpha.core import run\n\n\ndef test_run():\n    assert run(1) == 2\n",
        encoding="utf-8",
    )
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", ".", cwd=repo)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "init",
        cwd=repo,
    )
    # A later commit that the pin must NOT see.
    (repo / "alpha" / "late.py").write_text("def late():\n    pass\n")
    _git("add", ".", cwd=repo)
    _git(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "-m",
        "late",
        cwd=repo,
    )
    first = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD~1"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return first.stdout.strip()


@pytest.mark.slow
def test_end_to_end_on_a_tiny_pinned_corpus_is_deterministic(checker, tmp_path):
    sources = tmp_path / "sources"
    pinned = _tiny_repository(sources)
    suite = checker.Suite.model_validate(
        {
            "version": 1,
            "max_bytes": 131072,
            "corpus": {"github_owner": "nobody", "pinned_commits": {"alpha": pinned}},
            "cases": [
                {
                    "id": "callers",
                    "tool": "impact",
                    "args": {"target": "alpha.core.helper"},
                    "checks": [
                        {
                            "type": "contains_all",
                            "field": "affected",
                            "key": "node.qualified_name",
                            "values": ["core.run"],
                        },
                        {
                            "type": "bounded",
                            "truncated": False,
                            "totals": {"affected": "total_affected"},
                        },
                    ],
                },
                {
                    "id": "pinned",
                    "tool": "search",
                    "args": {"query": "late", "limit": 5},
                    "checks": [
                        {
                            "type": "excludes_any",
                            "field": "results",
                            "key": "node.path",
                            "values": ["alpha/alpha/late.py"],
                        },
                    ],
                },
                {
                    "id": "semantic",
                    "tool": "search",
                    "semantic": True,
                    "args": {"query": "helper"},
                    "checks": [{"type": "count", "field": "results", "min": 1}],
                },
            ],
        }
    )
    workdir = tmp_path / "cache"
    options = {"source_root": sources, "offline": True}
    first = checker.run_suite(suite, workdir, **options)
    second = checker.run_suite(suite, workdir, **options)
    statuses = {result.id: result.status for result in first}
    assert statuses == {"callers": "PASS", "pinned": "PASS", "semantic": "SKIP"}
    assert checker.render_table(first) == checker.render_table(second)
    corpus = workdir / "corpus" / "alpha"
    head = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=corpus,
        check=True,
        capture_output=True,
        text=True,
    )
    assert head.stdout.strip() == pinned
    markdown = checker.render_markdown(first)
    assert "| callers | impact | PASS |" in markdown


def test_where_filters_items_before_the_check(checker):
    payload = {
        "links": [
            {"from": {"path": "d/a.md"}, "to": {"path": "d/guide.md"}},
            {"from": {"path": "d/guide.md"}, "to": {"path": "d/a.md"}},
        ]
    }
    where = {"to.path": "d/guide.md"}
    inbound = {
        "type": "equals_set",
        "field": "links",
        "key": "from.path",
        "values": ["d/a.md"],
        "where": where,
    }
    count = {"type": "count", "field": "links", "equals": 1, "where": where}
    case = _case(checker, [inbound, count])
    assert checker.evaluate_case(case, payload, 10, 1000).status == "PASS"
    unfiltered = _case(checker, [{**inbound, "where": {}}])
    assert checker.evaluate_case(unfiltered, payload, 10, 1000).status == "FAIL"
