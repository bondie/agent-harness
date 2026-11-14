import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from agent_harness import activetime, cli

T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC).timestamp()


def iso(offset: float) -> str:
    return datetime.fromtimestamp(T0 + offset, UTC).isoformat().replace("+00:00", "Z")


def record(offset: float, kind: str = "assistant", **content: Any) -> dict[str, Any]:
    items = [content] if content else []
    return {"type": kind, "timestamp": iso(offset), "message": {"content": items}}


def write_lines(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


@pytest.fixture
def setup(tmp_path, monkeypatch) -> dict[str, Path]:
    projects = tmp_path / "claude" / "projects"
    lead = write_lines(
        projects / "-work-shop" / "s-1.jsonl",
        [
            record(0, "user"),
            record(60, type="tool_use", id="x"),
            {"type": "progress", "timestamp": iso(70)},
            record(1260, "user", type="tool_result", tool_use_id="x"),
            record(1300),
            "not a record",  # type: ignore[list-item]
            record(8500, "user"),
            record(8560),
        ],
    )
    write_lines(
        projects / "-work-shop" / "s-1" / "subagents" / "agent-a1.jsonl",
        [record(100, "user"), record(200), record(400)],
    )
    write_lines(
        projects / "-work-other" / "s-2" / "subagents" / "agent-a9.jsonl",
        [record(5000, "user"), record(5100)],
    )
    write_lines(
        projects / "-work-other" / "s-2" / "subagents" / "agent-u8.jsonl",
        [record(6000, "user"), record(6100)],
    )
    work = write_lines(
        tmp_path / "harness" / "work.jsonl",
        [
            {"session": "s-1", "ticket": "SHOP-1", "at": iso(0), "transcript": str(lead)},
            {"session": "s-1", "ticket": "SHOP-2", "at": iso(8000), "transcript": str(lead)},
            {"session": "s-9", "ticket": None, "at": "yesterday"},
        ],
    )
    runs = write_lines(
        tmp_path / "runs.jsonl",
        [
            {"agent_id": "a9", "session": "s-2", "ticket": "SHOP-1"},
            {"agent_id": "a9", "session": "s-2", "ticket": "SHOP-1"},
            {"agent_id": "u8", "session": "s-2", "ticket": "UTF-8"},
        ],
    )
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "harness"))
    monkeypatch.setenv("HARNESS_RUNS_LOG", str(runs))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return {"work": work, "runs": runs, "projects": projects}


def test_the_lead_counts_only_inside_its_window_and_a_running_tool_bridges_a_gap(setup):
    active = activetime.measure("SHOP-1")

    assert active.lead_seconds == 1300
    assert active.agent_seconds == 400
    assert active.total_seconds == 1400
    assert (active.sessions, active.agents) == (1, 2)
    assert (active.first, active.last) == (T0, T0 + 5100)


def test_the_next_marker_starts_the_next_ticket(setup):
    active = activetime.measure("SHOP-2")

    assert active.lead_seconds == 60
    assert active.agent_seconds == 0


def test_a_shorter_idle_threshold_counts_only_the_running_tool(setup):
    assert activetime.measure("SHOP-1", idle=30).lead_seconds == 1200


def test_a_ticket_without_markers_or_runs_has_no_time(setup):
    active = activetime.measure("SHOP-3")

    assert active.total_seconds == 0
    assert active.as_json()["first"] is None


def test_union_counts_overlaps_once():
    assert activetime.union([(0, 10), (5, 20), (30, 40), (31, 35)]) == 30


