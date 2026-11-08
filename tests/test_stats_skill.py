import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest

SKILL = Path(__file__).resolve().parents[1] / "skills" / "stats" / "SKILL.md"
HEADER = (
    f"{'agent':32} {'runs':>4} {'med min':>8} {'med req':>8} {'med tools':>9} "
    f"{'med out tok':>11} {'all tok (M)':>11} {'7d vs prev':>11}"
)
AGENTS = ("agent-harness:implementer", "agent-harness:estimator", "Explore")
PROJECTS = ("shop", "platform")
VERSIONS = (None, "0.5.0", "0.6.0")
MODELS = (None, "claude-a", "claude-b")
NOW = datetime.now(UTC)


def script() -> str:
    found = re.search(r"<<'EOF'\n(.*?)\nEOF\n", SKILL.read_text(), re.DOTALL)
    assert found
    return found.group(1)


def stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def random_runs(seed: int, count: int = 120) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    runs = []
    for index in range(count):
        hours_ago = int(rng.integers(0, 24 * 30)) + 0.5
        run: dict[str, Any] = {
            "agent_id": f"a{int(rng.integers(0, count * 3 // 4))}",
            "agent_type": AGENTS[int(rng.integers(0, len(AGENTS)))],
            "project": PROJECTS[int(rng.integers(0, len(PROJECTS)))],
            "model": MODELS[int(rng.integers(0, len(MODELS)))],
            "ended": stamp(NOW - timedelta(hours=hours_ago)),
            "seconds": None if index % 17 == 0 else float(rng.integers(10, 3000)),
            "requests": int(rng.integers(1, 80)),
            "tool_uses": int(rng.integers(0, 120)),
            "tokens": {
                "input_tokens": int(rng.integers(0, 5000)),
                "output_tokens": int(rng.integers(100, 40000)),
                "cache_read_input_tokens": int(rng.integers(0, 2_000_000)),
            },
        }
        version = VERSIONS[int(rng.integers(0, len(VERSIONS)))]
        if version is not None:
            run["plugin_version"] = version
        runs.append(run)
    return runs


def stats(
    tmp_path: Path, runs: list[dict[str, Any]], *args: str, tz: str = "UTC"
) -> subprocess.CompletedProcess[str]:
    log = tmp_path / "runs.jsonl"
    log.write_text("".join(json.dumps(run) + "\n" for run in runs) + "not json\n")
    return subprocess.run(  # noqa: S603
        [sys.executable, "-", *args],
        input=script(),
        capture_output=True,
        text=True,
        env={**os.environ, "HARNESS_RUNS_LOG": str(log), "TZ": tz},
        check=False,
    )


def table(tmp_path: Path, runs: list[dict[str, Any]], *args: str) -> list[str]:
    result = stats(tmp_path, runs, *args)
    assert (result.returncode, result.stderr) == (0, "")
    return result.stdout.splitlines()


def median(values: list[Any]) -> float:
    present = sorted(v for v in values if v is not None)
    if not present:
        return 0
    middle = len(present) // 2
    if len(present) % 2:
        return float(present[middle])
    return float(present[middle - 1] + present[middle]) / 2


def latest(runs: list[dict[str, Any]], wanted: str = "") -> dict[str, list[dict[str, Any]]]:
    last: dict[str, dict[str, Any]] = {}
    for run in runs:
        if wanted in run["project"] or wanted in run["agent_type"]:
            last[run["agent_id"]] = run
    per_agent: dict[str, list[dict[str, Any]]] = {}
    for run in last.values():
        per_agent.setdefault(run["agent_type"], []).append(run)
    return per_agent


def medians(runs: list[dict[str, Any]]) -> list[float]:
    return [
        median([r["seconds"] for r in runs]) / 60,
        median([r["requests"] for r in runs]),
        median([r["tool_uses"] for r in runs]),
        median([r["tokens"]["output_tokens"] for r in runs]),
    ]


def cells(values: list[float]) -> list[str]:
    return [f"{values[0]:.1f}", *(f"{v:.0f}" for v in values[1:])]


