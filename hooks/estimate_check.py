#!/usr/bin/env python3
"""SubagentStop hook: check the estimator's `harness-estimate` block and compute its total.

The estimator ends an estimate with a fenced block whose info string is
`json harness-estimate`. Code, not the model, turns it into the number: the reference median,
or the sum of the parts, plus the adjustments, rounded to half an hour and never below half an
hour. A block that is missing or breaks a rule (an adjustment without a citation, a part
without evidence, a total that differs from the computed one) keeps the estimator running
with the errors as its next instruction, at most twice per run; after that the hook lets it
stop and warns the person. A report handed back through SubagentHandback has already been
delivered when the subagent stops, so for it the hook only warns. Reports without an
"Estimate:" line (a retro, a recorded correction) are not checked.

`src/agent_harness/estimate_check.py` is a byte-identical copy that the backtest imports; a
test keeps the two equal. Standard library only, and bad input never blocks the session.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

AGENT = "estimator"
MAX_BLOCKS = 2
MODES = ("bottom-up", "reference")
KINDS = ("implementation", "verification", "release")
TICKET = re.compile(r"[A-Z][A-Z0-9]+-\d+")
BLOCK = re.compile(
    r"^(`{3,})[ \t]*json[ \t]+harness-estimate[ \t]*\n(.*?)^\1[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
ESTIMATE_LINE = re.compile(r"^[\s>*_#-]*estimate\b[*_ \t]*:", re.IGNORECASE | re.MULTILINE)
CITATION_SHAPES = (
    "a ticket key (SHOP-412), a repository-relative path (src/shop/export.py) or measured:<ref>"
)
UNCITED = "has no citation"
NO_EVIDENCE = "has no evidence"
MISSING = (
    "The report has no ```json harness-estimate block. End the report with one, as the Answer "
    "section describes."
)


def round_half(hours: float) -> float:
    """The nearest half hour, halves rounded up, never below half an hour."""
    return max(0.5, math.floor(hours * 2 + 0.5 + 1e-9) / 2)


def number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def is_repository_path(text: str) -> bool:
    if not text or any(character.isspace() for character in text) or "://" in text:
        return False
    if text.startswith(("/", "~", "\\")) or re.match(r"[A-Za-z]:", text):
        return False
    if ".." in text.split("/"):
        return False
    return "/" in text or re.search(r"\.[A-Za-z0-9]+(:\d+)?$", text) is not None


def is_citation(value: object) -> bool:
    """A ticket key, a repository-relative path or `measured:<ref>`."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text.startswith("measured:"):
        return bool(text.removeprefix("measured:").strip())
    return TICKET.fullmatch(text) is not None or is_repository_path(text)


def _label(kind: str, position: int, item: Mapping[str, Any], field: str) -> str:
    name = item.get(field)
    return (
        f'{kind}[{position}] ("{name}")'
        if isinstance(name, str) and name
        else f"{kind}[{position}]"
    )


def _part_errors(position: int, part: object) -> list[str]:
    if not isinstance(part, dict):
        return [f"parts[{position}] must be an object with name, kind, hours and evidence."]
    label = _label("parts", position, part, "name")
    errors = []
    hours = number(part.get("hours"))
    if hours is None or hours < 0:
        errors.append(f"{label}: hours must be a number of 0 or more.")
    if part.get("kind") not in KINDS:
        errors.append(f"{label}: kind must be one of {', '.join(KINDS)}.")
    evidence = part.get("evidence")
    if not isinstance(evidence, list) or not any(is_citation(item) for item in evidence):
        errors.append(f"{label} {NO_EVIDENCE}: cite {CITATION_SHAPES}.")
    return errors


def _adjustment_errors(position: int, adjustment: object) -> list[str]:
    if not isinstance(adjustment, dict):
        return [f"adjustments[{position}] must be an object with hours, reason and citation."]
    label = _label("adjustments", position, adjustment, "reason")
    errors = []
    if number(adjustment.get("hours")) is None:
        errors.append(f"{label}: hours must be a number, negative to go down.")
    citation = adjustment.get("citation")
    if citation is None or (isinstance(citation, str) and not citation.strip()):
        return [*errors, f"{label} {UNCITED}: cite {CITATION_SHAPES}, or drop the adjustment."]
    if not is_citation(citation):
        errors.append(f"{label}: citation {json.dumps(citation)} is not {CITATION_SHAPES}.")
    return errors


def _items(block: Mapping[str, Any], field: str) -> tuple[list[Any], list[str]]:
    value = block.get(field)
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], [f"{field} must be a list."]
    return value, []


