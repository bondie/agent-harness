#!/usr/bin/env python3
"""SubagentStop hook: append one line per finished subagent run to a JSONL log.

The log is what the `stats` skill and the retro read to see whether the agents get faster
and cheaper. Each line has the agent type, the plugin version, the model, the duration, the
API requests, the tool calls, the token usage and the first ticket key in the task. A
background agent that is resumed
stops more than once; its later lines carry the cumulative totals, so readers keep the
last line per agent id. Standard library only, and it never blocks the session.

The memory fields count characters of the agent's own memory directory, for an agent that has
one (`~/.claude/agent-memory/<agent>/` exists, or its prompt names it): the index in its system
prompt (`memory_injected_chars`, from the undocumented `prompt_snapshot` attachment, else the
size of `MEMORY.md` at the stop, else left out), the Read results of its files, without a Read
that an Edit, Write or MultiEdit of the same file follows within the agent's next
READ_FOR_EDIT_CALLS tool calls, each counted once (`memory_read_chars`), and the output of Bash
commands that name the directory, an upper bound (`memory_bash_chars`). Without its sibling
`guard_memory_writes.py` the line has no memory fields.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple, TextIO

TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
INDEX = "MEMORY.md"
# Claude Code puts the memory index under this heading, at the end of the memory instructions.
INDEX_HEADING = f"\n## {INDEX}\n"
WRITES = {"Edit", "Write", "MultiEdit"}
READ_FOR_EDIT_CALLS = 3


class Guard(NamedTuple):
    memory_dir_name: Callable[[str], str]
    in_own_memory: Callable[[str, str], bool]


def load_guard() -> Guard | None:
    try:
        from guard_memory_writes import in_own_memory, memory_dir_name
    except ImportError:
        return None
    return Guard(memory_dir_name, in_own_memory)


GUARD = load_guard()


def log_path(environ: dict[str, str]) -> Path:
    custom = environ.get("HARNESS_RUNS_LOG")
    return Path(custom) if custom else Path.home() / ".claude" / "agent-harness" / "runs.jsonl"


def text_of(message: Any) -> str:
    content = message.get("content") if isinstance(message, dict) else message
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return round((parse_time(end) - parse_time(start)).total_seconds(), 1)


def result_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return sum(len(text) for text in texts if isinstance(text, str))


def memory_part(attachment: Any) -> str | None:
    """The system prompt part of a `prompt_snapshot` attachment that holds the memory index."""
    if not isinstance(attachment, dict) or attachment.get("type") != "prompt_snapshot":
        return None
    parts = attachment.get("systemPrompt")
    for part in parts if isinstance(parts, list) else []:
        if isinstance(part, str) and INDEX_HEADING in part:
            return part
    return None


def user_memory(own: str) -> Path:
    return Path.home() / ".claude" / "agent-memory" / own


def index_on_disk(own: str) -> int | None:
    path = user_memory(own) / INDEX
    try:
        return len(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


class MemoryShare:
    """What one run spent on its own memory, collected in the pass over the transcript."""

    def __init__(self, agent_type: str, guard: Guard) -> None:
        self.agent_type = agent_type
        self.guard = guard
        self.own = guard.memory_dir_name(agent_type)
        own = re.escape(self.own)
        self.named = re.compile(rf"(?<![\w-])agent-memory(?:-local)?/{own}(?![\w-])")
        self.in_prompt = re.compile(rf"agent-memory(?:-local)?/{own}/")
        self.snapshot: str | None = None
        self.calls: dict[str, tuple[str, str, int]] = {}
        self.chars: dict[str, int] = {}
        self.written: dict[str, list[int]] = {}
        self.seen: set[Any] = set()
        self.uses = 0

    def add(self, kind: Any, attachment: Any, items: list[dict[str, Any]]) -> None:
        """One transcript entry; replays of earlier calls (`api-request-blob`) are skipped."""
        if self.snapshot is None:
            self.snapshot = memory_part(attachment)
        for item in items:
            if kind == "assistant" and item.get("type") == "tool_use":
                self.first_tool_use(item)
            call = item.get("tool_use_id")
            if item.get("type") == "tool_result" and call in self.calls:
                self.chars[call] = result_chars(item.get("content"))

    def first_tool_use(self, item: dict[str, Any]) -> None:
        if item.get("id") in self.seen:
            return
        self.seen.add(item.get("id"))
        self.tool_use(item)

    def tool_use(self, item: dict[str, Any]) -> None:
        self.uses += 1
        tool, call = item.get("name"), item.get("id")
        tool_input = item.get("input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        path, command = tool_input.get("file_path"), tool_input.get("command")
        own = (
            isinstance(path, str) and bool(path) and self.guard.in_own_memory(path, self.agent_type)
        )
        target = os.path.normpath(str(path)) if own else ""
        if tool in WRITES and own:
            self.written.setdefault(target, []).append(self.uses)
        if not isinstance(call, str):
            return
        if tool == "Read" and own:
            self.calls[call] = ("read", target, self.uses)
        if tool == "Bash" and isinstance(command, str) and self.named.search(command):
            self.calls[call] = ("bash", "", self.uses)

    def for_edit(self, path: str, position: int) -> bool:
        writes = self.written.get(path, [])
        return any(position < write <= position + READ_FOR_EDIT_CALLS for write in writes)

    def fields(self) -> dict[str, int]:
        named = self.snapshot is not None and self.in_prompt.search(self.snapshot) is not None
        if not named and not user_memory(self.own).is_dir():
            return {}
        found = {"memory_read_chars": 0, "memory_bash_chars": 0}
        for call, (kind, path, position) in self.calls.items():
            if kind == "read" and self.for_edit(path, position):
                continue
            found[f"memory_{kind}_chars"] += self.chars.get(call, 0)
        injected = index_on_disk(self.own)
        if named and self.snapshot is not None:
            injected = len(self.snapshot.rsplit(INDEX_HEADING, 1)[1].strip())
        return found if injected is None else {"memory_injected_chars": injected, **found}


def summarise(transcript: Path, agent_type: str | None = None) -> dict[str, Any]:
    stamps: list[str] = []
    requests: set[Any] = set()
    tokens = dict.fromkeys(TOKEN_FIELDS, 0)
    tool_uses = 0
    model = ticket = None
    share = MemoryShare(agent_type, GUARD) if agent_type and GUARD is not None else None
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("timestamp"):
            stamps.append(entry["timestamp"])
        message = entry.get("message") or {}
        if share is not None:
            content = message.get("content") if isinstance(message, dict) else None
            items = [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []
            share.add(entry.get("type"), entry.get("attachment"), items)
        if entry.get("type") == "user" and ticket is None:
            found = TICKET.search(text_of(message))
            ticket = found.group(0) if found else None
        if entry.get("type") != "assistant":
            continue
        model = model or message.get("model")
        content = message.get("content") or []
        tool_uses += sum(1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use")
        request = entry.get("requestId") or message.get("id")
        if request in requests:
            continue
        requests.add(request)
        usage = message.get("usage") or {}
        for field in TOKEN_FIELDS:
            tokens[field] += int(usage.get(field) or 0)
    return {
        "model": model,
        "ticket": ticket,
        "started": stamps[0] if stamps else None,
        "ended": stamps[-1] if stamps else None,
        "seconds": seconds(stamps[0] if stamps else None, stamps[-1] if stamps else None),
        "requests": len(requests),
        "tool_uses": tool_uses,
        "tokens": tokens,
        **(share.fields() if share is not None else {}),
    }


def project_name(cwd: str) -> str | None:
    if not cwd:
        return None
    path = Path(cwd)
    for folder in (path, *path.parents):
        if (folder / ".git").exists():
            return folder.name or None
    return path.name or None


def plugin_version() -> str | None:
    manifest_path = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = manifest.get("version") if isinstance(manifest, dict) else None
    return version if isinstance(version, str) else None


def record(event: dict[str, Any]) -> dict[str, Any] | None:
    transcript = event.get("agent_transcript_path")
    if not transcript or not Path(transcript).is_file():
        return None
    meta_path = Path(transcript).with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    agent_type = event.get("agent_type") or meta.get("agentType")
    return {
        "session": event.get("session_id"),
        "project": project_name(str(event.get("cwd") or "")),
        "agent_id": event.get("agent_id"),
        "agent_type": agent_type,
        "description": meta.get("description"),
        "plugin_version": plugin_version(),
        **summarise(Path(transcript), agent_type if isinstance(agent_type, str) else None),
    }


def main(stdin: TextIO = sys.stdin, environ: dict[str, str] | None = None) -> int:
    try:
        line = record(json.load(stdin))
        if line is not None:
            path = log_path(dict(os.environ) if environ is None else environ)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(line, ensure_ascii=False) + "\n")
    except (OSError, ValueError, TypeError):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
