#!/usr/bin/env python3
"""SessionStart, UserPromptSubmit, Notification, Stop and SessionEnd hook: keep the state of
each main session in `$HARNESS_HOME/sessions/<session>.json` for a viewer that shows which
sessions work and which wait for the person.

`guard_runaway.py` calls `record` for every tool event, so a tool call starts no second
process; for the same reason this module works with `os.path` and imports `subprocess` only
when it looks for the session's process. States: `working` (a prompt or a tool call, `detail`
holds the tool), `waiting_answer` (a question or a plan to approve), `waiting_permission` (a
permission prompt, `detail` holds its message) and `idle` (the turn ended, or the session sat
idle long enough for Claude Code's `idle_prompt`). A subagent's tool event only ends a wait
its own prompt started (or a wait of unknown origin); a compaction keeps the state. SessionEnd
removes the file; a session that ended without it is left for the viewer to drop by its `pid`,
and the next SessionStart removes it (one without a `pid` after a day). A prompt that starts the
ticket playbook or `/agent-harness:work <KEY>`, or the main session's Skill call for either,
marks the ticket the session works on: `ticket` in the state file, and a line in
`$HARNESS_HOME/work.jsonl` that `harness-eval active-time` reads; `/agent-harness:work stop`
ends it. Each running subagent
also gets `$HARNESS_HOME/agents/<session>/<agent>.json` (type, start, tool calls, last tool)
from SubagentStart to SubagentStop. TaskStop fires no SubagentStop, so the main session's
TaskStop adds `stopped_at` to the file instead, for a viewer to offer resuming it; the agent's next
start or tool call clears the mark. The cleanup of a session removes its agents too. Standard
library only; an internal error is printed to stderr and never blocks anything.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Mapping
from typing import Any, TextIO

VERSION = 1
TOOL_EVENTS = {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
QUESTIONS = {"AskUserQuestion", "ExitPlanMode"}
WAITING = {"waiting_answer", "waiting_permission"}
NOTIFICATION_STATES = {
    "permission_prompt": "waiting_permission",
    "elicitation_dialog": "waiting_answer",
    "idle_prompt": "idle",
}
UNSAFE = frozenset("/\\\0")
TURN_ENDS = {"Stop", "UserPromptSubmit", "SessionEnd", "SubagentStop"}
TOOL_DONE = {"PostToolUse", "PostToolUseFailure"}
WORK_SKILLS = {"agent-harness:ticket", "agent-harness:work"}
TYPED = re.compile(r"/(?:agent-harness:)?(ticket|work)(?:\s+(.*))?", re.DOTALL)
TAGGED = re.compile(
    r"^(?:<command-message>[^<]*</command-message>\s*)?"
    r"<command-name>\s*/(?:agent-harness:)?(ticket|work)\s*</command-name>"
    r"(?:.*?<command-args>(.*?)</command-args>)?",
    re.DOTALL,
)
KEY = re.compile(r"\b([A-Z][A-Z0-9]+)-\d+\b")
# Key-shaped names of standards, as in agent_harness.artifacts; a test keeps them in step.
NOT_TICKET_PREFIXES = (
    "UTF",
    "ISO",
    "PSR",
    "SHA",
    "RFC",
    "CVE",
    "HTTP",
    "TLS",
    "X",
    "ECMA",
    "IEEE",
    "UTC",
    "GMT",
)
STOP = "stop"
SAME_STATE_SECONDS = 2
STALE_SECONDS = 24 * 3600


def harness_home(environ: Mapping[str, str]) -> str:
    custom = environ.get("HARNESS_HOME", "").strip()
    if custom:
        return os.path.expanduser(custom)
    return os.path.join(os.path.expanduser("~"), ".claude", "agent-harness")


def state_path(event: Mapping[str, Any], environ: Mapping[str, str]) -> str | None:
    session = safe(event.get("session_id"))
    if session is None:
        return None
    return os.path.join(harness_home(environ), "sessions", f"{session}.json")


def safe(name: Any) -> str | None:
    if not isinstance(name, str) or not name or UNSAFE & set(name) or name[0] == ".":
        return None
    return name


def agent_path(event: Mapping[str, Any], environ: Mapping[str, str]) -> str | None:
    session, agent = safe(event.get("session_id")), safe(event.get("agent_id"))
    if session is None or agent is None:
        return None
    return os.path.join(harness_home(environ), "agents", session, f"{agent}.json")


def track_agent(event: Mapping[str, Any], environ: Mapping[str, str], moment: float) -> None:
    """Keep the file of a running subagent: created on its start, counted on its tool calls."""
    path = agent_path(event, environ)
    name = event.get("hook_event_name")
    if path is None or name not in ("SubagentStart", "SubagentStop", "PreToolUse"):
        return
    if name == "SubagentStop":
        if os.path.exists(path):
            os.remove(path)
        return
    data = read(path)
    if name == "PreToolUse" and not data:
        return
    if name == "SubagentStart":
        agent_type = event.get("agent_type")
        data = {
            "version": VERSION,
            "agent_id": event.get("agent_id"),
            "agent_type": agent_type if isinstance(agent_type, str) else None,
            "session_id": event.get("session_id"),
            "started_at": stamp(moment),
            "started_epoch": moment,
            "tool_calls": 0,
            "last_tool": None,
        }
    if name == "PreToolUse":
        data.pop("stopped_at", None)
        tool = event.get("tool_name")
        data["tool_calls"] = int(data.get("tool_calls") or 0) + 1
        data["last_tool"] = tool if isinstance(tool, str) else None
    data["updated_at"] = stamp(moment)
    data["updated_epoch"] = moment
    write(path, data)


def mark_stopped_agent(event: Mapping[str, Any], environ: Mapping[str, str], moment: float) -> None:
    tool_input = event.get("tool_input")
    if event.get("tool_name") != "TaskStop" or not isinstance(tool_input, Mapping):
        return
    task = {"session_id": event.get("session_id"), "agent_id": tool_input.get("task_id")}
    path = agent_path(task, environ)
    data = read(path) if path is not None else {}
    if path is None or not data:
        return
    data |= {"stopped_at": stamp(moment), "updated_at": stamp(moment), "updated_epoch": moment}
    write(path, data)


def work_marker(event: Mapping[str, Any]) -> str | None:
    """The key a main-session prompt or Skill call starts work on, `STOP`, or None."""
    found: tuple[str, str] | None = None
    if event.get("hook_event_name") == "UserPromptSubmit" and isinstance(event.get("prompt"), str):
        prompt = event["prompt"].strip()
        match = TAGGED.search(prompt) if prompt.startswith("<") else TYPED.fullmatch(prompt)
        found = (match.group(1), match.group(2) or "") if match else None
    tool_input = event.get("tool_input")
    if event.get("tool_name") == "Skill" and isinstance(tool_input, Mapping):
        skill = str(tool_input.get("skill") or "")
        found = (skill.removeprefix("agent-harness:"), str(tool_input.get("args") or ""))
        found = found if skill in WORK_SKILLS else None
    if found is None:
        return None
    skill, args = found
    words = args.split()
    if skill == "work" and words and words[0].lower() == STOP:
        return STOP
    keys = (key for key in KEY.finditer(args) if key.group(1) not in NOT_TICKET_PREFIXES)
    return next((key.group(0) for key in keys), None)


def mark_work(event: Mapping[str, Any], environ: Mapping[str, str], moment: float) -> str | None:
    """Log the start or end of work on a ticket; the key, `STOP` or None when not a marker."""
    marker = work_marker(event) if event.get("hook_event_name") != "PostToolUse" else None
    if marker is None:
        return None
    line = {
        "session": event.get("session_id"),
        "ticket": None if marker == STOP else marker,
        "at": stamp(moment),
        "transcript": event.get("transcript_path"),
    }
    path = os.path.join(harness_home(environ), "work.jsonl")
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    return marker


def describe(tool: str | None, tool_input: Any) -> str | None:
    """What a permission is for, in one line: the command, the file or the address."""
    if not isinstance(tool_input, dict):
        return tool
    for key in ("command", "file_path", "notebook_path", "url", "path", "pattern"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return f"{tool}: {' '.join(value.split())}"[:200]
    return tool


def clear_requests(event: Mapping[str, Any], environ: Mapping[str, str]) -> None:
    """Drop the permission requests this event has answered: the call ran or the turn ended."""
    name = event.get("hook_event_name")
    if name not in TURN_ENDS and name not in TOOL_DONE:
        return
    directory = os.path.join(harness_home(environ), "permissions")
    if not os.path.isdir(directory):
        return
    agent = event.get("agent_id") or None
    for file in os.listdir(directory):
        if not file.endswith(".json") or file.endswith(".decision.json"):
            continue
        path = os.path.join(directory, file)
        request = read(path)
        if request.get("session_id") != event.get("session_id") or request.get("agent_id") != agent:
            continue
        same_call = request.get("tool_name") == event.get("tool_name") and request.get(
            "tool_input"
        ) == event.get("tool_input")
        if name in TURN_ENDS or same_call:
            try:
                os.remove(path)
            except FileNotFoundError:
                continue


def transition(
    event: Mapping[str, Any], previous: Mapping[str, Any]
) -> tuple[str, str | None] | None:
    name = event.get("hook_event_name")
    tool = event.get("tool_name") if isinstance(event.get("tool_name"), str) else None
    agent = event.get("agent_id") or None
    if name == "PermissionRequest":
        return "waiting_permission", describe(tool, event.get("tool_input"))
    if agent and name != "Notification":
        waits_for = previous.get("waiting_agent")
        if name not in TOOL_EVENTS or previous.get("state") not in WAITING:
            return None
        return ("working", tool) if waits_for in (None, agent) else None
    if name == "PreToolUse" and tool in QUESTIONS:
        return "waiting_answer", tool
    if name in TOOL_EVENTS:
        return "working", tool
    if name == "UserPromptSubmit":
        return "working", None
    if name == "SessionStart" and event.get("source") == "compact" and previous.get("state"):
        return str(previous["state"]), previous.get("detail")
    if name in ("SessionStart", "Stop"):
        return "idle", None
    if name != "Notification":
        return None
    state = NOTIFICATION_STATES.get(str(event.get("notification_type")))
    if state is None:
        return None
    if state == "idle":
        return None if previous.get("state") in WAITING else (state, None)
    if state == previous.get("state"):
        return None
    message = event.get("message")
    return state, message[:200] if isinstance(message, str) else None


def read_text(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def repository_name(directory: str, git_dir: str) -> str:
    """The main repository's name, also for a linked worktree, whose git dir has a commondir."""
    try:
        common = read_text(os.path.join(git_dir, "commondir")).strip()
    except OSError:
        return os.path.basename(directory)
    common_dir = os.path.normpath(os.path.join(git_dir, common))
    if os.path.basename(common_dir) == ".git":
        return os.path.basename(os.path.dirname(common_dir))
    return os.path.basename(common_dir).removesuffix(".git") or os.path.basename(directory)


