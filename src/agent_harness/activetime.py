"""Active time on a ticket, from the work markers and the Claude Code transcripts.

`session_state.py` appends a line to `$HARNESS_HOME/work.jsonl` when a main session starts
work on a ticket (the ticket playbook or `/agent-harness:work <KEY>`) and when it stops
(`/agent-harness:work stop`). A session works on that ticket from the marker until its next
marker. Inside those windows the main session's records count, and so do the records of its
subagents; the runs that `runs.jsonl` files under the key count wherever they ran.

Time is active when two consecutive records of one transcript are at most `idle` apart, or
when a tool call is still running between them and the gap is at most `tool_cap`; a longer
gap does not count at all. A question to the person does not keep a transcript busy. The
lead, the agents and the total are unions of intervals, so parallel agents and sessions count
once. It is the time
a session or an agent was busy, not the hours a person logged. The transcript format is
internal to Claude Code and Claude Code deletes old transcripts (30 days by default), so the
reader skips what it does not understand and `export-case` freezes the result.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from .artifacts import harness_home, ticket_keys

IDLE_SECONDS = 5 * 60
TOOL_CAP_SECONDS = 60 * 60
RECORD_TYPES = {"user", "assistant", "attachment"}
WAITING_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
Interval = tuple[float, float]


@dataclass(frozen=True)
class Window:
    session: str
    start: float
    end: float | None
    transcript: Path | None


@dataclass(frozen=True)
class ActiveTime:
    ticket: str
    lead_seconds: float
    agent_seconds: float
    total_seconds: float
    sessions: int
    agents: int
    first: float | None
    last: float | None
    idle_seconds: float

    def as_json(self) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "total_hours": hours(self.total_seconds),
            "lead_hours": hours(self.lead_seconds),
            "agent_hours": hours(self.agent_seconds),
            "sessions": self.sessions,
            "agents": self.agents,
            "first": stamp(self.first),
            "last": stamp(self.last),
            "idle_minutes": self.idle_seconds / 60,
        }


def hours(seconds: float) -> float:
    return round(seconds / 3600, 2)


def stamp(moment: float | None) -> str | None:
    if moment is None:
        return None
    return datetime.fromtimestamp(moment, UTC).isoformat(timespec="seconds")


def epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo else None


def json_lines(path: Path) -> Iterator[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def runs_log() -> Path:
    custom = os.environ.get("HARNESS_RUNS_LOG")
    return Path(custom) if custom else Path.home() / ".claude" / "agent-harness" / "runs.jsonl"


def projects_dir() -> Path:
    custom = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = Path(custom).expanduser() if custom else Path.home() / ".claude"
    return base / "projects"


def windows(ticket: str, log: Path) -> list[Window]:
    """The stretches in which a session worked on `ticket`, from its markers."""
    markers: dict[str, list[tuple[float, str | None, Path | None]]] = {}
    for entry in json_lines(log):
        session, moment = entry.get("session"), epoch(entry.get("at"))
        if not isinstance(session, str) or moment is None:
            continue
        key = entry.get("ticket") if isinstance(entry.get("ticket"), str) else None
        path = entry.get("transcript")
        markers.setdefault(session, []).append(
            (moment, key, Path(path) if isinstance(path, str) and path else None)
        )
    found = []
    for session, entries in markers.items():
        entries.sort(key=lambda item: item[0])
        for index, (moment, key, path) in enumerate(entries):
            if key != ticket:
                continue
            end = entries[index + 1][0] if index + 1 < len(entries) else None
            found.append(Window(session, moment, end, path))
    return found


def records(path: Path) -> list[tuple[float, dict[str, Any]]]:
    found = []
    for entry in json_lines(path):
        moment = epoch(entry.get("timestamp"))
        if moment is not None and entry.get("type") in RECORD_TYPES:
            found.append((moment, entry))
    found.sort(key=lambda item: item[0])
    return found


def tool_ids(entry: dict[str, Any], kind: str, field: str) -> set[str]:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return set()
    return {
        str(item.get(field))
        for item in content
        if isinstance(item, dict)
        and item.get("type") == kind
        and item.get(field)
        and item.get("name") not in WAITING_TOOLS
    }


def busy(
    entries: list[tuple[float, dict[str, Any]]], idle: float, tool_cap: float
) -> list[Interval]:
    """The intervals between consecutive records in which the transcript was active."""
    running: set[str] = set()
    found = []
    for (start, entry), (end, _) in pairwise(entries):
        running |= tool_ids(entry, "tool_use", "id")
        running -= tool_ids(entry, "tool_result", "tool_use_id")
        gap = end - start
        if gap <= idle or (running and gap <= tool_cap):
            found.append((start, end))
    return found


def clip(intervals: Iterable[Interval], start: float, end: float | None) -> list[Interval]:
    stop = float("inf") if end is None else end
    clipped = ((max(a, start), min(b, stop)) for a, b in intervals)
    return [(a, b) for a, b in clipped if a < b]


def union(intervals: Iterable[Interval]) -> float:
    total, reached = 0.0, float("-inf")
    for start, end in sorted(intervals):
        if end <= reached:
            continue
        total += end - max(start, reached)
        reached = end
    return total


def filed_runs(log: Path) -> dict[str, tuple[str, str]]:
    """The session and the first key of each subagent run that `runs.jsonl` files under a key."""
    found = {}
    for entry in json_lines(log):
        task_keys = ticket_keys(str(entry.get("ticket") or ""))
        agent, session = entry.get("agent_id"), entry.get("session")
        if task_keys and isinstance(agent, str) and isinstance(session, str):
            found[agent] = (session, task_keys[0])
    return found


def run_transcript(agent: str, session: str, projects: Path) -> Path | None:
    matches = sorted(projects.glob(f"*/{session}/subagents/agent-{agent}.jsonl"))
    return matches[0] if matches else None


def measure(
    ticket: str,
    work_log: Path | None = None,
    runs: Path | None = None,
    projects: Path | None = None,
    idle: float = IDLE_SECONDS,
    tool_cap: float = TOOL_CAP_SECONDS,
) -> ActiveTime:
    work_log = work_log or harness_home() / "work.jsonl"
    projects = projects or projects_dir()
    lead: list[Interval] = []
    agents: dict[Path, list[Interval]] = {}
    sessions = set()
    filed = filed_runs(runs or runs_log())
    for window in windows(ticket, work_log):
        if window.transcript is None:
            continue
        sessions.add(window.session)
        lead += clip(busy(records(window.transcript), idle, tool_cap), window.start, window.end)
        for path in sorted(window.transcript.with_suffix("").glob("subagents/agent-*.jsonl")):
            if filed.get(path.stem.removeprefix("agent-"), ("", ticket))[1] != ticket:
                continue
            found = clip(busy(records(path), idle, tool_cap), window.start, window.end)
            if found:
                agents.setdefault(path, []).extend(found)
    for agent, (session, key) in filed.items():
        if key != ticket:
            continue
        transcript = run_transcript(agent, session, projects)
        if transcript is not None:
            agents[transcript] = busy(records(transcript), idle, tool_cap)
    agent_intervals = [interval for found in agents.values() for interval in found]
    every = lead + agent_intervals
    return ActiveTime(
        ticket=ticket,
        lead_seconds=union(lead),
        agent_seconds=union(agent_intervals),
        total_seconds=union(every),
        sessions=len(sessions),
        agents=sum(1 for found in agents.values() if found),
        first=min((a for a, _ in every), default=None),
        last=max((b for _, b in every), default=None),
        idle_seconds=idle,
    )
