#!/usr/bin/env python3
"""PreToolUse hook: subagents may read issue trackers but never write to them.

Writes are the team lead's job, after a person approves them; in the main session the normal
permission prompt still applies. Standard library only.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any, TextIO

TRACKER = re.compile(r"jira|atlassian|mantis|helpdesk|youtrack|linear|github|gitlab", re.I)
WRITE = re.compile(
    r"(^|_)(add|create|update|delete|remove|transition|assign|upload|edit|log_work|post|close"
    r"|merge|link|unlink)(_|$)",
    re.I,
)


def decision(event: dict[str, Any]) -> dict[str, Any] | None:
    tool = str(event.get("tool_name") or "")
    if not event.get("agent_type") or not tool.startswith("mcp__"):
        return None
    parts = tool.split("__", 2)
    if len(parts) < 3:
        return None
    server, name = parts[1], parts[2]
    if not TRACKER.search(f"{server} {name}") or not WRITE.search(name):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Subagents may not write to the issue tracker ({name}). Put the proposed "
                "change in your report; the team lead asks the person and writes it."
            ),
        }
    }


def main(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    verdict = decision(json.load(stdin))
    if verdict is not None:
        json.dump(verdict, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
