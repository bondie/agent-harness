#!/usr/bin/env python3
"""PreToolUse, PostToolUse and PostToolUseFailure hook: stop a subagent that loops or keeps failing.

Only events with an `agent_id` (a subagent) count; the main session is never touched, nor are
the agent types in `exclude_agents` (by full type or the part after the last `:`).
- loop: the `loop_repeats`-th arrival of the same call (tool and input; for Bash without
  `description` and with runs of spaces collapsed) with nothing changed in between is denied.
  Reads and searches neither end nor reset a run; any other call ends every other call's run.
  Subagent and messaging calls, the report, waits and polls (a Bash command with `sleep` or
  `wait` as a word) and questions are never counted and end every run.
- failure_streak: after `failure_streak` failed calls in a row (interrupts do not count, a
  success resets), the next call is denied once, unless it reports or asks. A Bash exit 1 of a
  command whose 1 means no match, a difference or false (grep, diff, test, …), as the whole
  call or its last pipeline stage, after any `cd … &&`, is not counted.
- long_run: once per agent, `warn_minutes` after its first call, the next tool result carries
  a reminder to report before work that takes more than about 10 more minutes.
Settings: `runaway` in `$HARNESS_HOME/guard.json`, then in the `guard` key of the project's
`.claude/agent-harness.json`; 0 turns a rule off. State per agent lives in
`$HARNESS_HOME/runaway/<session>/<agent>.json` and holds hashes, not inputs. Each deny and
reminder appends a line without the tool input to `$HARNESS_HOME/guard-events.jsonl`. The
rules are adapted from the MIT-licensed runaway-guard skill. Standard library only; on an
internal error it allows the call and logs to stderr.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, TextIO

# A main-session call returns early, so what only a subagent needs is imported where it is used.
if TYPE_CHECKING:
    from pathlib import Path

DEFAULTS: dict[str, Any] = {
    "loop_repeats": 3,
    "failure_streak": 5,
    "warn_minutes": 10,
    "exclude_agents": [],
}
EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
READS = {"Read", "Grep", "Glob", "LS", "NotebookRead", "WebFetch", "WebSearch", "ToolSearch"}
REPORTS = {"SubagentHandback", "SendMessage", "AskUserQuestion"}
UNCOUNTED = REPORTS | {"Agent", "Task", "TaskOutput", "BashOutput", "Monitor", "ScheduleWakeup"}
WAIT = re.compile(r"\b(?:sleep|wait)\b")
SPACES = re.compile(r"[ \t]+")
NO_MATCH = {"grep", "egrep", "fgrep", "rg", "ag", "diff", "cmp", "test", "[", "[["}
# Matched only as typed: `./test` or `bin/test` is a project script, not the builtin.
BUILTIN_TESTS = {"test", "[", "[["}
GIT_DIFF_EXIT = {"--exit-code", "--quiet", "--no-index"}
UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
KEEP_SECONDS = 2 * 24 * 3600


@dataclass
class Settings:
    loop_repeats: int
    failure_streak: int
    warn_minutes: float
    exclude_agents: list[str]
    warnings: list[str]


@dataclass
class State:
    started: float
    warned: bool = False
    streak: int = 0
    runs: dict[str, int] = field(default_factory=dict)


def is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def problem(key: str, value: Any) -> str | None:
    if key == "exclude_agents":
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
        return None if valid else "must be a list of strings"
    if key == "warn_minutes":
        minutes = number(value)
        return None if minutes is not None and minutes >= 0 else "must be a number, 0 or more"
    if not is_count(value) or value < 0:
        return "must be a whole number, 0 or more"
    if key == "loop_repeats" and value == 1:
        return "must be 0 or at least 2"
    return None


def load_settings(event: dict[str, Any], environ: Mapping[str, str]) -> Settings:
    from pathlib import Path

    from guard_shell import config_sources

    warnings: list[str] = []
    cwd = event.get("cwd") if isinstance(event.get("cwd"), str) else None
    project = environ.get("CLAUDE_PROJECT_DIR") or cwd
    values = dict(DEFAULTS)
    for name, data in config_sources(environ, Path(project) if project else None, warnings):
        if not isinstance(data, dict) or "runaway" not in data:
            continue
        section = data["runaway"]
        if not isinstance(section, dict):
            warnings.append(f"{name}: runaway is not a JSON object, so it is ignored")
            continue
        for key, value in section.items():
            if key not in DEFAULTS:
                warnings.append(f"{name}: unknown key runaway.{key}")
                continue
            issue = problem(key, value)
            if issue:
                warnings.append(f"{name}: runaway.{key} {issue}, so it is ignored")
                continue
            values[key] = values[key] + value if key == "exclude_agents" else value
    return Settings(
        values["loop_repeats"],
        values["failure_streak"],
        values["warn_minutes"],
        values["exclude_agents"],
        warnings,
    )


def safe_name(value: Any) -> str:
    return UNSAFE.sub("-", value)[:100] if isinstance(value, str) and value else "unknown"


def state_path(event: dict[str, Any], environ: Mapping[str, str]) -> Path | None:
    agent = event.get("agent_id")
    if not isinstance(agent, str) or not agent:
        return None
    from guard_shell import harness_home

    session = safe_name(event.get("session_id"))
    return harness_home(environ) / "runaway" / session / f"{safe_name(agent)}.json"


def read_state(path: Path) -> State | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        print(f"guard_runaway.py starts over, {path} is unreadable: {error}", file=sys.stderr)
        return None
    started = number(data.get("started")) if isinstance(data, dict) else None
    if started is None:
        print(f"guard_runaway.py starts over, {path} has no start time", file=sys.stderr)
        return None
    runs = data.get("runs")
    streak = data.get("streak")
    return State(
        started,
        data.get("warned") is True,
        streak if is_count(streak) and streak > 0 else 0,
        {str(k): v for k, v in runs.items() if is_count(v)} if isinstance(runs, dict) else {},
    )


def write_state(path: Path, state: State) -> None:
    import tempfile
    from pathlib import Path

    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(asdict(state), file)
        Path(temp).replace(path)
    finally:
        Path(temp).unlink(missing_ok=True)


def remove_old_sessions(current: Path, now: float) -> None:
    import shutil

    try:
        sessions = [entry for entry in current.parent.iterdir() if entry != current]
    except OSError:
        return
    for entry in sessions:
        try:
            old = not entry.is_symlink() and now - entry.stat().st_mtime > KEEP_SECONDS
        except OSError:
            continue
        if old and entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)


def is_uncounted(tool: str, tool_input: Any) -> bool:
    if tool in UNCOUNTED:
        return True
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    return tool == "Bash" and isinstance(command, str) and bool(WAIT.search(command))


def fingerprint(tool: str, tool_input: Any) -> str:
    import hashlib

    if tool == "Bash" and isinstance(tool_input, dict):
        tool_input = {key: value for key, value in tool_input.items() if key != "description"}
        if isinstance(tool_input.get("command"), str):
            tool_input["command"] = SPACES.sub(" ", tool_input["command"]).strip()
    text = json.dumps([tool, tool_input], sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def count(tool: str, tool_input: Any, state: State) -> int:
    if is_uncounted(tool, tool_input):
        state.runs = {}
        return 0
    key = fingerprint(tool, tool_input)
    seen = state.runs.get(key, 0) + 1
    if tool not in READS:
        state.runs = {}
    state.runs[key] = seen
    return seen


def before_call(event: dict[str, Any], settings: Settings, state: State) -> tuple[str, str] | None:
    tool = event.get("tool_name")
    if not isinstance(tool, str):
        return None
    seen = count(tool, event.get("tool_input"), state)
    if tool in REPORTS:
        state.streak = 0
        return None
    if settings.failure_streak and state.streak >= settings.failure_streak:
        failed, state.streak = state.streak, 0
        return "failure_streak", (
            f"agent-harness runaway guard, rule failure_streak: {failed} calls failed in a row. "
            "To proceed: if the failures were expected (no match, a test you expect to fail), go "
            "on; otherwise stop and report to the lead what fails and what you tried."
        )
    if settings.loop_repeats and seen >= settings.loop_repeats:
        return "loop", (
            f"agent-harness runaway guard, rule loop: the call was repeated {seen} times "
            "without a change. To proceed: change the approach, or stop and report to the "
            "lead what blocks you."
        )
    return None


def answered_no(event: dict[str, Any]) -> bool:
    """Whether a Bash call failed only by exit 1 of a command whose 1 means no, differs or false."""
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    error = event.get("error")
    if event.get("tool_name") != "Bash" or not isinstance(command, str):
        return False
    if not isinstance(error, str) or error.split("\n", 1)[0].strip() != "Exit code 1":
        return False
    from guard_shell import PIPES, Command, Lexer, commands, unwrap

    lexer = Lexer(command)
    items = commands(lexer.run())
    if lexer.unclosed or not items or not all(isinstance(item, Command) for item in items):
        return False
    stages = [item for item in items if isinstance(item, Command)]
    while len(stages) > 1 and stages[0].after == "&&" and changes_dir(stages[0].words):
        stages = stages[1:]
    if any(stage.after not in PIPES for stage in stages[:-1]):
        return False
    words = stages[-1].words
    call = unwrap(words)
    if call is None or call.via_xargs:
        return False
    typed = words[len(words) - len(call.args) - 1]
    if call.program in BUILTIN_TESTS:
        return typed == call.program
    if call.program in NO_MATCH:
        return True
    return call.program == "git" and git_answers_no(call.args)


def changes_dir(words: list[str]) -> bool:
    from guard_shell import unwrap

    call = unwrap(words)
    return call is not None and call.program in ("cd", "pushd")


def git_answers_no(args: list[str]) -> bool:
    from guard_shell import GIT_VALUE_OPTIONS

    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in GIT_VALUE_OPTIONS else 1
    sub, rest = (args[i], args[i + 1 :]) if i < len(args) else ("", [])
    return sub == "grep" or (sub == "diff" and bool(GIT_DIFF_EXIT & set(rest)))


def after_call(
    event: dict[str, Any], settings: Settings, state: State, now: float
) -> tuple[str, str] | None:
    if event.get("hook_event_name") == "PostToolUse":
        state.streak = 0
    failed = event.get("hook_event_name") == "PostToolUseFailure"
    if failed and event.get("is_interrupt") is not True and not answered_no(event):
        state.streak += 1
    elapsed = now - state.started
    if not settings.warn_minutes or state.warned or elapsed < settings.warn_minutes * 60:
        return None
    state.warned = True
    return "long_run", (
        f"agent-harness runaway guard: you have run {int(elapsed // 60)} minutes. If what is "
        "left will take more than about 10 more minutes (long tests, backtests, waiting for a "
        "pipeline), stop and report what is done, what is left, the expected time and the "
        "options, so the lead can decide."
    )


def excluded(event: dict[str, Any], settings: Settings) -> bool:
    agent = event.get("agent_type")
    if not isinstance(agent, str) or not agent:
        return False
    return agent in settings.exclude_agents or agent.rsplit(":", 1)[-1] in settings.exclude_agents


def deny(reason: str, warnings: list[str]) -> dict[str, Any]:
    if warnings:
        reason += " Runaway guard config warnings: " + "; ".join(warnings) + "."
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def decision(
    event: dict[str, Any], environ: Mapping[str, str], now: float | None = None
) -> dict[str, Any] | None:
    name = event.get("hook_event_name")
    path = state_path(event, environ)
    if name not in EVENTS or path is None:
        return None
    settings = load_settings(event, environ)
    if excluded(event, settings) or not (
        settings.loop_repeats or settings.failure_streak or settings.warn_minutes
    ):
        return None
    from datetime import UTC, datetime

    from guard_shell import log_guard_event
    from save_artifacts import locked

    moment = time.time() if now is None else now
    with locked(path.parent):
        state = read_state(path)
        if state is None:
            remove_old_sessions(path.parent, moment)
            state = State(moment)
        before = name == "PreToolUse"
        found = (
            before_call(event, settings, state)
            if before
            else after_call(event, settings, state, moment)
        )
        write_state(path, state)
    if found is None:
        return None
    rule, text = found
    outcome = "deny" if before else "context"
    log_guard_event(
        environ, event, "guard_runaway", rule, outcome, now=datetime.fromtimestamp(moment, UTC)
    )
    if before:
        return deny(text, settings.warnings)
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": text}}


def main(
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    environ: Mapping[str, str] = os.environ,
) -> int:
    try:
        event = json.load(stdin)
        verdict = decision(event, environ) if isinstance(event, dict) else None
    except Exception as error:
        print(
            f"guard_runaway.py allowed the call after an internal error: {error!r}", file=sys.stderr
        )
        return 0
    if verdict is not None:
        json.dump(verdict, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
