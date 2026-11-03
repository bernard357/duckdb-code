"""Measure whether duckdb-code makes Kilo Code cheaper or faster.

Puts a fixed question set to Kilo Code in two arms: without any MCP server
(arm A) and with duckdb-code serving a prebuilt index (arm B). Runs go through
the ``kilo`` binary bundled with the VS Code extension, so they use the same
engine as the editor, and every run is tagged ``[bench <question>-<arm>-<rep>]``
in its session title. Metrics are then read from Kilo Code's own session
database, opened read-only. The method is described in
``documentation/performance-workbook.md``.

Subcommands:

- ``run``: execute the planned runs (each one a real model call), appending
  each run's wall-clock time, startup included, to a local JSON-lines log.
- ``answers``: print each run's answer for one question, labelled by session
  id only, so it can be graded without knowing the arm.
- ``report``: write per-run metrics as CSV and per-question medians as a
  Markdown table. Neither output contains answer text.
"""

import argparse
import csv
import json
import os
import random
import re
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml
from pydantic import BaseModel

KILO_VERSION = "7.8.18"
DEFAULT_KILO = (
    Path.home()
    / f".vscode/extensions/kilocode.kilo-code-{KILO_VERSION}-darwin-arm64/bin/kilo"
)
DEFAULT_KILO_DB = Path.home() / ".local/share/kilo/kilo.db"
DEFAULT_QUESTIONS = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "benchmarks"
    / "kilo-questions.yaml"
)
# Wall-clock time per run, including Kilo Code and MCP server startup, which
# the session database does not record. Kept local (tmp/ is git-ignored).
DEFAULT_RUN_LOG = (
    Path(__file__).resolve().parent.parent / "tmp" / "kilo-benchmark-runs.jsonl"
)
DEFAULT_MODEL = "amazon-bedrock/global.anthropic.claude-opus-4-6-v1"
DEFAULT_AGENT = "ask"
DUCKDB_CODE_SPEC = "duckdb-code==261103"
ARMS = ("A", "B")
DUCKDB_CODE_PREFIX = "duckdb-code_"
KILO_DATA_DIR = (
    Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    / "kilo"
)
# Agent-level rules for the read-only `ask` agent, the same in both arms.
# Paths outside the workspace are denied (a search from the home folder then
# errors instead of prompting and ending the run), except Kilo's own
# tool-output and temporary folders, which it reads large tool outputs back
# from; they come after the catch-all so they win.
READ_ONLY_PERMISSIONS: dict[str, object] = {
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "webfetch": "deny",
    "websearch": "deny",
    "external_directory": {
        "*": "deny",
        f"{KILO_DATA_DIR}/tool-output/*": "allow",
        f"{tempfile.gettempdir()}/kilo/*": "allow",
    },
}
# A run counts toward the medians only with this status in the run log.
OK_STATUS = "rc=0"
TAG = re.compile(r"^\[bench (?P<question>[a-z0-9]+)-(?P<arm>[AB])-(?P<rep>\d+)\]$")


class RunTag(BaseModel):
    """The question, arm, and repetition encoded in a session title."""

    question: str
    arm: str
    rep: int


class PlannedRun(BaseModel):
    """One run to execute: a question, in one arm, at one repetition."""

    question: str
    arm: str
    rep: int

    @property
    def title(self) -> str:
        return f"[bench {self.question}-{self.arm}-{self.rep}]"


class RunMetrics(BaseModel):
    """What one run cost, read from Kilo Code's session database."""

    question: str
    arm: str
    rep: int
    session_id: str = ""
    model: str = ""
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    context_tokens: int = 0
    generated_tokens: int = 0
    cost: float = 0.0
    latency_ms: int = 0
    wall_ms: int = 0
    status: str = ""
    tool_calls: dict[str, int] = {}
    duckdb_code_calls: int = 0
    tool_errors: int = 0
    grade: int | None = None

    @property
    def title(self) -> str:
        return f"[bench {self.question}-{self.arm}-{self.rep}]"


class QuestionSummary(BaseModel):
    """Medians per arm for one question, and arm B's change from arm A."""

    question: str
    runs: dict[str, int]
    failed: dict[str, int]
    median_context_tokens: dict[str, float]
    median_generated_tokens: dict[str, float]
    median_cost: dict[str, float]
    median_latency_ms: dict[str, float]
    median_wall_ms: dict[str, float]
    median_steps: dict[str, float]
    median_duckdb_code_calls: dict[str, float]
    median_grade: dict[str, float | None]
    context_delta_pct: float | None
    latency_delta_pct: float | None


