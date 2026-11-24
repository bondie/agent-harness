#!/usr/bin/env python3
"""SubagentStop hook: keep the report and the structured blocks of each run per ticket.

For the analyst, the estimator, the implementer and the reviewer it writes, under
`$HARNESS_HOME/tickets/<KEY>/` (default `~/.claude/agent-harness/tickets/<KEY>/`), the final
report as `<agent>.<NN>.md`, one line per run in `index.jsonl`, and every fenced
`json harness-<kind>` block as `<kind>.json`, moving the previous one to `<kind>.<NN>.json`.
The key is HARNESS_TICKET, else the `ticket` of the report's own structured block, else the
first ticket key in the task prompt whose prefix is not in NOT_TICKET_PREFIXES (`UTF-8`,
`SHA-256`), else `adhoc-<YYYYMMDD>-<short agent id>`. A stop that a hook blocked does not end
the run, so the next stop of that agent replaces its report and index line. A run starts at
the task or at a SendMessage from the coordinator, and only its own entries make the report.
It also runs as PostToolUse on SubagentHandback and saves the delivered report from the tool
input, because SubagentStop can read a transcript without the last message; a later stop of
that run keeps the handed-back report, which `handbacks.json` in the harness home locates.
A retro run (task or latest message starting "Retro for") is not saved, so it cannot
overwrite the ticket's artifacts. The retro, the evals and the metrics read these files
instead of the conversation. Standard library
only; it never blocks the session and tells the person when it cannot write the files.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, TextIO

from log_agent_runs import TICKET, parse_time, text_of

if sys.platform != "win32":
    import fcntl

AGENTS = ("jira-analyst", "estimator", "implementer", "reviewer-qa")
HANDBACK = "SubagentHandback"
RESERVED_KINDS = {"gates", "escaped", "outcome"}
BLOCK = re.compile(
    r"^[ \t]*```json[ \t]+harness-([a-z][a-z0-9-]*)[ \t]*\r?\n(.*?)^[ \t]*```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
# Standards and encodings shaped like ticket keys; src/agent_harness/artifacts.py keeps the same.
NOT_TICKET_PREFIXES = (
    "UTF",
    "ISO",
    "PSR",
    "SHA",
    "RFC",
    "CVE",
    "HTTP",
    "TLS",
    "X",
    "ECMA",
    "IEEE",
    "UTC",
    "GMT",
)
# "Retro for <KEY>", optionally after the "<KEY>: " every ticket task starts with.
RETRO = re.compile(r"\s*(?:[A-Za-z][A-Za-z0-9]+-\d+\s*:\s*)?retro for\b", re.IGNORECASE)
# How Claude Code hands a SendMessage to a running or finished subagent.
RESUMED = "The coordinator sent a message while you were working:"
INTERRUPTED = "[Request interrupted by user"
HANDBACKS = "handbacks.json"
KEEP_SECONDS = 2 * 24 * 3600


class Block(NamedTuple):
    kind: str
    value: dict[str, Any] | None
    error: str


def harness_home(environ: Mapping[str, str]) -> Path:
    custom = environ.get("HARNESS_HOME", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".claude" / "agent-harness"


def agent_name(agent_type: Any) -> str | None:
    if not isinstance(agent_type, str) or agent_type.split(":")[-1] not in AGENTS:
        return None
    return agent_type.split(":")[-1]


def read_transcript(path: Any) -> list[dict[str, Any]]:
    if not isinstance(path, str) or not path or not Path(path).is_file():
        return []
    entries = []
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def agent_transcript(event: dict[str, Any]) -> str | None:
    """The subagent's own transcript; tool events carry only the session's, next to it."""
    path = event.get("agent_transcript_path")
    if isinstance(path, str) and path:
        return path
    session, agent_id = event.get("transcript_path"), event.get("agent_id")
    if not isinstance(session, str) or not session or not isinstance(agent_id, str) or not agent_id:
        return None
    name = f"agent-{agent_id}.jsonl"
    if Path(session).name == name:
        return session
    return str(Path(session).with_suffix("") / "subagents" / name)


def task_prompt(entries: list[dict[str, Any]]) -> str:
    for entry in entries:
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        text = text_of(entry.get("message") or {})
        if text.strip():
            return text
    return ""


def is_prompt(entry: dict[str, Any]) -> bool:
    """A task or a message from the coordinator, not a reminder, an interrupt or a tool result."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if entry.get("type") != "user" or not isinstance(content, (str, list)):
        return False
    if entry.get("isCompactSummary") is True:
        return False
    items = content if isinstance(content, list) else []
    if any(isinstance(item, dict) and item.get("type") == "tool_result" for item in items):
        return False
    text = text_of(message).strip()
    if not text or text.startswith(INTERRUPTED):
        return False
    origin = entry.get("origin")
    sender = origin.get("kind") if isinstance(origin, dict) else None
    return entry.get("isMeta") is not True or sender == "coordinator" or text.startswith(RESUMED)


