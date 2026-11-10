#!/usr/bin/env python3
"""SubagentStart, SubagentStop and SubagentHandback hook: memory a run grew over its cap.

`guard_memory_writes.py` caps Write and Edit in an agent's own memory, but a write through Bash
(`cat >>`, `sed -i`, a script) never reaches it. So for the four team agents this hook records,
when a run starts or resumes (SubagentStart), the time and the size of every file in the
agent's own memory directory (`~/.claude/agent-memory/<agent>/`). When the run stops, a file
blocks it once when all three hold:

- it is over its cap (`$HARNESS_HOME/memory.json`, loaded as the guard loads it: 8 KB, 4 KB for
  `MEMORY.md`);
- it is larger than at the recorded start, or new;
- a tool call the agent made since that start writes it: a Write, Edit or MultiEdit of it, or a
  write-shaped Bash command that names it (`bash_writes`); a read with `cat` does not count.

Without a recorded start, the run counts from the last prompt or coordinator message that
followed a stop (`resumed_at`), and a file counts as larger when it was modified since then.
A file that shrank, one only a parallel run wrote and one the run only read do not block.
After the block the hook lets the agent stop and warns the person; the count starts over at the
next start. In auto mode the agent delivers its report through SubagentHandback before it
stops, so the same check runs as a PreToolUse hook on that tool and denies the call; both paths
share the count. Retro runs are checked too: they write memory. When the hook cannot record its
count, it lets the agent through and says so. Standard library only, and an internal error
never blocks.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple, TextIO

from guard_memory_writes import INDEX, TEAM, load_settings, memory_dir_name
from log_agent_runs import parse_time
from save_artifacts import (
    HANDBACK,
    agent_transcript,
    harness_home,
    read_transcript,
    run_started,
    write_json,
)

MAX_BLOCKS = 1
WRITES = {"Write", "Edit", "MultiEdit"}
UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
KEEP_SECONDS = 2 * 24 * 3600


class Oversized(NamedTuple):
    path: Path
    size: int
    cap_kb: float
    through_bash: bool


class Checked(NamedTuple):
    key: str
    started: float
    calls: list[dict[str, Any]]


def team_agent(event: Mapping[str, Any]) -> str | None:
    agent_type = event.get("agent_type")
    if not isinstance(agent_type, str) or agent_type.split(":")[-1] not in TEAM:
        return None
    return agent_type


def memory_dir(agent_type: str) -> Path:
    return Path.home() / ".claude" / "agent-memory" / memory_dir_name(agent_type)


def memory_files(directory: Path) -> dict[Path, os.stat_result]:
    if not directory.is_dir():
        return {}
    found = {}
    for path in sorted(directory.rglob("*")):
        try:
            info = path.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            found[path] = info
    return found


def items_of(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return [item for item in content if isinstance(item, dict)] if isinstance(content, list) else []


def moment(entry: Mapping[str, Any]) -> float | None:
    stamp = entry.get("timestamp")
    try:
        return parse_time(stamp).timestamp() if isinstance(stamp, str) else None
    except ValueError:
        return None


def tool_calls(entries: list[dict[str, Any]], since: float | None = None) -> list[dict[str, Any]]:
    """The agent's own tool calls, without the replays in other kinds of entries."""
    calls = []
    for entry in entries:
        when = moment(entry)
        if entry.get("type") != "assistant" or (since is not None and (when or 0) < since):
            continue
        calls += [item for item in items_of(entry) if item.get("type") == "tool_use"]
    return calls


def resumed_at(entries: list[dict[str, Any]]) -> tuple[int, Any]:
    """Without a start record: the last prompt or coordinator message that followed a stop.

    A stop is a SubagentHandback call or an assistant turn with text and no tool call, and any
    message to the agent ends it; a message that arrives mid-task, a task notification or a
    system reminder (stop hook feedback, a handback reminder) starts nothing.
    """
    start, prompt, stamp, stopped = 0, None, None, True
    for number, entry in enumerate(entries):
        items = items_of(entry)
        uses = [item for item in items if item.get("type") == "tool_use"]
        if entry.get("type") == "assistant" and uses:
            stopped = any(item.get("name") == HANDBACK for item in uses)
            continue
        if entry.get("type") == "assistant":
            stopped = stopped or any(item.get("type") == "text" for item in items)
            continue
        if entry.get("type") != "user" or any(i.get("type") == "tool_result" for i in items):
            continue
        current = entry.get("promptId")
        has_id = isinstance(current, str) and bool(current)
        new = (current != prompt) if has_id else not entry.get("isMeta")
        prompt = current if has_id else prompt
        origin = entry.get("origin")
        sender = origin.get("kind") if isinstance(origin, dict) else None
        if new and stopped and (not entry.get("isMeta") or sender == "coordinator"):
            start, stamp = number, entry.get("timestamp")
        stopped = False
    return start, stamp