def test_the_command_prints_the_hours(setup, capsys):
    assert cli.main(["active-time", "SHOP-1"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SHOP-1: 0.39 h active (lead 0.36 h, agents 0.11 h")
    assert "in 1 session(s) and 2 agent run(s)" in out

    assert cli.main(["active-time", "SHOP-1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["total_hours"] == 0.39
    assert data["idle_minutes"] == 5

    assert cli.main(["active-time", "SHOP-3"]) == 0
    assert "no active time" in capsys.readouterr().out

    assert cli.main(["active-time", "SHOP-1", "--idle", "0"]) == 1
    assert cli.main(["active-time", "SHOP-1", "--away", "-1"]) == 1


def test_export_case_freezes_the_active_time(setup, capsys):
    ticket = setup["work"].parent / "tickets" / "SHOP-1"
    ticket.mkdir(parents=True)
    estimate = {"ticket": "SHOP-1", "mode": "reference", "reference_median": 1.0, "total": 1.0}
    (ticket / "estimate.json").write_text(json.dumps(estimate), encoding="utf-8")

    arguments = ["export-case", "SHOP-1", "--source", "worklog", "--actual-total", "1"]
    assert cli.main(arguments) == 0

    case = json.loads((setup["work"].parent / "eval-cases" / "SHOP-1.json").read_text("utf-8"))
    outcome = json.loads((ticket / "outcome.json").read_text("utf-8"))
    assert case["active_time"]["total_hours"] == 0.39
    assert outcome["active_time"] == case["active_time"]
    assert list(case["active_time"]["total_hours_by_idle"]) == ["3", "5", "10"]


def test_a_run_filed_under_another_key_does_not_count_in_the_window(setup):
    write_lines(
        setup["projects"] / "-work-shop" / "s-1" / "subagents" / "agent-b2.jsonl",
        [record(2000, "user"), record(2100)],
    )
    with setup["runs"].open("a", encoding="utf-8") as log:
        log.write(json.dumps({"agent_id": "b2", "session": "s-1", "ticket": "SHOP-7: review"}))
        log.write("\n")

    assert activetime.measure("SHOP-1").agent_seconds == 400
    assert activetime.measure("SHOP-7").agent_seconds == 100


def test_a_question_to_the_person_and_a_tool_past_the_cap_do_not_count():
    question = [
        (0.0, record(0, type="tool_use", id="q", name="AskUserQuestion")),
        (900.0, record(900, "user", type="tool_result", tool_use_id="q")),
    ]
    long_tool = [
        (0.0, record(0, type="tool_use", id="t", name="Bash")),
        (7200.0, record(7200, "user", type="tool_result", tool_use_id="t")),
    ]

    assert activetime.busy(question, idle=300, tool_cap=3600) == []
    assert activetime.busy(long_tool, idle=300, tool_cap=3600) == []
    assert activetime.busy(long_tool, idle=300, tool_cap=7200) == [(0.0, 7200.0)]


@given(
    intervals=st.lists(
        st.tuples(st.integers(0, 60), st.integers(0, 60)).map(sorted).map(tuple), max_size=12
    ),
    window=st.tuples(st.integers(0, 60), st.one_of(st.none(), st.integers(0, 60))),
)
def test_clip_and_union_match_counting_each_second(intervals, window):
    start, end = window
    clipped = activetime.clip(intervals, start, end)
    stop = 61 if end is None else end
    covered = {second for a, b in intervals for second in range(a, b) if start <= second < stop}

    assert activetime.union(clipped) == len(covered)


def prompt(offset: float, text: str = "go on") -> dict[str, Any]:
    return {"type": "user", "timestamp": iso(offset), "message": {"content": text}}


def timed(lines: list[dict[str, Any]]) -> list[tuple[float, dict[str, Any]]]:
    return [(activetime.epoch(line["timestamp"]) or 0.0, line) for line in lines]


def test_the_wait_for_a_prompt_is_the_persons_and_a_question_is_a_gate():
    lines = timed(
        [
            prompt(0),
            record(30),
            prompt(150),
            record(200, type="tool_use", id="q", name="AskUserQuestion"),
            record(800, "user", type="tool_result", tool_use_id="q"),
            record(820),
            prompt(4000),
            record(4010),
            {
                "type": "user",
                "timestamp": iso(4100),
                "message": {"content": "<task-notification> x"},
            },
            record(4120),
        ]
    )

    found = activetime.split(lines, idle=300, away=1800)

    def offsets(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(a - T0, b - T0) for a, b in intervals]

    assert offsets(found.busy) == [
        (0, 30),
        (150, 200),
        (800, 820),
        (4000, 4010),
        (4010, 4100),
        (4100, 4120),
    ]
    assert offsets(found.person) == [(30, 150)]
    assert offsets(found.gates) == [(200, 800)]


def test_measure_reports_the_persons_time_inside_the_window(setup):
    lead = setup["projects"] / "-work-shop" / "s-1.jsonl"
    write_lines(lead, [prompt(0), record(60), prompt(260), record(300)])

    active = activetime.measure("SHOP-1")

    assert active.lead_seconds == 100
    assert active.person_seconds == 200
    assert active.gate_seconds == 0


def test_the_command_shows_the_person_and_the_sensitivity(setup, capsys):
    assert cli.main(["active-time", "SHOP-1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert list(data["total_hours_by_idle"]) == ["3", "5", "10"]
    assert "person_hours" in data

    assert cli.main(["active-time", "SHOP-1"]) == 0
    out = capsys.readouterr().out
    assert "you: " in out
    assert "3/5/10 min idle threshold" in out
