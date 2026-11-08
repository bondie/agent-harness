#!/usr/bin/env python3
"""SubagentStop hook: run the repository's checks on the files the implementer changed.

The checks are `gate` in the repository's `.claude/agent-harness.json`:
`{"commands": [{"cmd": "ruff check {files}", "match": "\\.py$"}], "timeout": 300,
"max_blocks": 2}`. HARNESS_GATE_COMMANDS in the env (that object or just its command list, as
JSON) wins over the file: the session's env for the session's repository, the env of its
`.claude/settings.local.json` or `.claude/settings.json` for another one. Only the session's
repository and the repositories listed in its `gate.repositories` are trusted to configure
commands; changed files in any other repository are skipped with a note. Without a
configuration the hook does nothing. The changed files are those in the implementer's
`harness-implementation` block, else the uncommitted files of the repository it worked in.
Each command runs through the POSIX shell in its repository's root with `{files}` replaced by
the changed files matching `match`, already quoted and relative to that root; a command with
no matching file is skipped. The result goes to `gates.json` in the ticket's artifacts
directory. A failure blocks the implementer from finishing and hands it the output, at most
`max_blocks` times in a row; after that it may finish and the gate stays failed until the
next run of the agent. A retro run (task starting "Retro for") is not checked. Standard
library only, and it never fails the session.

In auto mode the implementer delivers its report through SubagentHandback before it stops, so
a SubagentStop block comes too late. The same script runs as a PreToolUse hook on that tool:
it checks the report in `tool_input.message` and denies the call on a failure, which sends the
implementer back to work. Both paths share one block count per run. Claude Code allows a
limited number of handback retries, so this path denies at most twice whatever `max_blocks`
says. When the handback passed the gate (or was let through after the last block),
SubagentStop does not run it again.
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
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from functools import cache
from pathlib import Path
from typing import Any, TextIO

from run_tests import is_implementer, repository_env
from save_artifacts import (
    HANDBACK,
    agent_transcript,
    final_report,
    harness_home,
    is_retro,
    locked,
    now,
    read_transcript,
    structured_blocks,
    ticket_key,
    unwritable,
    write_json,
)

CONFIG = Path(".claude") / "agent-harness.json"
STATE = "gate-blocks.json"
TAIL_LINES = 40
DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_BLOCKS = 2
HANDBACK_RETRIES = 2
# Only the end of a check's output reaches the report.
OUTPUT_BYTES = 1 << 20
DENIED, DELIVERED = "denied", "delivered"
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


@dataclass(frozen=True)
class Run:
    blocks: int = 0
    handback: str | None = None


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


def listed_repositories(gate: Any) -> list[Any]:
    names = gate.get("repositories") if isinstance(gate, dict) else None
    return names if isinstance(names, list) else []


def trusted_repositories(session: Path, environ: Mapping[str, str]) -> set[Path]:
    """The session's repository and the ones its configuration lists in `repositories`."""
    try:
        config = json.loads((session / CONFIG).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        config = {}
    try:
        env = json.loads(environ.get("HARNESS_GATE_COMMANDS", "").strip() or "{}")
    except (ValueError, RecursionError):
        env = {}
    file = config.get("gate") if isinstance(config, dict) else None
    names = listed_repositories(file) + listed_repositories(env)
    trusted = {session}
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        path = (session / Path(name).expanduser()).resolve()
        trusted.add(toplevel(path) or path)
    return trusted


def repository_gate(repo: Path, session: Path, environ: Mapping[str, str]) -> Gate | None:
    if repo == session:
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


def kill(process: subprocess.Popen[bytes]) -> None:
    try:
        if sys.platform == "win32":
            process.kill()
            return
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        return


def read_output(handle: Any) -> str:
    size = handle.seek(0, os.SEEK_END)
    handle.seek(max(0, size - OUTPUT_BYTES))
    return bytes(handle.read()).decode("utf-8", errors="replace")


def run_check(command: str, repo: Path, timeout: float) -> dict[str, Any]:
    started = time.monotonic()
    entry: dict[str, Any] = {"cmd": command, "repo": str(repo), "exit": None}
    # A file, not a pipe: a background child that keeps stdout open cannot hold the gate up.
    with tempfile.TemporaryFile() as output:
        try:
            # The command is the repository's own configuration, run as the user would type it.
            process = subprocess.Popen(  # noqa: S602
                command,
                shell=True,
                cwd=repo,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            return {**entry, "seconds": 0.0, "output_tail": f"Could not start: {error}"}
        late = ""
        try:
            entry["exit"] = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            late = f"\nDid not finish within {round(timeout, 1):g} s."
        # Also ends what the check left running in the background.
        kill(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            late += "\nCould not be stopped."
        text = read_output(output) + late
    seconds = round(time.monotonic() - started, 1)
    return {**entry, "seconds": seconds, "output_tail": tail(text)}


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


def is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def read_state(path: Path) -> dict[str, Run]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    runs = {}
    for key, value in state.items():
        if is_count(value):
            runs[str(key)] = Run(value)
            continue
        if not isinstance(value, dict) or not is_count(value.get("blocks")):
            continue
        handback = value.get("handback")
        runs[str(key)] = Run(value["blocks"], handback if handback in (DENIED, DELIVERED) else None)
    return runs


def write_state(path: Path, state: dict[str, Run]) -> None:
    write_json(path, {key: asdict(run) for key, run in state.items()})


def checked_at_handback(directory: Path, agent_id: str) -> bool:
    """The handback path already ran the gate on this run's report; the run is over."""
    path = directory / STATE
    if not path.is_file():
        return False
    with locked(directory):
        state = read_state(path)
        if state.get(agent_id, Run()).handback != DELIVERED:
            return False
        del state[agent_id]
        write_state(path, state)
    return True


def handed_back_report(event: dict[str, Any]) -> str:
    tool_input = event.get("tool_input")
    message = tool_input.get("message") if isinstance(tool_input, dict) else None
    return message if isinstance(message, str) else ""


def skipped_note(skipped: dict[Path, list[str]]) -> str:
    where = "; ".join(f"{', '.join(files)} in {repo}" for repo, files in skipped.items())
    return (
        f"The quality gate did not check {where}: it runs only the checks of the session's "
        "repository and of the repositories listed in its gate.repositories."
    )


def block_reason(
    commands: list[dict[str, Any]], blocks: int, limit: int, at_handback: bool = False
) -> str:
    parts = [
        f"The quality gate failed (block {blocks} of at most {limit}), so you cannot finish yet. "
        "Fix the cause, run the check again and show its output in your report; never weaken "
        "or skip the check. When the failure is not caused by your change and fixing it is "
        "outside the approved scope, say so in your report instead."
        + (" Then call SubagentHandback again with the whole report." if at_handback else "")
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


def deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def gate(event: dict[str, Any], environ: Mapping[str, str], stdout: TextIO) -> None:
    if not is_implementer(event.get("agent_type")):
        return
    at_handback = event.get("hook_event_name") == "PreToolUse"
    if at_handback and event.get("tool_name") != HANDBACK:
        return
    entries = read_transcript(agent_transcript(event))
    if is_retro(entries):
        return
    report = handed_back_report(event) if at_handback else final_report(entries, event)
    key = ticket_key(event, entries, environ, report)
    directory = harness_home(environ) / "tickets" / key
    agent_id = str(event.get("agent_id") or "")
    if not at_handback and checked_at_handback(directory, agent_id):
        return
    cwd = Path(str(event.get("cwd") or "."))
    root = toplevel(cwd.resolve())
    changed = changed_files(cwd, root, report)
    project = Path(environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".").resolve()
    session = toplevel(project) or project
    trusted = trusted_repositories(session, environ)
    repos = list(dict.fromkeys(repo for repo in (root, *changed) if repo is not None))
    gates = {
        repo: found
        for repo in repos
        if repo in trusted and (found := repository_gate(repo, session, environ))
    }
    if not gates:
        return
    skipped = {repo: files for repo, files in changed.items() if repo not in trusted}
    problem = unwritable(directory)
    if problem:
        message = f"agent-harness did not run the quality gate, {problem}. Check HARNESS_HOME."
        json.dump({"systemMessage": message}, stdout)
        return
    commands = run_gates(gates, changed)
    passed = all(command["exit"] == 0 for command in commands)
    limit = min(found.max_blocks for found in gates.values())
    if at_handback:
        limit = min(limit, HANDBACK_RETRIES)
    with locked(directory):
        state = read_state(directory / STATE)
        run = state.pop(agent_id, Run())
        used = run.blocks
        if not at_handback and run.handback is None and event.get("stop_hook_active") is False:
            used = 0
        blocks = used + 1 if not passed and used < limit else used
        if at_handback:
            state[agent_id] = Run(blocks, DENIED if blocks > used else DELIVERED)
        if not at_handback and not passed:
            state[agent_id] = Run(blocks, run.handback)
        write_state(directory / STATE, state)
        result = {"ticket": key, "ran_at": now(), "repo": str(repos[0]), "passed": passed}
        result |= {"blocks": blocks, "commands": commands}
        if skipped:
            result["skipped"] = [{"repo": str(r), "files": f} for r, f in skipped.items()]
        write_json(directory / "gates.json", result)
    note = skipped_note(skipped) if skipped else ""
    if blocks > used:
        reason = block_reason(commands, blocks, limit, at_handback)
        reason += f"\n\n{note}" if note else ""
        json.dump(deny(reason) if at_handback else {"decision": "block", "reason": reason}, stdout)
        return
    if passed:
        if note:
            json.dump({"systemMessage": note}, stdout)
        return
    failing = ", ".join(command["cmd"] for command in commands if command["exit"] != 0)
    message = (
        f"The quality gate still fails and the implementer finished anyway: {failing}. "
        f"See {directory / 'gates.json'}."
    )
    json.dump({"systemMessage": " ".join(filter(None, [message, note]))}, stdout)


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