def current_run(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The entries after the latest prompt, and that prompt: a resumed agent starts a new run."""
    for number in range(len(entries) - 1, -1, -1):
        if is_prompt(entries[number]):
            return entries[number + 1 :], entries[number]
    return entries, None


def is_retro(entries: list[dict[str, Any]]) -> bool:
    prompt = current_run(entries)[1]
    latest = text_of(prompt.get("message") or {}).strip() if prompt else ""
    tasks = (task_prompt(entries), latest.removeprefix(RESUMED))
    return any(RETRO.match(task) is not None for task in tasks)


def prompt_time(entries: list[dict[str, Any]]) -> str | None:
    """When the prompt that started the current run was sent; it identifies the run."""
    prompt = current_run(entries)[1]
    stamp = prompt.get("timestamp") if prompt else None
    return stamp if isinstance(stamp, str) else None


def read_handbacks(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def handback_ticket(environ: Mapping[str, str], agent_id: Any, prompt_at: str | None) -> str | None:
    """The ticket the run's handback went to, so that a stop of that run finds it."""
    if prompt_at is None or not agent_id:
        return None
    saved = read_handbacks(harness_home(environ) / HANDBACKS).get(str(agent_id))
    if not isinstance(saved, dict) or saved.get("prompt_at") != prompt_at:
        return None
    ticket = saved.get("ticket")
    return ticket if isinstance(ticket, str) and SAFE_KEY.fullmatch(ticket) else None


def remember_handback(
    environ: Mapping[str, str], agent_id: Any, prompt_at: str | None, ticket: str
) -> None:
    if prompt_at is None or not agent_id:
        return
    home = harness_home(environ)
    moment = datetime.now(UTC).timestamp()
    with locked(home):
        state = {
            key: value
            for key, value in read_handbacks(home / HANDBACKS).items()
            if isinstance(value, dict)
            and isinstance(value.get("at"), (int, float))
            and moment - value["at"] < KEEP_SECONDS
        }
        state[str(agent_id)] = {"prompt_at": prompt_at, "ticket": ticket, "at": moment}
        partial = home / f"{HANDBACKS}.tmp"
        write_json(partial, state)
        partial.replace(home / HANDBACKS)


def run_ticket(
    event: dict[str, Any],
    entries: list[dict[str, Any]],
    environ: Mapping[str, str],
    report: str = "",
) -> str:
    """The run's ticket: at a stop, where the run's handback went, else `ticket_key`."""
    if event.get("hook_event_name") == "SubagentStop":
        ticket = handback_ticket(environ, event.get("agent_id"), prompt_time(entries))
        if ticket:
            return ticket
    return ticket_key(event, entries, environ, report)


def handed_back_report(event: dict[str, Any]) -> str:
    tool_input = event.get("tool_input")
    message = tool_input.get("message") if isinstance(tool_input, dict) else None
    return message if isinstance(message, str) else ""


def is_ticket(text: str) -> bool:
    return TICKET.fullmatch(text) is not None and text.split("-")[0] not in NOT_TICKET_PREFIXES


def run_started(entries: list[dict[str, Any]]) -> datetime:
    for entry in entries:
        stamp = entry.get("timestamp")
        if not isinstance(stamp, str):
            continue
        try:
            return parse_time(stamp)
        except ValueError:
            continue
    return datetime.now(UTC)


def ticket_key(
    event: dict[str, Any],
    entries: list[dict[str, Any]],
    environ: Mapping[str, str],
    report: str = "",
) -> str:
    override = environ.get("HARNESS_TICKET", "").strip()
    if SAFE_KEY.fullmatch(override):
        return override
    for block in structured_blocks(report):
        stated = (block.value or {}).get("ticket")
        if isinstance(stated, str) and is_ticket(stated.strip()):
            return stated.strip()
    for found in TICKET.finditer(task_prompt(entries)):
        if is_ticket(found.group(0)):
            return found.group(0)
    short = re.sub(r"[^A-Za-z0-9]", "", str(event.get("agent_id") or ""))[:8] or "unknown"
    return f"adhoc-{run_started(entries):%Y%m%d}-{short}"


def final_report(entries: list[dict[str, Any]], event: dict[str, Any]) -> str:
    report, report_id, handed_back = "", None, False
    for entry in current_run(entries)[0]:
        message = entry.get("message")
        if entry.get("type") != "assistant" or not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and item.get("name") == HANDBACK:
                handback = (item.get("input") or {}).get("message")
                if isinstance(handback, str) and handback.strip():
                    report, report_id, handed_back = handback, None, True
                continue
            text = item.get("text")
            if item.get("type") != "text" or not isinstance(text, str) or not text.strip():
                continue
            if not handed_back and report_id is not None and message.get("id") == report_id:
                report += "\n\n" + text
                continue
            report, report_id, handed_back = text, message.get("id"), False
    last = event.get("last_assistant_message")
    if handed_back or not isinstance(last, str) or not last.strip():
        return report
    return last


def structured_blocks(report: str) -> list[Block]:
    blocks = []
    for match in BLOCK.finditer(report):
        kind = match.group(1)
        if kind in RESERVED_KINDS:
            blocks.append(Block(kind, None, f"harness-{kind} is reserved for the plugin's files"))
            continue
        try:
            value = json.loads(match.group(2))
        except (ValueError, RecursionError) as error:
            blocks.append(Block(kind, None, f"invalid JSON: {error}"))
            continue
        if not isinstance(value, dict):
            blocks.append(Block(kind, None, "not a JSON object"))
            continue
        blocks.append(Block(kind, value, ""))
    return blocks


def unwritable(directory: Path) -> str | None:
    """Why nothing can be recorded in `directory`, or None when it can."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / ".lock").open("a"):
            pass
    except OSError as error:
        return f"{directory} is not writable ({error.strerror or error})"
    return None


@contextmanager
def locked(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as handle:
        if sys.platform != "win32":
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def next_number(directory: Path, prefix: str, suffix: str) -> int:
    pattern = re.compile(re.escape(prefix) + r"\.(\d+)" + re.escape(suffix))
    numbers = [
        int(found.group(1))
        for path in directory.iterdir()
        if (found := pattern.fullmatch(path.name))
    ]
    return max(numbers, default=0) + 1


def write_block(directory: Path, kind: str, value: dict[str, Any], keep: bool = True) -> None:
    latest = directory / f"{kind}.json"
    if keep and latest.exists():
        latest.replace(directory / f"{kind}.{next_number(directory, kind, '.json'):02d}.json")
    write_json(latest, value)


class Run(NamedTuple):
    number: int
    report: str
    line: dict[str, Any]


def last_run(lines: list[str], agent: str, agent_id: Any) -> Run | None:
    """The agent's latest line in the index, when it names a report of that agent."""
    for number in range(len(lines) - 1, -1, -1):
        try:
            line = json.loads(lines[number])
        except ValueError:
            continue
        if not isinstance(line, dict) or line.get("agent") != agent:
            continue
        if line.get("agent_id") != agent_id:
            continue
        report = line.get("report")
        if isinstance(report, str) and re.fullmatch(re.escape(agent) + r"\.\d+\.md", report):
            return Run(number, report, line)
        return None
    return None


def blocked_run(lines: list[str], agent: str, agent_id: Any) -> tuple[int, str] | None:
    run = last_run(lines, agent, agent_id)
    return (run.number, run.report) if run else None


def same_run(lines: list[str], agent: str, agent_id: Any, prompt_at: str | None) -> Run | None:
    """The line saved earlier for the run that `prompt_at` started, if any."""
    run = last_run(lines, agent, agent_id)
    if prompt_at is None or run is None or run.line.get("prompt_at") != prompt_at:
        return None
    return run


def repeats(directory: Path, run: Run | None, report: str) -> bool:
    if run is None:
        return False
    try:
        saved = (directory / run.report).read_text(encoding="utf-8")
    except OSError:
        return False
    return saved.rstrip("\n") == report.rstrip("\n")


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def save(
    directory: Path,
    agent: str,
    event: dict[str, Any],
    report: str,
    prompt_at: str | None = None,
    handback: bool = False,
) -> None:
    index = directory / "index.jsonl"
    agent_id = event.get("agent_id")
    with locked(directory):
        lines = read_lines(index)
        previous = None
        overwrite: set[str] = set()
        run = same_run(lines, agent, agent_id, prompt_at)
        if run is not None:
            if run.line.get("handback") is True and not handback:
                return
            previous = (run.number, run.report)
            if event.get("stop_hook_active") is not True:
                overwrite = {kind for kind in run.line.get("blocks") or [] if isinstance(kind, str)}
        elif event.get("stop_hook_active") is True:
            previous = blocked_run(lines, agent, agent_id)
        stale = previous is None and not handback
        if stale and repeats(directory, last_run(lines, agent, agent_id), report):
            print(
                f"save_artifacts: {agent} {agent_id} repeated its last report; skipped",
                file=sys.stderr,
            )
            return
        name = f"{agent}.{next_number(directory, agent, '.md'):02d}.md"
        if previous:
            name = previous[1]
        (directory / name).write_text(report.rstrip("\n") + "\n", encoding="utf-8")
        kinds: list[str] = []
        errors: dict[str, str] = {}
        for block in structured_blocks(report):
            if block.value is None:
                errors[block.kind] = block.error
                continue
            write_block(directory, block.kind, block.value, keep=block.kind not in overwrite)
            overwrite.discard(block.kind)
            if block.kind not in kinds:
                kinds.append(block.kind)
        line: dict[str, Any] = {
            "agent": agent,
            "agent_id": agent_id,
            "ended": now(),
            "report": name,
            "blocks": kinds,
        }
        if prompt_at is not None:
            line["prompt_at"] = prompt_at
        if handback:
            line["handback"] = True
        if errors:
            line["error"] = errors
        text = json.dumps(line, ensure_ascii=False)
        if previous is None:
            with index.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")
            return
        lines[previous[0]] = text
        partial = index.with_suffix(".jsonl.tmp")
        partial.write_text("\n".join(lines) + "\n", encoding="utf-8")
        partial.replace(index)


def main(
    stdin: TextIO = sys.stdin,
    environ: Mapping[str, str] = os.environ,
    stdout: TextIO = sys.stdout,
) -> int:
    try:
        event = json.load(stdin)
        if not isinstance(event, dict):
            return 0
        agent = agent_name(event.get("agent_type"))
        if agent is None:
            return 0
        handback = event.get("hook_event_name") == "PostToolUse"
        if handback and event.get("tool_name") != HANDBACK:
            return 0
        entries = read_transcript(agent_transcript(event))
        report = handed_back_report(event) if handback else final_report(entries, event)
        if not report.strip() or is_retro(entries):
            return 0
        prompt_at = prompt_time(entries)
        ticket = run_ticket(event, entries, environ, report)
        directory = harness_home(environ) / "tickets" / ticket
        problem = unwritable(directory)
        if problem:
            message = f"agent-harness could not save the {agent} report: {problem}."
            json.dump({"systemMessage": message}, stdout)
            print(f"save_artifacts: {problem}", file=sys.stderr)
            return 0
        save(directory, agent, event, report, prompt_at, handback)
        if handback:
            remember_handback(environ, event.get("agent_id"), prompt_at, ticket)
    except Exception as error:
        print(f"save_artifacts: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