def parse_tag(title: str) -> RunTag | None:
    """Read the question, arm, and repetition from a benchmark session title."""
    match = TAG.match(title or "")
    if match is None:
        return None
    return RunTag(question=match["question"], arm=match["arm"], rep=int(match["rep"]))


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)


def _tagged_sessions(
    con: sqlite3.Connection, questions: set[str] | None
) -> list[tuple[str, int, RunTag]]:
    """Return (session id, created time, tag) for the benchmark sessions."""
    rows = con.execute(
        "SELECT id, time_created, title FROM session WHERE title LIKE '[bench %' "
        "ORDER BY time_created"
    ).fetchall()
    # A rerun reuses its title: keep only the most recent session per title.
    latest: dict[str, tuple[str, int, RunTag]] = {}
    for session_id, created, title in rows:
        tag = parse_tag(title)
        if tag is None or (questions is not None and tag.question not in questions):
            continue
        latest[title] = (session_id, created, tag)
    return list(latest.values())


def _run_metrics(
    con: sqlite3.Connection, session_id: str, created: int, tag: RunTag
) -> RunMetrics:
    metrics = RunMetrics(
        question=tag.question, arm=tag.arm, rep=tag.rep, session_id=session_id
    )
    start: int | None = None
    end = 0
    for message_created, raw in con.execute(
        "SELECT time_created, data FROM message WHERE session_id = ? "
        "ORDER BY time_created",
        [session_id],
    ):
        data = json.loads(raw)
        role = data.get("role")
        if role == "user":
            if start is None or message_created < start:
                start = message_created
            continue
        if role != "assistant":
            continue
        metrics.steps += 1
        metrics.model = metrics.model or data.get("modelID", "")
        tokens = data.get("tokens") or {}
        cache = tokens.get("cache") or {}
        metrics.input_tokens += int(tokens.get("input") or 0)
        metrics.output_tokens += int(tokens.get("output") or 0)
        metrics.reasoning_tokens += int(tokens.get("reasoning") or 0)
        metrics.cache_read_tokens += int(cache.get("read") or 0)
        metrics.cache_write_tokens += int(cache.get("write") or 0)
        metrics.cost += float(data.get("cost") or 0.0)
        completed = (data.get("time") or {}).get("completed")
        if completed:
            end = max(end, int(completed))
    metrics.context_tokens = (
        metrics.input_tokens + metrics.cache_read_tokens + metrics.cache_write_tokens
    )
    metrics.generated_tokens = metrics.output_tokens + metrics.reasoning_tokens
    if end:
        metrics.latency_ms = end - (created if start is None else start)
    tool_calls: dict[str, int] = {}
    for (raw,) in con.execute(
        "SELECT data FROM part WHERE session_id = ? "
        "AND json_extract(data, '$.type') = 'tool'",
        [session_id],
    ):
        data = json.loads(raw)
        name = data.get("tool") or "?"
        tool_calls[name] = tool_calls.get(name, 0) + 1
        if (data.get("state") or {}).get("status") == "error":
            metrics.tool_errors += 1
    metrics.tool_calls = tool_calls
    metrics.duckdb_code_calls = sum(
        count
        for name, count in tool_calls.items()
        if name.startswith(DUCKDB_CODE_PREFIX)
    )
    return metrics


def load_runs(db_path: Path, questions: set[str] | None = None) -> list[RunMetrics]:
    """Read the metrics of every tagged benchmark run, ordered by run."""
    con = _connect_read_only(db_path)
    try:
        runs = [
            _run_metrics(con, session_id, created, tag)
            for session_id, created, tag in _tagged_sessions(con, questions)
        ]
    finally:
        con.close()
    return sorted(runs, key=lambda r: (r.question, r.rep, r.arm, r.session_id))


def load_answers(db_path: Path, question: str) -> list[tuple[str, str]]:
    """Return (session id, final answer text) for every run of one question."""
    con = _connect_read_only(db_path)
    try:
        answers = []
        for session_id, _created, _tag in _tagged_sessions(con, {question}):
            row = con.execute(
                "SELECT id FROM message WHERE session_id = ? "
                "AND json_extract(data, '$.role') = 'assistant' "
                "ORDER BY time_created DESC LIMIT 1",
                [session_id],
            ).fetchone()
            text = ""
            if row is not None:
                parts = con.execute(
                    "SELECT json_extract(data, '$.text') FROM part "
                    "WHERE message_id = ? AND json_extract(data, '$.type') = 'text' "
                    "ORDER BY time_created",
                    [row[0]],
                ).fetchall()
                text = "\n".join(part for (part,) in parts if part)
            answers.append((session_id, text))
    finally:
        con.close()
    return answers


