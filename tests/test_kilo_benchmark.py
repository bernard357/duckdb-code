"""Tests for the Kilo Code benchmark harness (scripts/kilo_benchmark.py).

The harness reads Kilo Code's session database read-only. These tests build a
synthetic SQLite file with the same tables and JSON fields (``session``,
``message``, ``part``) and check session selection by title tag, per-run
metrics, per-question medians, the interleaved run plan, and the per-arm
configuration overlays. No model is called.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "kilo_benchmark.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("kilo_benchmark", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["kilo_benchmark"] = module
    spec.loader.exec_module(module)
    return module


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE session (id TEXT, title TEXT, time_created INTEGER);
        CREATE TABLE message (
            id TEXT, session_id TEXT, time_created INTEGER, data TEXT
        );
        CREATE TABLE part (
            id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER,
            data TEXT
        );
        """
    )
    con.commit()
    con.close()


def _add_session(path, sid, title, start, steps, tools=()):
    """Add one session: a user message at ``start``, then assistant steps.

    ``steps`` is a list of (input, output, reasoning, cache_read, cache_write,
    cost, completed_ms) tuples; ``tools`` a list of (tool_name, status).
    """
    con = sqlite3.connect(path)
    con.execute("INSERT INTO session VALUES (?, ?, ?)", [sid, title, start])
    con.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        [f"{sid}-u", sid, start, json.dumps({"role": "user"})],
    )
    for i, (inp, out, rea, cr, cw, cost, done) in enumerate(steps):
        data = {
            "role": "assistant",
            "modelID": "global.anthropic.claude-opus-4-6-v1",
            "tokens": {
                "input": inp,
                "output": out,
                "reasoning": rea,
                "cache": {"read": cr, "write": cw},
            },
            "time": {"created": start + i, "completed": done},
            "cost": cost,
        }
        con.execute(
            "INSERT INTO message VALUES (?, ?, ?, ?)",
            [f"{sid}-a{i}", sid, start + i, json.dumps(data)],
        )
    for j, (name, status) in enumerate(tools):
        part = {"type": "tool", "tool": name, "state": {"status": status}}
        con.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?)",
            [f"{sid}-p{j}", f"{sid}-a0", sid, start + j, json.dumps(part)],
        )
    con.commit()
    con.close()


@pytest.fixture
def kilo_db(tmp_path):
    db = tmp_path / "kilo.db"
    _make_db(db)
    # q01, arm A: two steps, built-in tools only.
    _add_session(
        db,
        "s1",
        "[bench q01-A-1]",
        1_000,
        [(100, 10, 5, 2_000, 300, 0.10, 4_000), (50, 20, 0, 2_100, 0, 0.05, 9_000)],
        [("bash", "completed"), ("read", "completed"), ("read", "error")],
    )
    # q01, arm B: one step, one duckdb-code call.
    _add_session(
        db,
        "s2",
        "[bench q01-B-1]",
        20_000,
        [(40, 8, 2, 1_000, 100, 0.03, 23_000)],
        [("duckdb-code_search", "completed")],
    )
    # Ignored: untagged, smoke run, and a question not in the set.
    _add_session(db, "s3", "a normal session", 30_000, [(1, 1, 0, 0, 0, 0.0, 31_000)])
    _add_session(db, "s4", "[bench smoke-B-3]", 40_000, [(1, 1, 0, 0, 0, 0.0, 41_000)])
    _add_session(db, "s5", "[bench q99-A-1]", 50_000, [(1, 1, 0, 0, 0, 0.0, 51_000)])
    return db


def test_parse_tag_reads_question_arm_and_rep(bench):
    tag = bench.parse_tag("[bench q07-B-3]")
    assert (tag.question, tag.arm, tag.rep) == ("q07", "B", 3)
    assert bench.parse_tag("[bench q07-C-1]") is None
    assert bench.parse_tag("bench q07-A-1") is None


def test_load_runs_selects_only_tagged_runs_of_known_questions(bench, kilo_db):
    runs = bench.load_runs(kilo_db, questions={"q01", "q02"})
    assert sorted((r.question, r.arm, r.rep) for r in runs) == [
        ("q01", "A", 1),
        ("q01", "B", 1),
    ]


def test_run_metrics_sum_tokens_and_cost_and_measure_latency(bench, kilo_db):
    runs = {r.arm: r for r in bench.load_runs(kilo_db, questions={"q01"})}
    a = runs["A"]
    assert a.input_tokens == 150
    assert a.output_tokens == 30
    assert a.reasoning_tokens == 5
    assert a.cache_read_tokens == 4_100
    assert a.cache_write_tokens == 300
    # Context = input + cache read + cache write; generated = output + reasoning.
    assert a.context_tokens == 150 + 4_100 + 300
    assert a.generated_tokens == 35
    assert a.cost == pytest.approx(0.15)
    # From the user message to the last completed assistant step.
    assert a.latency_ms == 9_000 - 1_000
    assert a.steps == 2
    assert a.model == "global.anthropic.claude-opus-4-6-v1"


