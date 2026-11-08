#!/usr/bin/env python3
"""SubagentStop hook: keep the report and the structured blocks of each run per ticket.

For the analyst, the estimator, the implementer and the reviewer it writes, under
`$HARNESS_HOME/tickets/<KEY>/` (default `~/.claude/agent-harness/tickets/<KEY>/`), the final
report as `<agent>.<NN>.md`, one line per run in `index.jsonl`, and every fenced
`json harness-<kind>` block as `<kind>.json`, moving the previous one to `<kind>.<NN>.json`.
The key is HARNESS_TICKET, else the first ticket key in the task prompt, else
`adhoc-<YYYYMMDD>-<short agent id>`. A stop that a hook blocked does not end the run, so the
next stop of that agent replaces its report and index line. The retro, the evals and the
metrics read these files instead of the conversation. Standard library only, and it never
blocks the session.
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


def task_prompt(entries: list[dict[str, Any]]) -> str:
    for entry in entries:
        if entry.get("type") != "user" or entry.get("isMeta"):
            continue
        text = text_of(entry.get("message") or {})
        if text.strip():
            return text
    return ""


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
    event: dict[str, Any], entries: list[dict[str, Any]], environ: Mapping[str, str]
) -> str:
    override = environ.get("HARNESS_TICKET", "").strip()
    if SAFE_KEY.fullmatch(override):
        return override
    found = TICKET.search(task_prompt(entries))
    if found:
        return found.group(0)
    short = re.sub(r"[^A-Za-z0-9]", "", str(event.get("agent_id") or ""))[:8] or "unknown"
    return f"adhoc-{run_started(entries):%Y%m%d}-{short}"


def final_report(entries: list[dict[str, Any]], event: dict[str, Any]) -> str:
    report, report_id, handed_back = "", None, False
    for entry in entries:
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


def write_block(directory: Path, kind: str, value: dict[str, Any]) -> None:
    latest = directory / f"{kind}.json"
    if latest.exists():
        latest.replace(directory / f"{kind}.{next_number(directory, kind, '.json'):02d}.json")
    write_json(latest, value)


def blocked_run(lines: list[str], agent: str, agent_id: Any) -> tuple[int, str] | None:
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
            return number, report
        return None
    return None


def read_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def save(directory: Path, agent: str, event: dict[str, Any], report: str) -> None:
    index = directory / "index.jsonl"
    with locked(directory):
        lines = read_lines(index)
        previous = None
        if event.get("stop_hook_active") is True:
            previous = blocked_run(lines, agent, event.get("agent_id"))
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
            write_block(directory, block.kind, block.value)
            if block.kind not in kinds:
                kinds.append(block.kind)
        line: dict[str, Any] = {
            "agent": agent,
            "agent_id": event.get("agent_id"),
            "ended": now(),
            "report": name,
            "blocks": kinds,
        }
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


def main(stdin: TextIO = sys.stdin, environ: Mapping[str, str] = os.environ) -> int:
    try:
        event = json.load(stdin)
        if not isinstance(event, dict):
            return 0
        agent = agent_name(event.get("agent_type"))
        if agent is None:
            return 0
        entries = read_transcript(event.get("agent_transcript_path"))
        report = final_report(entries, event)
        if not report.strip():
            return 0
        directory = harness_home(environ) / "tickets" / ticket_key(event, entries, environ)
        save(directory, agent, event, report)
    except Exception as error:
        print(f"save_artifacts: {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
