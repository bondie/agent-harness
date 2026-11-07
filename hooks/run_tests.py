#!/usr/bin/env python3
"""PostToolUse hook: run the project's tests after the implementer edits a file.

The command comes from HARNESS_TEST_COMMAND, set in the project's `.claude/settings.json`
under "env". `{file}` in it becomes the edited path relative to the project. A failure exits
with 2, which hands the test output back to the implementer. An edit in another git
repository (not the project's, one above it or one nested in it) runs that repository's own
command and timeout from the "env" of its `.claude/settings.local.json` or
`.claude/settings.json`, in its root; when it has none, the hook runs no tests and, if the
project has a command, tells the implementer so. Standard library only.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

AGENT = "implementer"
TAIL_LINES = 60
SETTINGS = ("settings.json", "settings.local.json")
DEFAULT_TIMEOUT = 300.0


def is_implementer(agent_type: Any) -> bool:
    return isinstance(agent_type, str) and agent_type.split(":")[-1] == AGENT


def repository(path: str) -> Path | None:
    if not path:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(Path(path).parent), "rev-parse", "--show-toplevel"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def is_session_repository(project: Path, repo: Path) -> bool:
    project = project.resolve()
    return project == repo or repo in project.parents or project in repo.parents


def relative_path(edited: str, base: Path) -> str:
    # git reports the real path, so the edited path has to drop symlinks in its folders too.
    return os.path.relpath(Path(edited).parent.resolve() / Path(edited).name, base.resolve())


def repository_env(repo: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for name in SETTINGS:
        try:
            settings = json.loads((repo / ".claude" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        values = settings.get("env") if isinstance(settings, dict) else None
        if isinstance(values, dict):
            env.update({str(key): str(value) for key, value in values.items() if value is not None})
    return env


def no_tests_notice(repo: Path, relative: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": (
                f"No tests ran after editing {relative} because {repo} sets no "
                "HARNESS_TEST_COMMAND in the env of its .claude/settings.local.json or "
                ".claude/settings.json. In your report, name the tests in that repository that "
                "cover the change and the command that runs them, so the team lead can decide "
                "whether to run them."
            ),
        }
    }


def timeout_seconds(env: Mapping[str, str]) -> float:
    try:
        timeout = float(env.get("HARNESS_TEST_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT
    if not math.isfinite(timeout) or timeout <= 0:
        return DEFAULT_TIMEOUT
    return timeout


def run_tests(env: Mapping[str, str], cwd: Path, relative: str) -> int:
    command = env.get("HARNESS_TEST_COMMAND", "").strip()
    if not command:
        return 0
    command = command.replace("{file}", shlex.quote(relative))
    timeout = timeout_seconds(env)
    try:
        # The command is the project's own configuration, run as the user would type it.
        result = subprocess.run(  # noqa: S602
            command,
            shell=True,
            cwd=cwd,
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


def main(
    stdin: TextIO = sys.stdin,
    environ: Mapping[str, str] = os.environ,
    stdout: TextIO = sys.stdout,
) -> int:
    event: dict[str, Any] = json.load(stdin)
    if not is_implementer(event.get("agent_type")):
        return 0
    project = Path(environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
    edited = (event.get("tool_input") or {}).get("file_path") or ""
    repo = repository(edited)
    if repo is None or is_session_repository(project, repo):
        return run_tests(environ, project, relative_path(edited, project) if edited else "")
    relative = relative_path(edited, repo)
    env = repository_env(repo)
    if env.get("HARNESS_TEST_COMMAND", "").strip():
        return run_tests(env, repo, relative)
    if environ.get("HARNESS_TEST_COMMAND", "").strip():
        json.dump(no_tests_notice(repo, relative), stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
