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
and the next SessionStart removes it (one without a `pid` after a day). Standard library only;
an internal error is printed to stderr and never blocks anything.
"""

from __future__ import annotations

import json
import os
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
SAME_STATE_SECONDS = 2
STALE_SECONDS = 24 * 3600


def harness_home(environ: Mapping[str, str]) -> str:
    custom = environ.get("HARNESS_HOME", "").strip()
    if custom:
        return os.path.expanduser(custom)
    return os.path.join(os.path.expanduser("~"), ".claude", "agent-harness")


def state_path(event: Mapping[str, Any], environ: Mapping[str, str]) -> str | None:
    session = event.get("session_id")
    if not isinstance(session, str) or not session or UNSAFE & set(session) or session[0] == ".":
        return None
    return os.path.join(harness_home(environ), "sessions", f"{session}.json")


def transition(
    event: Mapping[str, Any], previous: Mapping[str, Any]
) -> tuple[str, str | None] | None:
    name = event.get("hook_event_name")
    tool = event.get("tool_name") if isinstance(event.get("tool_name"), str) else None
    agent = event.get("agent_id") or None
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
    directory, name = os.path.split(path)
    os.makedirs(directory, exist_ok=True)
    temporary = os.path.join(directory, f".{name}.{os.getpid()}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def ended(data: Mapping[str, Any], moment: float) -> bool:
    if data.get("pid") is not None:
        return not alive(data.get("pid"))
    updated = data.get("updated_epoch")
    return not isinstance(updated, (int, float)) or moment - updated > STALE_SECONDS


def remove_dead(path: str, moment: float) -> None:
    directory = os.path.dirname(path)
    for name in os.listdir(directory):
        other = os.path.join(directory, name)
        if name.endswith(".json") and other != path and ended(read(other), moment):
            try:
                os.remove(other)
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
    if name == "SessionEnd":
        if not agent and os.path.exists(path):
            os.remove(path)
        return None
    previous = read(path)
    found = transition(event, previous)
    if found is None:
        return None
    state, detail = found
    moment = time.time() if now is None else now
    same_state = previous.get("state") == state
    unchanged = same_state and previous.get("detail") == detail
    if unchanged and moment - float(previous.get("updated_epoch") or 0) < SAME_STATE_SECONDS:
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