def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _delta_pct(baseline: float, value: float) -> float | None:
    if baseline <= 0:
        return None
    return (value - baseline) / baseline * 100.0


def _failed(run: RunMetrics) -> bool:
    """A run failed when the run log recorded anything but a clean exit.

    A run with no log entry (status unknown) is kept, so metrics read from the
    session database alone still summarize.
    """
    return bool(run.status) and run.status != OK_STATUS


def summarize(runs: list[RunMetrics]) -> list[QuestionSummary]:
    """Group runs by question and report per-arm medians and B's change.

    Failed runs are counted per arm but left out of every median, so a run
    that ended early cannot pass for a cheap one.
    """
    by_question: dict[str, list[RunMetrics]] = {}
    for run in runs:
        by_question.setdefault(run.question, []).append(run)
    summaries = []
    for question in sorted(by_question):
        group = by_question[question]
        failed = {
            arm: sum(1 for r in group if r.arm == arm and _failed(r)) for arm in ARMS
        }
        per_arm = {
            arm: [r for r in group if r.arm == arm and not _failed(r)] for arm in ARMS
        }

        def medians(field: str, arms=per_arm) -> dict[str, float]:
            return {
                arm: _median([float(getattr(r, field)) for r in rs])
                for arm, rs in arms.items()
                if rs
            }

        grades: dict[str, float | None] = {}
        for arm, rs in per_arm.items():
            graded = [float(r.grade) for r in rs if r.grade is not None]
            if rs:
                grades[arm] = _median(graded) if graded else None
        context = medians("context_tokens")
        latency = medians("latency_ms")
        both = "A" in context and "B" in context
        summaries.append(
            QuestionSummary(
                question=question,
                runs={arm: len(rs) for arm, rs in per_arm.items() if rs},
                failed={arm: n for arm, n in failed.items() if n},
                median_context_tokens=context,
                median_generated_tokens=medians("generated_tokens"),
                median_cost=medians("cost"),
                median_latency_ms=latency,
                median_wall_ms=medians("wall_ms"),
                median_steps=medians("steps"),
                median_duckdb_code_calls=medians("duckdb_code_calls"),
                median_grade=grades,
                context_delta_pct=_delta_pct(context["A"], context["B"])
                if both
                else None,
                latency_delta_pct=_delta_pct(latency["A"], latency["B"])
                if both
                else None,
            )
        )
    return summaries


def plan_runs(questions: list[str], reps: int) -> list[PlannedRun]:
    """Order the runs repetition by repetition, alternating arm A and arm B."""
    return [
        PlannedRun(question=question, arm=arm, rep=rep)
        for rep in range(1, reps + 1)
        for question in questions
        for arm in ARMS
    ]


def arm_overlay(arm: str, *, db: str, uvx: str, spec: str = DUCKDB_CODE_SPEC) -> dict:
    """The KILO_CONFIG_CONTENT overlay that sets one arm's MCP servers.

    The overlay is deep-merged over the global Kilo configuration, so each
    arm states every server it cares about rather than relying on what the
    global file holds. Both arms disable the stale ``duckdb-graph`` server.
    Arm A also disables ``duckdb-code``, which the setup procedure adds to the
    global file and would otherwise leak into the baseline (a bare
    ``enabled: false`` entry is accepted even where no global entry exists).
    Arm B sets ``duckdb-code`` in full, serving a prebuilt index without
    re-indexing on startup; its ``command`` replaces any global one.

    A non-interactive run rejects any tool call that would prompt, and the
    run then ends. So both arms give the read-only ``ask`` agent the same
    agent-level rules: the read-only file tools are allowed and the web tools
    are denied (a denied call returns an error and the run goes on, and the
    answer has to come from the local tree). Arm B also allows the
    duckdb-code tools.
    """
    permission = json.loads(json.dumps(READ_ONLY_PERMISSIONS))
    overlay: dict = {
        "mcp": {
            "duckdb-graph": {"enabled": False},
            "duckdb-code": {"enabled": False},
        },
        "agent": {"ask": {"permission": permission}},
    }
    if arm == "B":
        overlay["mcp"]["duckdb-code"] = {
            "type": "local",
            "command": [uvx, spec, "mcp", "--db", db, "--no-startup-index"],
            "enabled": True,
        }
        permission["duckdb-code_*"] = "allow"
    return overlay


