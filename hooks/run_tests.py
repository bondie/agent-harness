#!/usr/bin/env python3
"""PostToolUse hook: run the project's tests after the implementer edits a file.

The command comes from HARNESS_TEST_COMMAND, set in the project's `.claude/settings.json`
under "env". `{file}` in it becomes the edited path relative to the project. A failure exits
with 2, which hands the test output back to the implementer. Standard library only.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

AGENT = "implementer"
TAIL_LINES = 60


def is_implementer(agent_type: Any) -> bool:
    return isinstance(agent_type, str) and agent_type.split(":")[-1] == AGENT


def main(stdin: TextIO = sys.stdin, environ: Mapping[str, str] = os.environ) -> int:
    event: dict[str, Any] = json.load(stdin)
    command = environ.get("HARNESS_TEST_COMMAND", "").strip()
    if not command or not is_implementer(event.get("agent_type")):
        return 0
    project = Path(environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
    edited = (event.get("tool_input") or {}).get("file_path") or ""
    relative = os.path.relpath(edited, project) if edited else ""
    command = command.replace("{file}", shlex.quote(relative))
    timeout = float(environ.get("HARNESS_TEST_TIMEOUT", "300"))
    try:
        # The command is the project's own configuration, run as the user would type it.
        result = subprocess.run(  # noqa: S602
            command,
            shell=True,
            cwd=project,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"Tests did not finish within {timeout:.0f} s: {command}", file=sys.stderr)
        return 2
    if result.returncode == 0:
        return 0
    output = (result.stdout + result.stderr).strip().splitlines()[-TAIL_LINES:]
    print(f"Tests failed after editing {relative or 'a file'} ({command}):", file=sys.stderr)
    print("\n".join(output), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
