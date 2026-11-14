#!/usr/bin/env python3
"""PreToolUse hook: memory lessons carry a source and a date; read-only agents write only there.

The analyst, the estimator and the reviewer get Write and Edit from their `memory` front
matter so that they can keep notes between runs in `agent-memory/<agent>/`; every other file
stays read-only for them. For all four agents, every new or changed lesson in their own memory
must end with its source and date, and a memory file must stay under its size cap, set in
`$HARNESS_HOME/memory.json` (`~/.claude/agent-harness/memory.json`). Lines already in the file
are not checked, so older notes do not block a write. Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import date
from pathlib import Path, PurePath
from typing import Any, NamedTuple, TextIO

READ_ONLY_AGENTS = {"jira-analyst", "estimator", "reviewer-qa"}
TEAM = READ_ONLY_AGENTS | {"implementer"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
MEMORY_DIRS = {"agent-memory", "agent-memory-local"}
INDEX = "MEMORY.md"

FORMAT = "- <lesson text> (source: <KEY|MR|commit|retro>, YYYY-MM-DD[, expires YYYY-MM-DD])"
EXAMPLE = "- The specs need a running database (source: SHOP-412, 2026-01-15)"
BULLET = re.compile(r"[-*+]\s")
INDEX_LINK = re.compile(r"[-*+]\s+\[[^\]]+\]\((?![a-zA-Z][\w+.-]*:)[^)\s]+\.md\)")
TRAILER = re.compile(
    r"\((?:source:\s*(?P<source>[^,()]*?)\s*,\s*)?(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?:\s*,\s*expires\s+(?P<expires>\d{4}-\d{2}-\d{2}))?\)[ \t]*$",
    re.M,
)
FENCE = re.compile(r"(`{3,}|~{3,})")
SHOWN_FAILURES = 5


class Settings(NamedTuple):
    max_file_kb: float = 8
    max_index_kb: float = 4
    require_source: bool = True
    warn_ratio: float = 0.8


def memory_dir_name(agent_type: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-_]", "-", agent_type) or "unknown"


def in_own_memory(path: str, agent_type: str) -> bool:
    parts = PurePath(os.path.normpath(path)).parts
    own = memory_dir_name(agent_type)
    return any(
        parts[i] in MEMORY_DIRS and parts[i + 1] == own and i + 2 < len(parts)
        for i in range(len(parts) - 1)
    )


def opens(line: str) -> str:
    """The fence a line opens: a run of three or more backticks or tildes, or nothing."""
    match = FENCE.match(line)
    if match is None:
        return ""
    return match[1]


def closes(line: str, fence: str) -> bool:
    """A run of the fence's character at least as long as it, with only spaces after."""
    pattern = f"{re.escape(fence[0])}{{{len(fence)},}}[ \t]*"
    return re.fullmatch(pattern, line.rstrip("\r\n")) is not None


def front_matter_end(lines: list[str]) -> int:
    if not lines or lines[0].strip() != "---":
        return 0
    for number, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            return number + 1
    return 0


def lessons(text: str, index: bool = False) -> list[tuple[int, str]]:
    """Each lesson as (line number, its bullet line and continuation lines)."""
    found: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    fence = ""
    lines = text.splitlines()
    start = front_matter_end(lines)
    for number, line in enumerate(lines[start:], start + 1):
        if fence:
            if closes(line, fence):
                fence = ""
            continue
        if opens(line):
            fence, current = opens(line), None
            continue
        if current is not None and line.startswith(("  ", "\t")) and line.strip():
            current.append(line.rstrip())
            continue
        current = None
        if not BULLET.match(line) or (index and INDEX_LINK.match(line)):
            continue
        current = [line.rstrip()]
        found.append((number, current))
    return [(number, "\n".join(body)) for number, body in found]


def valid_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value or "")
    except ValueError:
        return None


def problem(lesson: str, require_source: bool) -> str | None:
    trailers = list(TRAILER.finditer(lesson))
    if not trailers:
        return "it does not end with (source: …, YYYY-MM-DD)"
    trailer = trailers[-1]
    day = valid_date(trailer["date"])
    if day is None:
        return f"{trailer['date']} is not a date"
    expires = trailer["expires"]
    end = valid_date(expires)
    if expires is not None and (end is None or end <= day):
        return f"expires {expires} is not a date after {day}"
    if require_source and not (trailer["source"] or "").strip():
        return "it has no source"
    return None


