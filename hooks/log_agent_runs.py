#!/usr/bin/env python3
"""SubagentStop hook: append one line per finished subagent run to a JSONL log.

The log is what the `stats` skill and the retro read to see whether the agents get faster
and cheaper. Each line has the agent type, the plugin version, the model, the duration, the
API requests, the tool calls, the token usage and the first ticket key in the task. A
background agent that is resumed
stops more than once; its later lines carry the cumulative totals, so readers keep the
last line per agent id. Standard library only, and it never blocks the session.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


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


def summarise(transcript: Path) -> dict[str, Any]:
    stamps: list[str] = []
    requests: set[Any] = set()
    tokens = dict.fromkeys(TOKEN_FIELDS, 0)
    tool_uses = 0
    model = ticket = None
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("timestamp"):
            stamps.append(entry["timestamp"])
        message = entry.get("message") or {}
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
    return {
        "session": event.get("session_id"),
        "project": project_name(str(event.get("cwd") or "")),
        "agent_id": event.get("agent_id"),
        "agent_type": event.get("agent_type") or meta.get("agentType"),
        "description": meta.get("description"),
        "plugin_version": plugin_version(),
        **summarise(Path(transcript)),
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