def ended(run: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(run["ended"].replace("Z", "+00:00"))


@pytest.mark.parametrize("args", [(), ("",), ("implementer",), ("shop",)])
def test_without_by_or_split_the_table_stays_as_it_was(tmp_path, args):
    runs = random_runs(seed=1)
    expected = {}
    for agent, items in latest(runs, "".join(args)).items():
        week = [r["seconds"] for r in items if r["seconds"] and ended(r) > NOW - timedelta(days=7)]
        prev = [
            r["seconds"]
            for r in items
            if r["seconds"] and NOW - timedelta(days=14) < ended(r) <= NOW - timedelta(days=7)
        ]
        trend = f"{median(week) / median(prev) - 1:+.0%}" if week and prev else "-"
        total = sum(sum(r["tokens"].values()) for r in items) / 1e6
        expected[agent] = [str(len(items)), *cells(medians(items)), f"{total:.1f}", trend]

    [header, *rows] = table(tmp_path, runs, *args)

    assert header == HEADER
    assert {row.split()[0]: row.split()[1:] for row in rows} == expected
    counts = [int(row.split()[1]) for row in rows]
    assert counts == sorted(counts, reverse=True)


@pytest.mark.parametrize(
    ("field", "args"),
    [
        ("plugin_version", ("--by version",)),
        ("plugin_version", ("--by", "version")),
        ("model", ("--by model",)),
        ("model", ("--by model shop",)),
        ("model", ("shop --by model",)),
    ],
)
def test_by_groups_each_agent_by_version_or_model(tmp_path, field, args):
    runs = random_runs(seed=2)
    wanted = "shop" if "shop" in " ".join(args) else ""
    expected = {}
    for agent, items in latest(runs, wanted).items():
        groups: dict[str, list[dict[str, Any]]] = {}
        for run in items:
            groups.setdefault(run.get(field) or "unknown", []).append(run)
        for group, members in groups.items():
            expected[agent, group] = [str(len(members)), *cells(medians(members))]

    [header, *rows] = table(tmp_path, runs, *args)

    assert header.split()[:2] == ["agent", "version" if field == "plugin_version" else "model"]
    assert {tuple(row.split()[:2]): row.split()[2:] for row in rows} == expected


@pytest.mark.parametrize(
    ("split", "moment"),
    [
        ("2026-01-01", datetime(2026, 1, 1, tzinfo=UTC)),
        (stamp(NOW - timedelta(days=12)), NOW - timedelta(days=12)),
        (
            (NOW - timedelta(days=5)).astimezone(timezone(timedelta(hours=2))).isoformat(),
            NOW - timedelta(days=5),
        ),
    ],
)
def test_split_compares_each_agent_before_and_after(tmp_path, split, moment):
    runs = random_runs(seed=3)
    expected = {}
    for agent, items in latest(runs).items():
        before = [r for r in items if ended(r) < moment]
        after = [r for r in items if ended(r) >= moment]
        for side, members in (("before", before), ("after", after)):
            if members:
                expected[agent, side] = [str(len(members)), *cells(medians(members))]
        if before and after:
            changes = zip(medians(after), medians(before), strict=True)
            expected[agent, "change"] = [f"{a / b - 1:+.0%}" if b else "-" for a, b in changes]

    [header, *rows] = table(tmp_path, runs, f"--split {split}")

    assert header.split()[:2] == ["agent", "split"]
    assert {tuple(row.split()[:2]): row.split()[2:] for row in rows} == expected
    assert any(key[1] == "change" for key in expected) or split == "2026-01-01"


def run(agent_id: str, end: str, seconds: float, version: str | None = None) -> dict[str, Any]:
    line: dict[str, Any] = {
        "agent_id": agent_id,
        "agent_type": "agent-harness:implementer",
        "project": "shop",
        "model": "claude-a",
        "ended": end,
        "seconds": seconds,
        "requests": 10,
        "tool_uses": 20,
        "tokens": {"output_tokens": 1000},
    }
    if version:
        line["plugin_version"] = version
    return line


EXAMPLE = [
    run("a1", "2026-03-02T12:00:00Z", 1500, "0.6.0"),
    run("a2", "2026-02-01T12:00:00Z", 600),
    run("a3", "2026-02-20T12:00:00Z", 1200, "0.5.0"),
    run("a4", "2026-02-03T12:00:00Z", 1800),
    run("a5", "2026-03-01T02:00:00Z", 300, "0.6.0"),
]


def test_groups_follow_the_order_they_first_ran_in(tmp_path):
    assert table(tmp_path, EXAMPLE, "--by version") == [
        "agent                            version                  runs  med min  med req "
        "med tools med out tok",
        "agent-harness:implementer        unknown                     2     20.0       10 "
        "       20        1000",
        "agent-harness:implementer        0.5.0                       1     20.0       10 "
        "       20        1000",
        "agent-harness:implementer        0.6.0                       2     15.0       10 "
        "       20        1000",
    ]


@pytest.mark.parametrize(
    ("tz", "split", "before", "change"),
    [
        ("UTC", "2026-03-01", ["3", "20.0"], "-25%"),
        ("EST+5", "2026-03-01", ["4", "15.0"], "+67%"),
        ("EST+5", "2026-03-01T02:00:00Z", ["3", "20.0"], "-25%"),
    ],
)
def test_the_split_is_local_time_unless_it_has_a_zone(tmp_path, tz, split, before, change):
    result = stats(tmp_path, EXAMPLE, f"--split {split}", tz=tz)

    [_, before_row, _, change_row] = result.stdout.splitlines()
    assert before_row.split()[1:4] == ["before", *before]
    assert change_row.split()[1:] == ["change", change, "+0%", "+0%", "+0%"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ("--by", "--by needs a value."),
        ("shop --split", "--split needs a value."),
        ("--by colour", "--by takes version or model, not colour."),
        ("--split yesterday", "--split takes an ISO date or datetime, not yesterday."),
        ("--by version --split 2026-01-01", "Use --by or --split, not both."),
    ],
)
def test_bad_arguments_stop_with_a_message(tmp_path, args, message):
    result = stats(tmp_path, EXAMPLE, args)

    assert (result.returncode, result.stdout, result.stderr.strip()) == (1, "", message)