def size_kb(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return float(value)


def ratio(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    if not math.isfinite(value) or not 0 < value <= 1:
        return default
    return float(value)


def harness_home(environ: dict[str, str]) -> Path:
    custom = environ.get("HARNESS_HOME", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".claude" / "agent-harness"


def load_settings(environ: dict[str, str]) -> Settings:
    try:
        raw = json.loads((harness_home(environ) / "memory.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    defaults = Settings()
    require_source = raw.get("require_source")
    return Settings(
        size_kb(raw.get("max_file_kb"), defaults.max_file_kb),
        size_kb(raw.get("max_index_kb"), defaults.max_index_kb),
        require_source if isinstance(require_source, bool) else defaults.require_source,
        ratio(raw.get("warn_ratio"), defaults.warn_ratio),
    )


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def replacements(tool: str, tool_input: dict[str, Any]) -> list[tuple[str, str, bool]]:
    edits = [tool_input] if tool == "Edit" else tool_input.get("edits")
    if not isinstance(edits, list):
        return []
    return [
        (
            as_text(edit.get("old_string")),
            as_text(edit.get("new_string")),
            edit.get("replace_all") is True,
        )
        for edit in edits
        if isinstance(edit, dict)
    ]


def after_edits(current: str, edits: list[tuple[str, str, bool]]) -> str | None:
    result = current
    for old, new, replace_all in edits:
        if old not in result:
            return None
        result = result.replace(old, new) if replace_all else result.replace(old, new, 1)
    return result


def proposal(
    tool: str, tool_input: dict[str, Any], current: str
) -> tuple[str | None, list[tuple[str, str]], str]:
    """The file after the write, each (old, new) text pair to compare, and how lines are named."""
    if tool == "Write":
        content = as_text(tool_input.get("content"))
        return content, [(current, content)], "line {}"
    edits = replacements(tool, tool_input)
    pairs = [(old, new) for old, new, _ in edits]
    return after_edits(current, edits), pairs, "line {} of new_string"


def failures(
    changes: list[tuple[str, str]], kept: set[str], index: bool, require_source: bool, where: str
) -> list[str]:
    found = []
    for before, after in changes:
        unchanged = kept | {lesson for _, lesson in lessons(before, index)}
        for number, lesson in lessons(after, index):
            reason = None if lesson in unchanged else problem(lesson, require_source)
            if reason is None:
                continue
            first = lesson.splitlines()[0]
            shown = first if len(first) <= 160 else first[:159] + "…"
            found.append(f"- {where.format(number)}: {reason}: `{shown}`")
    return found


def lesson_reason(name: str, found: list[str]) -> str:
    more = len(found) - SHOWN_FAILURES
    listed = found[:SHOWN_FAILURES] + ([f"- and {more} more"] if more > 0 else [])
    return "\n".join(
        [
            f"Every new or changed lesson in {name} needs its source and date.",
            *listed,
            f"Expected: `{FORMAT}`, e.g. `{EXAMPLE}`. Today is {date.today()}. A lesson may "
            "continue on lines indented by two spaces. Index links in MEMORY.md "
            "(`- [Title](file.md) — hook`) and lines already in the file are not checked.",
        ]
    )


def size_reason(name: str, size: int, limit_kb: float, index: bool) -> str:
    kind = "the memory index" if index else "a memory file"
    return (
        f"{name} would grow to {size / 1024:.1f} KB, over the {limit_kb:g} KB cap for {kind}: "
        "compact or move detail to a topic file. A write that shrinks the file is always "
        "allowed."
    )


def memory_problem(
    tool: str, tool_input: dict[str, Any], path: Path, settings: Settings
) -> str | None:
    if tool == "NotebookEdit":
        return None
    index = path.name == INDEX
    current = read(path)
    result, changes, where = proposal(tool, tool_input, current)
    kept = {lesson for _, lesson in lessons(current, index)}
    found = failures(changes, kept, index, settings.require_source, where)
    if found:
        return lesson_reason(path.name, found)
    if result is None:
        return None
    limit_kb = settings.max_index_kb if index else settings.max_file_kb
    size = len(result.encode("utf-8"))
    if size <= limit_kb * 1024 or size <= len(current.encode("utf-8")):
        return None
    return size_reason(path.name, size, limit_kb, index)


def deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def decision(event: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Any] | None:
    agent_type = event.get("agent_type")
    tool = event.get("tool_name")
    if not isinstance(agent_type, str) or tool not in WRITE_TOOLS:
        return None
    agent = agent_type.split(":")[-1]
    if agent not in TEAM:
        return None
    tool_input = event.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if target and not PurePath(target).is_absolute():
        target = str(PurePath(str(event.get("cwd") or "")) / target)
    if target and in_own_memory(target, agent_type):
        settings = load_settings(dict(os.environ) if environ is None else environ)
        reason = memory_problem(str(tool), tool_input, Path(os.path.normpath(target)), settings)
        return deny(reason) if reason else None
    if agent not in READ_ONLY_AGENTS:
        return None
    return deny(
        f"The {agent} writes only to its memory directory "
        f"(agent-memory/{memory_dir_name(agent_type)}/). Put proposed changes to "
        "other files in your report."
    )


def main(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    event: Any = None
    try:
        event = json.load(stdin)
        verdict = decision(event)
    except Exception as error:
        if not isinstance(event, dict) or not event.get("agent_type"):
            print(
                f"guard_memory_writes.py allowed the write after an internal error: {error!r}",
                file=sys.stderr,
            )
            return 0
        print(
            f"guard_memory_writes.py denied the write after an internal error: {error!r}",
            file=sys.stderr,
        )
        verdict = deny(
            f"The memory guard could not check this write ({type(error).__name__}: {error}). "
            "Report the error to the lead and let it decide."
        )
    if verdict is not None:
        json.dump(verdict, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
