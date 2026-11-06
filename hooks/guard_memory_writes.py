#!/usr/bin/env python3
"""PreToolUse hook: the analyst and the estimator write only to their own memory.

Their `memory` front matter gives them Write and Edit so that they can keep notes between runs
in `agent-memory/<agent>/`. Every other file stays read-only for them. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import PurePath
from typing import Any, TextIO

READ_ONLY_AGENTS = {"jira-analyst", "estimator"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
MEMORY_DIRS = {"agent-memory", "agent-memory-local"}


def memory_dir_name(agent_type: str) -> str:
    return re.sub(r"[^a-zA-Z0-9\-_]", "-", agent_type) or "unknown"


def in_own_memory(path: str, agent_type: str) -> bool:
    parts = PurePath(os.path.normpath(path)).parts
    own = memory_dir_name(agent_type)
    return any(
        parts[i] in MEMORY_DIRS and parts[i + 1] == own and i + 2 < len(parts)
        for i in range(len(parts) - 1)
    )


def decision(event: dict[str, Any]) -> dict[str, Any] | None:
    agent_type = event.get("agent_type")
    tool = event.get("tool_name")
    if not isinstance(agent_type, str) or agent_type.split(":")[-1] not in READ_ONLY_AGENTS:
        return None
    if tool not in WRITE_TOOLS:
        return None
    tool_input = event.get("tool_input") or {}
    target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
    if target and not PurePath(target).is_absolute():
        target = str(PurePath(str(event.get("cwd") or "")) / target)
    if target and in_own_memory(target, agent_type):
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"The {agent_type.split(':')[-1]} writes only to its memory directory "
                f"(agent-memory/{memory_dir_name(agent_type)}/). Put proposed changes to "
                "other files in your report."
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