def load_questions(path: Path) -> list[dict]:
    """Read the question set fixture and return its questions in order."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(data["questions"])


def load_run_log(path: Path) -> dict[str, dict]:
    """Read the run log: the last entry recorded for each session title.

    Each entry holds the run's ``status``, its ``wall_ms`` (startup included),
    and, for a failed run, the first ``error`` line Kilo printed. A run
    repeated under the same title is represented by its last entry, matching
    the session that ``load_runs`` keeps. A missing log yields no entries.
    """
    if not path.exists():
        return {}
    entries: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            entries[entry["title"]] = entry
    return entries


def first_error_line(stderr: str) -> str:
    """The first line of Kilo's stderr that reports a failure, shortened.

    Kept so a failed run can be diagnosed from the run log. It carries Kilo's
    own error text (for example a rejected permission), never answer text.
    """
    for line in stderr.splitlines():
        text = line.strip()
        if text.startswith("Error") or "rejected" in text or "denied" in text:
            return text[:200]
    return ""


def attach_run_log(runs: list[RunMetrics], entries: dict[str, dict]) -> None:
    """Set each run's wall-clock time and status from the run log, by title."""
    for run in runs:
        entry = entries.get(run.title)
        if entry is not None:
            run.wall_ms = int(entry.get("wall_ms", run.wall_ms))
            run.status = str(entry.get("status", run.status))


def load_grades(path: Path) -> dict[str, int]:
    """Read a ``session_id,grade`` CSV written while grading answers blind."""
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            row["session_id"]: int(row["grade"])
            for row in csv.DictReader(handle)
            if row.get("grade", "").strip()
        }


CSV_FIELDS = [
    "question",
    "arm",
    "rep",
    "session_id",
    "model",
    "steps",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "context_tokens",
    "generated_tokens",
    "cost",
    "latency_ms",
    "wall_ms",
    "status",
    "tool_calls",
    "duckdb_code_calls",
    "tool_errors",
    "grade",
]


def write_csv(runs: list[RunMetrics], out) -> None:
    """Write one row of metrics per run. No answer text is written."""
    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    writer.writeheader()
    for run in runs:
        row = run.model_dump()
        row["tool_calls"] = json.dumps(run.tool_calls, sort_keys=True)
        row["cost"] = f"{run.cost:.6f}"
        writer.writerow({field: row[field] for field in CSV_FIELDS})


def _fmt(value: float | None, digits: int = 0) -> str:
    if value is None:
        return "–"
    return f"{value:,.{digits}f}"


def _fmt_delta(value: float | None) -> str:
    return "–" if value is None else f"{value:+.0f}%"


def markdown_table(summaries: list[QuestionSummary]) -> str:
    """Render per-question medians and deltas as a Markdown table."""
    lines = [
        "| Question | Runs A/B | Failed A/B | Context tokens A | Context tokens B "
        "| Δ context "
        "| Latency A (s) | Latency B (s) | Δ latency | Wall A (s) | Wall B (s) "
        "| Cost A ($) | Cost B ($) | duckdb-code calls B | Grade A | Grade B |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- "
        "| --- | --- | --- | --- |",
    ]
    for s in summaries:
        lat = {arm: v / 1000 for arm, v in s.median_latency_ms.items()}
        wall = {arm: v / 1000 for arm, v in s.median_wall_ms.items() if v}
        lines.append(
            f"| {s.question} | {s.runs.get('A', 0)}/{s.runs.get('B', 0)} "
            f"| {s.failed.get('A', 0)}/{s.failed.get('B', 0)} "
            f"| {_fmt(s.median_context_tokens.get('A'))} "
            f"| {_fmt(s.median_context_tokens.get('B'))} "
            f"| {_fmt_delta(s.context_delta_pct)} "
            f"| {_fmt(lat.get('A'), 1)} | {_fmt(lat.get('B'), 1)} "
            f"| {_fmt_delta(s.latency_delta_pct)} "
            f"| {_fmt(wall.get('A'), 1)} | {_fmt(wall.get('B'), 1)} "
            f"| {_fmt(s.median_cost.get('A'), 3)} "
            f"| {_fmt(s.median_cost.get('B'), 3)} "
            f"| {_fmt(s.median_duckdb_code_calls.get('B'), 1)} "
            f"| {_fmt(s.median_grade.get('A'), 1)} "
            f"| {_fmt(s.median_grade.get('B'), 1)} |"
        )
    return "\n".join(lines) + "\n"