def checked_part(entries: list[dict[str, Any]], state: Mapping[str, Any]) -> Checked:
    """The calls to check: since the recorded start, else since the last resume after a stop."""
    started = state.get("started")
    if isinstance(started, int | float) and not isinstance(started, bool):
        return Checked(f"start:{started}", float(started), tool_calls(entries, float(started)))
    start, stamp = resumed_at(entries)
    when = moment({"timestamp": stamp})
    since = when if when is not None else run_started(entries).timestamp()
    return Checked(f"{start}:{stamp}", since, tool_calls(entries[start:]))


REDIRECT = re.compile(r"\d?>>?\|?\s*(\"[^\"]*\"|'[^']*'|[^\s;&|<>()]+)")
PIECES = re.compile(r"&&|\|\||[;|\n]")
PYTHON_WRITE = re.compile(
    r"\bopen\((?:[^()]|\([^()]*\))*?['\"][rbt+]*[wax][rbt+]*['\"]|\.write_(?:text|bytes)\("
)
IN_PLACE = {"sed": re.compile(r"-[a-zA-Z]*i|--in-place"), "perl": re.compile(r"-[a-zA-Z0-9]*i")}
COPIERS = {"cp", "mv", "rsync", "install"}


def piece_writes(piece: str, name: str, own: str) -> bool:
    words = piece.split()
    while words and (words[0] == "sudo" or re.match(r"\w+=", words[0])):
        words = words[1:]
    if not words or name not in piece:
        return False
    tool = Path(words[0]).name
    if tool == "tee":
        return True
    if tool in IN_PLACE:
        return any(IN_PLACE[tool].match(word) for word in words[1:])
    return tool in COPIERS and (own in words[-1] or name in words[-1])


def bash_writes(command: str, path: Path, own: str) -> bool:
    """A write-shaped Bash command that names the file: a read such as `cat` does not count."""
    name = path.name
    if str(path) not in command and not (name in command and own in command):
        return False
    if any(name in target for target in REDIRECT.findall(command)):
        return True
    if PYTHON_WRITE.search(command):
        return True
    return any(piece_writes(piece, name, own) for piece in PIECES.split(command))


def check(
    event: Mapping[str, Any], environ: Mapping[str, str], state: Mapping[str, Any]
) -> tuple[list[Oversized], str] | None:
    """The files this run grew over their cap and the key of the checked part; None if unchecked."""
    agent_type = team_agent(event)
    if agent_type is None:
        return None
    if event.get("hook_event_name") == "PreToolUse" and event.get("tool_name") != HANDBACK:
        return None
    checked = checked_part(read_transcript(agent_transcript(dict(event))), state)
    baseline = state.get("baseline") if isinstance(state.get("baseline"), dict) else None
    settings = load_settings(dict(environ))
    directory = memory_dir(agent_type)
    own = memory_dir_name(agent_type)
    written: set[str] = set()
    commands: list[str] = []
    for call in checked.calls:
        tool_input = call.get("input")
        if not isinstance(tool_input, dict):
            continue
        target, command = tool_input.get("file_path"), tool_input.get("command")
        if call.get("name") in WRITES and isinstance(target, str) and target:
            written |= {os.path.normpath(target), os.path.realpath(target)}
        if call.get("name") == "Bash" and isinstance(command, str):
            commands.append(command)
    found = []
    for path, info in memory_files(directory).items():
        cap_kb = settings.max_index_kb if path.name == INDEX else settings.max_file_kb
        if info.st_size <= cap_kb * 1024:
            continue
        before = None if baseline is None else baseline.get(path.relative_to(directory).as_posix())
        if baseline is None and info.st_mtime < checked.started:
            continue
        if baseline is not None and isinstance(before, int) and info.st_size <= before:
            continue
        by_bash = any(bash_writes(command, path, own) for command in commands)
        by_tool = bool({os.path.normpath(path), os.path.realpath(path)} & written)
        if by_bash or by_tool:
            found.append(Oversized(path, info.st_size, cap_kb, by_bash))
    return found, checked.key


