"""The artifacts of each ticket under `$HARNESS_HOME/tickets/<KEY>/`: reads and small writes."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

Json = dict[str, Any]

TICKET = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BLOCK = re.compile(
    r"^[ \t]*```json[ \t]+harness-(?P<kind>[a-z][a-z0-9-]*)[ \t]*\r?\n"
    r"(?P<body>.*?)^[ \t]*```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
RESERVED_KINDS = {"gates", "escaped", "outcome"}
DISPOSITIONS = ("accepted", "rejected", "deferred")
SOURCES = ("sentry", "incident-scout", "manual")


def harness_home() -> Path:
    custom = os.environ.get("HARNESS_HOME", "").strip()
    return Path(custom).expanduser() if custom else Path.home() / ".claude" / "agent-harness"


def ticket_dir(key: str, home: Path | None = None) -> Path:
    if not SAFE_KEY.fullmatch(key) or ".." in key:
        raise ValueError(f"Not a ticket key: {key!r}")
    return (home or harness_home()) / "tickets" / key


def ticket_dirs(home: Path | None = None) -> list[Path]:
    root = (home or harness_home()) / "tickets"
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def first_key(text: str) -> str | None:
    match = TICKET.search(text)
    return match.group() if match else None


def read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON: {error}") from error


def read_object(path: Path) -> Json | None:
    data = read_json(path)
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"{path} must hold a JSON object")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".tmp")
    partial.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    partial.replace(path)


def report_blocks(directory: Path, kind: str) -> list[Json]:
    """Every `json harness-<kind>` block in the agents' reports in the ticket directory, parsed
    like `hooks/save_artifacts.py`: the reserved kinds are the plugin's own files."""
    blocks: list[Json] = []
    if kind in RESERVED_KINDS:
        return blocks
    for report in sorted(directory.glob("*.md")):
        for match in BLOCK.finditer(report.read_text(encoding="utf-8")):
            if match["kind"] != kind:
                continue
            try:
                data = json.loads(match["body"])
            except (ValueError, RecursionError):
                continue
            if isinstance(data, dict):
                blocks.append(data)
    return blocks


def set_disposition(
    directory: Path, ids: Sequence[str], disposition: str, note: str | None = None
) -> list[Json]:
    if disposition not in DISPOSITIONS:
        raise ValueError(f"Disposition must be one of {', '.join(DISPOSITIONS)}")
    path = directory / "review.json"
    review = read_object(path)
    if review is None:
        raise ValueError(f"No review.json in {directory}")
    findings = review.get("findings")
    by_id = {
        str(finding.get("id")): finding
        for finding in (findings if isinstance(findings, list) else [])
        if isinstance(finding, dict)
    }
    unknown = [finding_id for finding_id in ids if finding_id not in by_id]
    if unknown:
        known = ", ".join(by_id) or "none"
        raise ValueError(f"No finding {', '.join(unknown)} in {path} (it has: {known})")
    for finding_id in ids:
        by_id[finding_id]["disposition"] = disposition
        if note is not None:
            by_id[finding_id]["note"] = note
    write_json(path, review)
    return [by_id[finding_id] for finding_id in ids]


def append_escaped(directory: Path, entry: Json) -> bool:
    """Append an escaped defect; False when the same commit and reference are already there."""
    path = directory / "escaped.json"
    entries = read_json(path)
    entries = [] if entries is None else entries
    if not isinstance(entries, list):
        raise ValueError(f"{path} must hold a JSON list")
    for known in entries:
        if not isinstance(known, dict):
            continue
        if known.get("commit") == entry["commit"] and known.get("ref") == entry["ref"]:
            return False
    write_json(path, [*entries, entry])
    return True


@dataclass(frozen=True)
class IncidentCase:
    ref: str
    summary: str


def incident_case(path: Path) -> IncidentCase:
    """The id and the reviewed cause of an incident-scout case (`scout export-case`, YAML).

    A case carries no commit, release or deploy reference, so the commit is always given
    separately. TODO(lead): map one here once `scout export-case` emits it.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"Cannot read the case {path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("pack"), dict):
        raise ValueError(f"{path} is not an incident-scout case")
    summary = str(data.get("note") or data["pack"].get("summary") or "")
    return IncidentCase(ref=str(data.get("id") or path.stem), summary=one_line(summary))


def one_line(text: str) -> str:
    return " ".join(text.split())