def _cmd_run(args: argparse.Namespace) -> int:
    questions = load_questions(Path(args.questions))
    prompts = {q["id"]: q["prompt"] for q in questions}
    selected = args.only.split(",") if args.only else list(prompts)
    unknown = [q for q in selected if q not in prompts]
    if unknown:
        print(f"unknown question ids: {', '.join(unknown)}", file=sys.stderr)
        return 2
    arms = set(args.arms.split(","))
    plan = [p for p in plan_runs(selected, args.reps) if p.arm in arms]
    uvx = args.uvx or shutil.which("uvx") or "uvx"
    db = str(Path(args.db).expanduser().resolve())
    workspace = str(Path(args.workspace).expanduser().resolve())
    log = Path(args.log)
    print(f"{len(plan)} runs, model {args.model}, agent {args.agent}")
    for index, run in enumerate(plan, start=1):
        overlay = arm_overlay(run.arm, db=db, uvx=uvx, spec=args.spec)
        command = [
            str(args.kilo),
            "run",
            "--model",
            args.model,
            "--agent",
            args.agent,
            "--dir",
            workspace,
            "--title",
            run.title,
            prompts[run.question],
        ]
        if args.dry_run:
            print(f"[{index}/{len(plan)}] {run.title} (dry run)")
            continue
        env = dict(os.environ, KILO_CONFIG_CONTENT=json.dumps(overlay))
        started = time.time()
        try:
            proc = subprocess.run(
                command,
                env=env,
                capture_output=True,
                text=True,
                timeout=args.timeout,
                check=False,
            )
            status = f"rc={proc.returncode}"
            stderr = proc.stderr
        except subprocess.TimeoutExpired:
            status = "timeout"
            stderr = ""
        elapsed = time.time() - started
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            entry = {
                "title": run.title,
                "status": status,
                "wall_ms": int(elapsed * 1000),
                "started_at": int(started * 1000),
            }
            if status != OK_STATUS:
                entry["error"] = first_error_line(stderr)
            handle.write(json.dumps(entry) + "\n")
        print(f"[{index}/{len(plan)}] {run.title} {status} {elapsed:.1f}s")
    return 0


def _cmd_answers(args: argparse.Namespace) -> int:
    answers = load_answers(Path(args.kilo_db), args.question)
    random.Random(args.seed).shuffle(answers)
    for session_id, text in answers:
        print(f"===== {session_id} =====")
        print(text.strip() or "(no answer text)")
        print()
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    questions = {q["id"] for q in load_questions(Path(args.questions))}
    runs = load_runs(Path(args.kilo_db), questions=questions)
    attach_run_log(runs, load_run_log(Path(args.log)))
    if args.grades:
        grades = load_grades(Path(args.grades))
        for run in runs:
            run.grade = grades.get(run.session_id)
    if args.csv:
        with Path(args.csv).open("w", newline="", encoding="utf-8") as handle:
            write_csv(runs, handle)
    table = markdown_table(summarize(runs))
    if args.markdown:
        Path(args.markdown).write_text(table, encoding="utf-8")
    else:
        sys.stdout.write(table)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="execute the planned runs (real model calls)")
    run.add_argument("--db", required=True, help="prebuilt duckdb-code index for arm B")
    run.add_argument("--workspace", default="~/Public", help="folder Kilo runs in")
    run.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    run.add_argument("--only", help="comma-separated question ids (default: all)")
    run.add_argument("--arms", default="A,B", help="comma-separated arms")
    run.add_argument("--reps", type=int, default=3)
    run.add_argument("--model", default=DEFAULT_MODEL)
    run.add_argument("--agent", default=DEFAULT_AGENT)
    run.add_argument("--spec", default=DUCKDB_CODE_SPEC, help="duckdb-code to run")
    run.add_argument("--kilo", default=str(DEFAULT_KILO))
    run.add_argument("--uvx", help="absolute uvx path (default: from PATH)")
    run.add_argument("--timeout", type=int, default=900, help="seconds per run")
    run.add_argument("--dry-run", action="store_true", help="print the plan only")
    run.add_argument("--log", default=str(DEFAULT_RUN_LOG), help="wall-time log")
    run.set_defaults(handler=_cmd_run)

    answers = sub.add_parser("answers", help="print one question's answers, blind")
    answers.add_argument("question")
    answers.add_argument("--kilo-db", default=str(DEFAULT_KILO_DB))
    answers.add_argument("--seed", type=int, default=0, help="shuffle seed")
    answers.set_defaults(handler=_cmd_answers)

    report = sub.add_parser("report", help="write metrics as CSV and Markdown")
    report.add_argument("--kilo-db", default=str(DEFAULT_KILO_DB))
    report.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    report.add_argument("--grades", help="session_id,grade CSV from blind grading")
    report.add_argument("--log", default=str(DEFAULT_RUN_LOG), help="wall-time log")
    report.add_argument("--csv", help="write per-run metrics here")
    report.add_argument("--markdown", help="write the summary table here")
    report.set_defaults(handler=_cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