def state_path(event: Mapping[str, Any], environ: Mapping[str, str]) -> Path:
    agent = str(event.get("agent_id") or "agent").removeprefix("agent-")
    run = f"{event.get('session_id') or 'session'}-{agent}"
    return harness_home(environ) / "memory-cap" / f"{UNSAFE.sub('-', run)}.json"


def read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def record(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, state)


def remove_old(directory: Path, now: float) -> None:
    for old in directory.glob("*.json"):
        try:
            if old.stat().st_mtime < now - KEEP_SECONDS:
                old.unlink()
        except OSError:
            continue


def start(event: Mapping[str, Any], environ: Mapping[str, str]) -> None:
    """SubagentStart: the sizes the run starts from, replacing an earlier run's state."""
    agent_type = team_agent(event)
    if agent_type is None:
        return
    directory = memory_dir(agent_type)
    sizes = {
        path.relative_to(directory).as_posix(): info.st_size
        for path, info in memory_files(directory).items()
    }
    path = state_path(event, environ)
    try:
        record(path, {"baseline": sizes, "started": time.time()})
        remove_old(path.parent, time.time())
    except OSError:
        return


def listing(found: list[Oversized]) -> str:
    return "\n".join(
        f"- {item.path}: {item.size / 1024:.1f} KB, over its {item.cap_kb:g} KB cap"
        + (", written through Bash, which skips the size check" if item.through_bash else "")
        for item in found
    )


def reason(found: list[Oversized], at_handback: bool) -> str:
    retry = (
        "Then call SubagentHandback again with the whole report."
        if at_handback
        else "Then reply with the whole report again: your last message is what the team "
        "lead receives."
    )
    return (
        "Your memory is over its size cap after this run's changes:\n"
        f"{listing(found)}\n"
        "Move detail into a topic file linked from MEMORY.md rather than deleting lessons, and "
        "write memory only with Edit or Write; a write that shrinks a file is always allowed. "
        f"{retry}"
    )


def warning(agent_type: str, found: list[Oversized]) -> dict[str, Any]:
    return {
        "systemMessage": f"agent-harness: {agent_type} left its memory over the size cap after "
        f"this run's changes and {MAX_BLOCKS} block:\n{listing(found)}"
    }


def unrecorded(path: Path, error: OSError, found: list[Oversized]) -> dict[str, Any]:
    why = f"{path.parent} is not writable ({error.strerror or error})"
    return {
        "systemMessage": f"agent-harness could not run the memory cap check, {why}. Check "
        f"HARNESS_HOME. Over the cap:\n{listing(found)}"
    }


def deny(text: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": text,
        }
    }


def decision(event: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any] | None:
    if event.get("hook_event_name") == "SubagentStart":
        start(event, environ)
        return None
    path = state_path(event, environ)
    state = read_state(path)
    verdict = check(event, environ, state)
    if verdict is None or not verdict[0]:
        return None
    found, key = verdict
    at_handback = event.get("hook_event_name") == "PreToolUse"
    same = state.get("segment") == key
    if same and not at_handback and state.get("delivered") is True:
        return None
    blocks = state.get("blocks") if same else 0
    blocks = blocks if isinstance(blocks, int) and not isinstance(blocks, bool) else 0
    again = blocks < MAX_BLOCKS
    update = {**state, "segment": key, "blocks": blocks + 1 if again else blocks}
    update["delivered"] = at_handback and not again
    try:
        record(path, update)
    except OSError as error:
        return unrecorded(path, error, found)
    if not again:
        return warning(str(event.get("agent_type")), found)
    if at_handback:
        return deny(reason(found, True))
    return {"decision": "block", "reason": reason(found, False)}


def main(
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    environ: Mapping[str, str] | None = None,
) -> int:
    try:
        verdict = decision(json.load(stdin), os.environ if environ is None else environ)
    except Exception as error:
        print(
            f"memory_cap.py let the agent through after an internal error: {error!r}",
            file=sys.stderr,
        )
        return 0
    if verdict is not None:
        json.dump(verdict, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