def git_head(cwd: str) -> tuple[str, str, str | None] | None:
    """The working tree root, repository name and branch of `cwd`, read without running git."""
    directory = os.path.abspath(cwd)
    while True:
        dot_git = os.path.join(directory, ".git")
        try:
            git_dir = dot_git
            if os.path.isfile(dot_git):
                pointer = read_text(dot_git).strip()
                if not pointer.startswith("gitdir:"):
                    return None
                target = pointer.removeprefix("gitdir:").strip()
                git_dir = os.path.normpath(os.path.join(directory, target))
            if os.path.isdir(git_dir):
                head = read_text(os.path.join(git_dir, "HEAD")).strip()
                branch = head.removeprefix("ref: refs/heads/") if head.startswith("ref: ") else None
                return directory, repository_name(directory, git_dir), branch
        except OSError:
            return None
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def claude_pid() -> int | None:
    """The nearest ancestor process named claude: the hook runs under a shell it starts."""
    import subprocess

    pid = os.getppid()
    for _ in range(4):
        try:
            found = subprocess.run(  # noqa: S603
                ["ps", "-o", "ppid=,comm=", "-p", str(pid)],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout.split(None, 1)
            if len(found) < 2:
                return None
            if os.path.basename(found[1].strip()).startswith("claude"):
                return pid
            pid = int(found[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None


def alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read(path: str) -> dict[str, Any]:
    try:
        data = json.loads(read_text(path))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write(path: str, data: dict[str, Any]) -> None:
    """Write privately: `detail` can hold a command with a secret in it."""
    directory, name = os.path.split(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = os.path.join(directory, f".{name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def ended(data: Mapping[str, Any], moment: float) -> bool:
    if data.get("pid") is not None:
        return not alive(data.get("pid"))
    updated = data.get("updated_epoch")
    return not isinstance(updated, (int, float)) or moment - updated > STALE_SECONDS


def stale(path: str, moment: float) -> bool:
    updated = read(path).get("updated_epoch")
    return not isinstance(updated, (int, float)) or moment - updated > STALE_SECONDS


def remove_dead(path: str, moment: float) -> None:
    directory = os.path.dirname(path)
    home = os.path.dirname(directory)
    agents = os.path.join(home, "agents")
    for folder in (directory, agents):
        if os.path.isdir(folder):
            os.chmod(folder, 0o700)
    dead = set()
    for name in os.listdir(directory):
        other = os.path.join(directory, name)
        if name.endswith(".json") and other != path and ended(read(other), moment):
            dead.add(name.removesuffix(".json"))
            try:
                os.remove(other)
            except FileNotFoundError:
                continue
    remove_old_requests(os.path.join(home, "permissions"), dead, moment)
    if not os.path.isdir(agents):
        return
    import shutil

    for session in os.listdir(agents):
        folder = os.path.join(agents, session)
        files = [os.path.join(folder, name) for name in os.listdir(folder)]
        if session in dead or all(stale(file, moment) for file in files):
            shutil.rmtree(folder, ignore_errors=True)


def remove_old_requests(directory: str, dead: set[str], moment: float) -> None:
    """Drop permission requests a killed hook left behind: of an ended session or an hour old."""
    if not os.path.isdir(directory):
        return
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if not name.endswith(".json"):
            continue
        if name.endswith(".decision.json"):
            asked = os.path.join(directory, name.removesuffix(".decision.json") + ".json")
            if not os.path.exists(asked):
                os.remove(path)
            continue
        request = read(path)
        created = request.get("created_epoch")
        old = not isinstance(created, (int, float)) or moment - created > 3600
        if request.get("session_id") in dead or old:
            try:
                os.remove(path)
            except FileNotFoundError:
                continue


def stamp(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


def where(cwd: Any, previous: Mapping[str, Any]) -> dict[str, Any]:
    head = git_head(cwd) if isinstance(cwd, str) else None
    root = head[0] if head else cwd
    if head:
        project: str | None = head[1]
    else:
        project = os.path.basename(root.rstrip(os.sep)) if isinstance(root, str) else None
    pid = previous.get("pid")
    if not alive(pid) and not ("pid" in previous and pid is None):
        pid = claude_pid()
    return {
        "root": root if isinstance(root, str) else None,
        "project": project,
        "branch": head[2] if head else None,
        "pid": pid,
    }


def record(
    event: Mapping[str, Any], environ: Mapping[str, str], now: float | None = None
) -> dict[str, Any] | None:
    """Update the session's state file for one hook event and return what it now holds."""
    path = state_path(event, environ)
    if path is None:
        return None
    name = event.get("hook_event_name")
    agent = event.get("agent_id") or None
    moment = time.time() if now is None else now
    clear_requests(event, environ)
    if agent:
        track_agent(event, environ, moment)
    if not agent and name == "PostToolUse":
        mark_stopped_agent(event, environ, moment)
    marker = None if agent else mark_work(event, environ, moment)
    if name == "SessionEnd":
        if not agent and os.path.exists(path):
            os.remove(path)
        return None
    previous = read(path)
    found = transition(event, previous)
    if found is None:
        return None
    state, detail = found
    same_state = previous.get("state") == state
    unchanged = same_state and previous.get("detail") == detail
    recent = moment - float(previous.get("updated_epoch") or 0) < SAME_STATE_SECONDS
    if unchanged and recent and marker is None:
        return previous
    cwd = previous.get("cwd") if agent or not isinstance(event.get("cwd"), str) else event["cwd"]
    data = dict(previous)
    if not agent and (name not in TOOL_EVENTS or "project" not in previous):
        data |= where(cwd, previous)
    data |= {
        "version": VERSION,
        "session_id": event.get("session_id"),
        "cwd": cwd,
        "state": state,
        "detail": detail,
        "waiting_agent": agent if state in WAITING else None,
        "state_since": previous.get("state_since") if same_state else stamp(moment),
        "started_at": previous.get("started_at") or stamp(moment),
        "updated_at": stamp(moment),
        "updated_epoch": moment,
    }
    if marker is not None:
        data |= {"ticket": None if marker == STOP else marker, "ticket_since": stamp(moment)}
    write(path, data)
    if name == "SessionStart":
        remove_dead(path, moment)
    return data


def main(stdin: TextIO = sys.stdin, environ: Mapping[str, str] = os.environ) -> int:
    try:
        event = json.load(stdin)
        if isinstance(event, dict):
            record(event, environ)
    except Exception as error:
        print(f"session_state.py could not record the session state: {error!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