def test_tool_calls_split_duckdb_code_from_built_in(bench, kilo_db):
    runs = {r.arm: r for r in bench.load_runs(kilo_db, questions={"q01"})}
    assert runs["A"].tool_calls == {"bash": 1, "read": 2}
    assert runs["A"].duckdb_code_calls == 0
    assert runs["A"].tool_errors == 1
    assert runs["B"].tool_calls == {"duckdb-code_search": 1}
    assert runs["B"].duckdb_code_calls == 1


def test_summarize_reports_medians_and_arm_deltas(bench):
    def run(arm, rep, context, latency):
        return bench.RunMetrics(
            question="q01",
            arm=arm,
            rep=rep,
            context_tokens=context,
            latency_ms=latency,
        )

    runs = [
        run("A", 1, 1_000, 10_000),
        run("A", 2, 3_000, 30_000),
        run("A", 3, 2_000, 20_000),
        run("B", 1, 600, 9_000),
        run("B", 2, 400, 11_000),
        run("B", 3, 500, 10_000),
    ]
    (summary,) = bench.summarize(runs)
    assert summary.question == "q01"
    assert summary.runs == {"A": 3, "B": 3}
    assert summary.median_context_tokens == {"A": 2_000, "B": 500}
    assert summary.median_latency_ms == {"A": 20_000, "B": 10_000}
    assert summary.context_delta_pct == pytest.approx(-75.0)
    assert summary.latency_delta_pct == pytest.approx(-50.0)


def test_plan_runs_interleaves_arms_and_tags_every_run(bench):
    plan = bench.plan_runs(["q01", "q02"], reps=2)
    assert [(p.question, p.arm, p.rep) for p in plan] == [
        ("q01", "A", 1),
        ("q01", "B", 1),
        ("q02", "A", 1),
        ("q02", "B", 1),
        ("q01", "A", 2),
        ("q01", "B", 2),
        ("q02", "A", 2),
        ("q02", "B", 2),
    ]
    assert plan[1].title == "[bench q01-B-1]"


def test_overlays_differ_only_in_the_duckdb_code_server(bench):
    a = bench.arm_overlay("A", db="/abs/public.duckdb", uvx="/opt/uvx")
    b = bench.arm_overlay("B", db="/abs/public.duckdb", uvx="/opt/uvx")
    # Both arms disable the stale duckdb-graph server.
    assert a["mcp"]["duckdb-graph"] == {"enabled": False}
    assert b["mcp"]["duckdb-graph"] == {"enabled": False}
    assert "duckdb-code" not in a["mcp"]
    assert "permission" not in a
    server = b["mcp"]["duckdb-code"]
    assert server["type"] == "local"
    assert server["enabled"] is True
    assert server["command"][:2] == ["/opt/uvx", "duckdb-code==261101"]
    assert server["command"][2:] == [
        "mcp",
        "--db",
        "/abs/public.duckdb",
        "--no-startup-index",
    ]
    # Arm B lets the read-only `ask` agent call the tools without prompting.
    assert b["agent"]["ask"]["permission"] == {"duckdb-code_*": "allow"}


def test_wall_times_come_from_the_run_log_last_attempt_wins(bench, tmp_path):
    log = tmp_path / "runs.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(entry)
            for entry in [
                {"title": "[bench q01-A-1]", "status": "rc=0", "wall_ms": 12_000},
                {"title": "[bench q01-B-1]", "status": "timeout", "wall_ms": 900_000},
                {"title": "[bench q01-B-1]", "status": "rc=0", "wall_ms": 15_000},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    walls = bench.load_wall_times(log)
    assert walls == {"[bench q01-A-1]": 12_000, "[bench q01-B-1]": 15_000}
    runs = [
        bench.RunMetrics(question="q01", arm="A", rep=1, latency_ms=8_000),
        bench.RunMetrics(question="q01", arm="B", rep=1, latency_ms=9_000),
    ]
    bench.attach_wall_times(runs, walls)
    assert [r.wall_ms for r in runs] == [12_000, 15_000]
    (summary,) = bench.summarize(runs)
    assert summary.median_wall_ms == {"A": 12_000, "B": 15_000}


def test_missing_run_log_leaves_wall_times_unset(bench, tmp_path):
    assert bench.load_wall_times(tmp_path / "absent.jsonl") == {}