def validate_and_total(block: object) -> tuple[float | None, list[str]]:
    """The estimate code computes from a `harness-estimate` block, or the rules it breaks.

    Reference mode: reference_median + the adjustments. Bottom-up: the parts + the
    adjustments. The model's own `total` is ignored. Rounded with `round_half`.
    """
    if not isinstance(block, dict):
        return None, ["The harness-estimate block must be one JSON object."]
    parts, errors = _items(block, "parts")
    adjustments, more = _items(block, "adjustments")
    errors += more
    for position, part in enumerate(parts):
        errors += _part_errors(position, part)
    for position, adjustment in enumerate(adjustments):
        errors += _adjustment_errors(position, adjustment)
    mode = block.get("mode")
    if mode not in MODES:
        return None, ['mode must be "bottom-up" or "reference".', *errors]
    base = number(block.get("reference_median"))
    if mode == "reference" and (base is None or base < 0):
        errors.append(
            "reference mode needs reference_median: the median hours of the reference class "
            "you anchored on."
        )
    if mode == "bottom-up" and not parts:
        errors.append("bottom-up mode needs at least one part.")
    if errors:
        return None, errors
    if mode == "bottom-up":
        base = sum(float(part["hours"]) for part in parts)
    shift = sum(float(adjustment["hours"]) for adjustment in adjustments)
    return round_half((base or 0.0) + shift), []


def find_block(text: str) -> tuple[object, list[str]]:
    """The JSON body of the last `harness-estimate` block in a report."""
    found = list(BLOCK.finditer(text))
    if not found:
        return None, [MISSING]
    try:
        return json.loads(found[-1].group(2)), []
    except ValueError as error:
        return None, [f"The harness-estimate block is not valid JSON: {error}."]


def check_report(text: str) -> tuple[float | None, list[str]]:
    """Errors of an estimate report, including a stated total that differs from the code's."""
    block, errors = find_block(text)
    if errors:
        return None, errors
    total, errors = validate_and_total(block)
    if errors or total is None or not isinstance(block, dict):
        return None, errors
    stated = number(block.get("total"))
    if stated != total:
        return total, [
            f"total is {block.get('total')!r}, but the block computes to {total:g} h. Set total "
            f"to {total:g} and give {total:g} h as the Estimate."
        ]
    return total, []


def is_estimate(text: str) -> bool:
    return "harness-estimate" in text or ESTIMATE_LINE.search(text) is not None


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def transcript_reports(path: object) -> tuple[str | None, str]:
    """The report handed back through SubagentHandback, if any, and the last assistant text."""
    if not isinstance(path, str) or not path or not Path(path).is_file():
        return None, ""
    handed_back: str | None = None
    last = ""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(message, dict) or entry.get("type") != "assistant":
            continue
        content = message.get("content")
        last = _text(content) or last
        for item in content if isinstance(content, list) else []:
            if isinstance(item, dict) and item.get("name") == "SubagentHandback":
                handed_back = str((item.get("input") or {}).get("message") or "")
    return handed_back, last


def state_path(event: Mapping[str, Any], environ: Mapping[str, str]) -> Path:
    home = environ.get("HARNESS_HOME") or str(Path.home() / ".claude" / "agent-harness")
    run = f"{event.get('session_id') or 'session'}-{event.get('agent_id') or 'agent'}"
    return (
        Path(home).expanduser() / "estimate-check" / f"{re.sub(r'[^A-Za-z0-9_-]', '-', run)}.json"
    )


def _blocks_so_far(path: Path) -> int:
    try:
        blocks = json.loads(path.read_text(encoding="utf-8")).get("blocks")
    except (OSError, ValueError, AttributeError):
        return 0
    return blocks if isinstance(blocks, int) else 0


def _record(path: Path, blocks: int, errors: list[str], handed_back: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"blocks": blocks, "errors": errors, "handed_back": handed_back}
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")


def decision(event: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any] | None:
    agent_type = event.get("agent_type")
    if not isinstance(agent_type, str) or agent_type.split(":")[-1] != AGENT:
        return None
    handed_back, last = transcript_reports(event.get("agent_transcript_path"))
    report = handed_back
    if report is None:
        message = event.get("last_assistant_message")
        report = message if isinstance(message, str) else last
    path = state_path(event, environ)
    if not is_estimate(report):
        return None
    _, errors = check_report(report)
    if not errors:
        path.unlink(missing_ok=True)
        return None
    blocks = _blocks_so_far(path)
    if handed_back is None and blocks < MAX_BLOCKS:
        _record(path, blocks + 1, errors, handed_back=False)
        return {
            "decision": "block",
            "reason": "The harness-estimate block does not pass the check:\n"
            + "\n".join(f"- {error}" for error in errors)
            + "\nReply with the whole report again, ending with the corrected block: your last "
            "message is what the team lead receives.",
        }
    _record(path, blocks, errors, handed_back=handed_back is not None)
    why = "it was handed back already" if handed_back is not None else f"after {blocks} retries"
    return {
        "systemMessage": f"agent-harness: the estimator's harness-estimate block is invalid "
        f"({why}): " + " ".join(errors)
    }


def main(
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    environ: Mapping[str, str] | None = None,
) -> int:
    try:
        verdict = decision(json.load(stdin), os.environ if environ is None else environ)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0
    if verdict is not None:
        json.dump(verdict, stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
