#!/usr/bin/env python3
"""SubagentStop hook: run the repository's checks on the files the implementer changed.

The checks are `gate` in the repository's `.claude/agent-harness.json`:
`{"commands": [{"cmd": "ruff check {files}", "match": "\\.py$"}], "timeout": 300,
"max_blocks": 2}`. HARNESS_GATE_COMMANDS in the env (that object or just its command list, as
JSON) wins over the file: the session's env for the session's repository, the env of its
`.claude/settings.local.json` or `.claude/settings.json` for another one. Without either the
hook does nothing. The changed files are those in the implementer's `harness-implementation`
block, else the uncommitted files of the repository it worked in. Each command runs in its
repository's root with `{files}` replaced by the changed files matching `match`, relative to
that root; a command with no matching file is skipped. The result goes to `gates.json` in the
ticket's artifacts directory. A failure blocks the implementer from finishing and hands it the
output, at most `max_blocks` times in a row; after that it may finish and the gate stays
failed. Standard library only, and it never fails the session.
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, TextIO

from run_tests import is_implementer, is_session_repository, repository_env
from save_artifacts import (
    final_report,
    harness_home,
    locked,
    now,
    read_transcript,
    structured_blocks,
    ticket_key,
    write_json,
)

CONFIG = Path(".claude") / "agent-harness.json"
STATE = "gate-blocks.json"
TAIL_LINES = 40
DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_BLOCKS = 2
# Stays below the 600 s the plugin gives this hook, so it can still record the result.
BUDGET = 570.0


@dataclass(frozen=True)
class Check:
    cmd: str
    match: re.Pattern[str] | None

    def files(self, changed: list[str]) -> list[str]:
        return [name for name in changed if self.match is None or self.match.search(name)]


@dataclass(frozen=True)
class Gate:
    checks: tuple[Check, ...]
    timeout: float
    max_blocks: int


def parse_check(item: Any) -> Check | None:
    if not isinstance(item, dict) or not isinstance(item.get("cmd"), str):
        return None
    if not item["cmd"].strip():
        return None
    pattern = item.get("match")
    if pattern is None:
        return Check(item["cmd"], None)
    if not isinstance(pattern, str):
        return None
    try:
        return Check(item["cmd"], re.compile(pattern))
    except re.error:
        return None


def timeout_setting(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return DEFAULT_TIMEOUT
    try:
        timeout = float(value)
    except (ValueError, OverflowError):
        return DEFAULT_TIMEOUT
    if not math.isfinite(timeout) or timeout <= 0:
        return DEFAULT_TIMEOUT
    return timeout


def max_blocks_setting(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return DEFAULT_MAX_BLOCKS
    return value


def parse_gate(value: Any) -> Gate | None:
    if isinstance(value, list):
        value = {"commands": value}
    if not isinstance(value, dict) or not isinstance(value.get("commands"), list):
        return None
    checks = tuple(check for check in map(parse_check, value["commands"]) if check)
    if not checks:
        return None
    return Gate(
        checks, timeout_setting(value.get("timeout")), max_blocks_setting(value.get("max_blocks"))
    )


def env_gate(env: Mapping[str, str]) -> Gate | None:
    raw = env.get("HARNESS_GATE_COMMANDS", "").strip()
    if not raw:
        return None
    try:
        return parse_gate(json.loads(raw))
    except (ValueError, RecursionError):
        return None


def file_gate(repo: Path) -> Gate | None:
    try:
        config = json.loads((repo / CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(config, dict):
        return None
    return parse_gate(config.get("gate"))


def repository_gate(repo: Path, project: Path, environ: Mapping[str, str]) -> Gate | None:
    if is_session_repository(project, repo):
        return env_gate(environ) or file_gate(repo)
    return env_gate(repository_env(repo)) or file_gate(repo)


@cache
def toplevel(directory: Path) -> Path | None:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],  # noqa: S607
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


def uncommitted(repo: Path) -> list[Path]:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "status", "--porcelain=v1", "-z", "--untracked-files=all"],  # noqa: S607
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    paths, renamed = [], False
    for item in result.stdout.split("\0"):
        if renamed or len(item) < 4:
            renamed = False
            continue
        renamed = bool({"R", "C"} & set(item[:2]))
        paths.append(repo / item[3:])
    return paths


def listed(names: list[Any], cwd: Path, root: Path | None) -> list[Path]:
    bases = [base for base in (cwd, root) if base is not None]
    paths = []
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        path = Path(name).expanduser()
        if path.is_absolute():
            paths.append(path)
            continue
        found = [base / path for base in bases if (base / path).exists()]
        paths.append((found or [cwd / path])[0])
    return paths


def implementation_files(report: str) -> list[Any]:
    files: list[Any] = []
    for block in structured_blocks(report):
        value = block.value or {}
        if block.kind == "implementation" and isinstance(value.get("files"), list):
            files = value["files"]
    return files


def changed_files(cwd: Path, root: Path | None, report: str) -> dict[Path, list[str]]:
    names = implementation_files(report)
    paths = listed(names, cwd, root)
    if not names and root is not None:
        paths = uncommitted(root)
    grouped: dict[Path, list[str]] = {}
    for path in paths:
        if not path.is_file():
            continue
        resolved = path.resolve()
        repo = toplevel(resolved.parent)
        if repo is None:
            continue
        relative = Path(os.path.relpath(resolved, repo)).as_posix()
        files = grouped.setdefault(repo, [])
        if relative not in files:
            files.append(relative)
    return grouped


def tail(output: str) -> str:
    return "\n".join(output.rstrip().splitlines()[-TAIL_LINES:])


def kill(process: subprocess.Popen[str]) -> None:
    try:
        if sys.platform == "win32":
            process.kill()
            return
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        return


def drain(process: subprocess.Popen[str]) -> str:
    try:
        output, _ = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        return ""
    return output or ""


def run_check(command: str, repo: Path, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    entry: dict[str, Any] = {"cmd": command, "repo": str(repo), "exit": None}
    try:
        # The command is the repository's own configuration, run as the user would type it.
        process = subprocess.Popen(  # noqa: S602
            command,
            shell=True,
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as error:
        return {**entry, "seconds": 0.0, "output_tail": f"Could not start: {error}"}
    try:
        output, _ = process.communicate(timeout=timeout)
        entry["exit"] = process.returncode
    except subprocess.TimeoutExpired:
        # A shell runs its commands as children; killing the whole group frees the pipe.
        kill(process)
        output = drain(process) + f"\nDid not finish within {round(timeout, 1):g} s."
    seconds = round(time.monotonic() - started, 1)
    return {**entry, "seconds": seconds, "output_tail": tail(output or "")}


def run_gates(gates: dict[Path, Gate], changed: dict[Path, list[str]]) -> list[dict[str, Any]]:
    deadline = time.monotonic() + BUDGET
    results = []
    for repo, gate in gates.items():
        for check in gate.checks:
            files = check.files(changed.get(repo, []))
            if not files:
                continue
            command = check.cmd.replace("{files}", " ".join(map(shlex.quote, files)))
            remaining = deadline - time.monotonic()
            if remaining < 1:
                ran_out = "Not run: the gate ran out of time."
                entry = {"cmd": command, "repo": str(repo), "exit": None, "seconds": 0.0}
                results.append({**entry, "output_tail": ran_out})
                continue
            results.append(run_check(command, repo, min(gate.timeout, remaining)))
    return results


def read_state(path: Path) -> dict[str, int]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    return {
        str(key): value
        for key, value in state.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def block_reason(commands: list[dict[str, Any]], blocks: int, limit: int) -> str:
    parts = [
        f"The quality gate failed (block {blocks} of at most {limit}), so you cannot finish yet. "
        "Fix the cause, run the check again and show its output in your report; never weaken "
        "or skip the check. When the failure is not caused by your change and fixing it is "
        "outside the approved scope, say so in your report instead."
    ]
    for command in commands:
        if command["exit"] == 0:
            continue
        status = f"exit {command['exit']}"
        if command["exit"] is None:
            status = "did not finish"
        parts.append(
            f"$ {command['cmd']}\n(in {command['repo']}, {status})\n{command['output_tail']}"
        )
    return "\n\n".join(parts)


def gate(event: dict[str, Any], environ: Mapping[str, str], stdout: TextIO) -> None:
    if not is_implementer(event.get("agent_type")):
        return
    entries = read_transcript(event.get("agent_transcript_path"))
    report = final_report(entries, event)
    cwd = Path(str(event.get("cwd") or "."))
    root = toplevel(cwd.resolve())
    changed = changed_files(cwd, root, report)
    project = Path(environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
    repos = list(dict.fromkeys(repo for repo in (root, *changed) if repo is not None))
    gates = {repo: found for repo in repos if (found := repository_gate(repo, project, environ))}
    if not gates:
        return
    commands = run_gates(gates, changed)
    passed = all(command["exit"] == 0 for command in commands)
    limit = min(found.max_blocks for found in gates.values())
    key = ticket_key(event, entries, environ)
    directory = harness_home(environ) / "tickets" / key
    agent_id = str(event.get("agent_id") or "")
    with locked(directory):
        state = read_state(directory / STATE)
        used = state.pop(agent_id, 0)
        if event.get("stop_hook_active") is False:
            used = 0
        blocks = used
        if not passed and used < limit:
            blocks = used + 1
            state[agent_id] = blocks
        write_json(directory / STATE, state)
        result = {"ticket": key, "ran_at": now(), "repo": str(repos[0]), "passed": passed}
        write_json(directory / "gates.json", {**result, "blocks": blocks, "commands": commands})
    if blocks > used:
        json.dump({"decision": "block", "reason": block_reason(commands, blocks, limit)}, stdout)
        return
    if passed:
        return
    failing = ", ".join(command["cmd"] for command in commands if command["exit"] != 0)
    message = (
        f"The quality gate still fails and the implementer finished anyway: {failing}. "
        f"See {directory / 'gates.json'}."
    )
    json.dump({"systemMessage": message}, stdout)


def main(
    stdin: TextIO = sys.stdin,
    environ: Mapping[str, str] = os.environ,
    stdout: TextIO = sys.stdout,
) -> int:
    try:
        event = json.load(stdin)
        if isinstance(event, dict):
            gate(event, environ, stdout)
    except Exception as error:
        print(f"quality_gate: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
