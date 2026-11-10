"""LLM estimators: the shipped estimator prompt, run offline on Claude Code or Ollama."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import httpx
from ticket_precedent.effort import ReferenceClass, reference_for_ticket
from ticket_precedent.search import Searcher

from .baselines import Estimate
from .dataset import Target
from .estimate_check import KINDS, MODES, UNCITED, is_citation, number, validate_and_total
from .memory import Memory, memory_section

AGENT_FILE = Path(__file__).resolve().parents[2] / "agents" / "estimator.md"
DESCRIPTION_CHARS = 4000
SNIPPET_CHARS = 300
ESTIMATE_WORDS = re.compile(
    r"odhad|estimat|nacen|naceň|pracnost|story ?points?|\bMD\b|man-?days?", re.IGNORECASE
)
NUMBER = re.compile(r"\d+(?:[.,]\d+)?(?:\s*(?:h|hod\w*|hours?|md|mh|min\w*)\b)?", re.IGNORECASE)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "median_hours": {"type": "number", "minimum": 0},
        "p25_hours": {"type": "number", "minimum": 0},
        "p75_hours": {"type": "number", "minimum": 0},
        "cited": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["median_hours", "p25_hours", "p75_hours", "cited", "reasoning"],
    "additionalProperties": False,
}

ESTIMATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ticket": {"type": "string"},
        "mode": {"type": "string", "enum": list(MODES)},
        "reference_median": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "hours": {"type": "number"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "kind", "hours", "evidence"],
                "additionalProperties": False,
            },
        },
        "adjustments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hours": {"type": "number"},
                    "reason": {"type": "string"},
                    "citation": {"type": "string"},
                },
                "required": ["hours", "reason", "citation"],
                "additionalProperties": False,
            },
        },
        "total": {"type": "number"},
    },
    "required": ["ticket", "mode", "reference_median", "parts", "adjustments", "total"],
    "additionalProperties": False,
}

OFFLINE = """
## Offline evaluation

This run is a backtest. Tools are unavailable: the message holds everything you may use, and
the reference class in it was already retrieved with the cut-off of the ticket's creation
time. Answer with the JSON object only: median_hours, p25_hours, p75_hours, cited (the keys
of the tickets you relied on) and reasoning (at most three sentences). There are no memory
files to open: the lessons you may use, if any, are already in this prompt.
"""

OFFLINE_STRUCTURED = """
## Offline evaluation

This run is a backtest. Tools are unavailable: the message holds everything you may use, and
the reference class in it was already retrieved with the cut-off of the ticket's creation
time. The ticket comes without an implementation plan. Answer with the body of the
harness-estimate block only (see Answer): one JSON object, without the report around it.
There are no memory files to open: the lessons you may use, if any, are already in this prompt.
"""

STRUCTURED = "-structured"
WITH_MEMORY = "+memory"
REJECTED = "rejected: "
REJECTED_UNCITED = "rejected, uncited adjustment: "
RETRY = (
    "Your harness-estimate block was rejected by the estimate check: {errors} Answer again with "
    "a corrected block."
)


class LLMError(RuntimeError):
    def __init__(self, message: str, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class Backend(Protocol):
    @property
    def name(self) -> str: ...

    def complete(
        self, system: str, user: str, schema: Mapping[str, Any] = SCHEMA
    ) -> tuple[dict[str, Any], float]: ...


def agent_prompt(path: Path = AGENT_FILE) -> str:
    """The body of a subagent definition, without its YAML front matter."""
    text = path.read_text()
    if not text.startswith("---"):
        return text.strip()
    return text.split("---", 2)[2].strip()


def redact_estimates(text: str) -> tuple[str, bool]:
    """Mask numbers on lines that talk about estimates, so the model cannot copy a human one."""
    changed = False
    lines = []
    for line in text.splitlines():
        if ESTIMATE_WORDS.search(line) and NUMBER.search(line):
            line = NUMBER.sub("[redacted]", line)
            changed = True
        lines.append(line)
    return "\n".join(lines), changed


def ticket_message(
    target: Target, reference: str | None, process_note: str | None
) -> tuple[str, bool]:
    title, title_redacted = redact_estimates(target.title)
    description, redacted = redact_estimates(target.description[:DESCRIPTION_CHARS])
    parts = [
        f"Ticket {target.key} ({target.issue_type or 'unknown type'}), "
        f"created {target.created[:10]}",
        f"Title: {title}",
        f"Description:\n{description or '(empty)'}",
    ]
    if process_note:
        parts.append(f"Process change: {process_note}")
    parts.append(reference or "No reference class is available: estimate from the ticket alone.")
    return "\n\n".join(parts), redacted or title_redacted


def describe_reference(searcher: Searcher, found: ReferenceClass) -> str:
    rows = searcher.store.tickets_by_id(hit.ticket_id for hit in found.hits)
    if not found.hits:
        return "Reference class: no similar ticket was finished before this one."
    summary = found.summary()
    lines = [
        f"Reference class: {summary['tickets']} similar tickets finished before the cut-off, "
        f"median {summary['median_hours']} h, P25-P75 "
        f"{summary['p25_hours']}-{summary['p75_hours']} h. Most similar first:",
    ]
    for position, hit in enumerate(found.hits, start=1):
        row = rows[hit.ticket_id]
        text = " ".join((row["description"] or "").split())[:SNIPPET_CHARS]
        lines.append(
            f"{position}. {row['key']} · {row['issue_type'] or 'ticket'} · created "
            f"{row['created'][:10]} · logged {row['time_spent_seconds'] / 3600:.2f} h\n"
            f"   {row['title']}\n   {text}"
        )
    return "\n".join(lines)


def parse_estimate(payload: dict[str, Any], cost: float, note: str = "") -> Estimate:
    try:
        low, median, high = sorted(
            float(payload[name]) for name in ("p25_hours", "median_hours", "p75_hours")
        )
    except (KeyError, TypeError, ValueError) as error:
        raise LLMError(f"Malformed estimate {payload!r}") from error
    cited = tuple(str(key) for key in payload.get("cited") or ())
    return Estimate(median=median, p25=low, p75=high, cited=cited, cost_usd=cost, note=note)


class RejectedError(LLMError):
    def __init__(self, errors: list[str], payload: dict[str, Any], cost_usd: float) -> None:
        prefix = REJECTED_UNCITED if any(UNCITED in error for error in errors) else REJECTED
        super().__init__(prefix + " ".join(errors), cost_usd)
        self.errors = errors
        self.payload = payload


def parse_structured(
    payload: dict[str, Any], cost: float, note: str = "", retrieved: float | None = None
) -> Estimate:
    """The estimate code computes from a harness-estimate block; an invalid block fails.

    In reference mode the total starts from the `retrieved` median, not the one the model
    wrote: a different starting point counts only through a cited adjustment.
    """
    total, errors = validate_and_total(payload)
    mode = payload.get("mode")
    if total is not None and mode == "reference" and retrieved is not None:
        total, errors = validate_and_total({**payload, "reference_median": retrieved})
    if errors or total is None:
        raise RejectedError(errors, payload, cost)
    return Estimate(
        median=total,
        cited=citations(payload),
        cost_usd=cost,
        note=note,
        block_mode=str(mode),
        block_reference_median=number(payload.get("reference_median")),
    )


def kept_after_check(rejected: RejectedError, cost: float, note: str) -> Estimate:
    """The stated total of a block that failed the check twice, like the hook lets it through."""
    stated = number(rejected.payload.get("total"))
    if stated is None or stated < 0:
        raise LLMError(str(rejected), cost)
    mode = rejected.payload.get("mode")
    return Estimate(
        median=stated,
        cited=citations(rejected.payload),
        cost_usd=cost,
        note=note,
        block_mode=mode if isinstance(mode, str) else None,
        block_reference_median=number(rejected.payload.get("reference_median")),
        retried=True,
        check_errors=tuple(rejected.errors),
    )


def citations(block: Mapping[str, Any]) -> tuple[str, ...]:
    parts = [part for part in _list(block.get("parts")) if isinstance(part, dict)]
    items = [item for part in parts for item in _list(part.get("evidence"))]
    items += [
        adjustment.get("citation")
        for adjustment in _list(block.get("adjustments"))
        if isinstance(adjustment, dict)
    ]
    return tuple(dict.fromkeys(item.strip() for item in items if is_citation(item)))


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


@dataclass
class LLMEstimator:
    """The estimator agent's prompt with a reference class from `context` retrieval.

    `context` is "none" (the ticket alone), "keyword" (BM25 neighbours, like a plain tracker
    search) or "hybrid" (ticket-precedent's BM25 + embeddings, what the agent uses). With
    `since`, the reference class holds only tickets created after a process change.
    `structured` asks for the harness-estimate block and lets code compute the number;
    `memory` adds the lessons learned before the ticket was created to the prompt.
    """

    backend: Backend
    searcher: Searcher
    context: str = "hybrid"
    k: int = 10
    since: str | None = None
    sources: frozenset[str] | None = None
    process_note: str | None = None
    structured: bool = False
    memory: Memory | None = None
    system: str = ""

    def __post_init__(self) -> None:
        if not self.system:
            offline = OFFLINE_STRUCTURED if self.structured else OFFLINE
            self.system = agent_prompt() + "\n" + offline

    @property
    def name(self) -> str:
        recent = "-recent" if self.since else ""
        structured = STRUCTURED if self.structured else ""
        memory = WITH_MEMORY if self.memory is not None else ""
        return f"llm-{self.context}{recent}{structured}{memory}@{self.backend.name}"

    def estimate(self, target: Target) -> Estimate | None:
        reference = None
        median = None
        if self.context != "none":
            mode = "bm25" if self.context == "keyword" else "hybrid"
            row = self.searcher.store.tickets_by_id([target.ticket_id])[target.ticket_id]
            found = reference_for_ticket(
                self.searcher, row, self.k, self.since, mode=mode, sources=self.sources
            )
            reference = describe_reference(self.searcher, found)
            median = found.summary()["median_hours"]
        message, redacted = ticket_message(target, reference, self.process_note)
        note = "redacted" if redacted else ""
        system = self.system
        lessons = None
        if self.memory is not None:
            usable = self.memory.usable_on(target.created)
            system += memory_section(usable)
            lessons = len(usable)
        estimate = (
            self._structured(system, message, note, median)
            if self.structured
            else parse_estimate(*self.backend.complete(system, message, SCHEMA), note)
        )
        return replace(
            estimate,
            reference_median=median,
            lessons=lessons,
            lessons_dated=None if self.memory is None else self.memory.dated,
            lessons_undated=None if self.memory is None else self.memory.undated,
        )

    def _structured(
        self, system: str, message: str, note: str, retrieved: float | None
    ) -> Estimate:
        """One retry with the check's errors; after that the stated total is kept."""
        payload, cost = self.backend.complete(system, message, ESTIMATE_SCHEMA)
        try:
            return parse_structured(payload, cost, note, retrieved)
        except RejectedError as rejected:
            retry = RETRY.format(errors=" ".join(rejected.errors))
            payload, more = self.backend.complete(system, f"{message}\n\n{retry}", ESTIMATE_SCHEMA)
            cost += more
        try:
            return replace(parse_structured(payload, cost, note, retrieved), retried=True)
        except RejectedError as rejected:
            return kept_after_check(rejected, cost, note)


Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass
class ClaudeCode:
    """`claude -p` in an empty directory, without tools, MCP servers or settings."""

    model: str = "sonnet"
    binary: str = "claude"
    timeout: float = 300.0
    runner: Runner = subprocess.run

    @property
    def name(self) -> str:
        return f"claude-{self.model}"

    def complete(
        self, system: str, user: str, schema: Mapping[str, Any] = SCHEMA
    ) -> tuple[dict[str, Any], float]:
        command = [
            self.binary,
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--system-prompt",
            system,
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers": {}}',
            "--setting-sources",
            "",
            "--no-session-persistence",
        ]
        with tempfile.TemporaryDirectory(prefix="harness-eval-") as empty:
            result = self.runner(
                command,
                input=user,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=empty,
                check=False,
            )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise LLMError(f"claude -p failed: {result.stderr.strip()[:300]}") from error
        output = response.get("structured_output")
        cost = float(response.get("total_cost_usd") or 0.0)
        if response.get("is_error") or not isinstance(output, dict):
            raise LLMError(
                f"claude -p returned no estimate: {str(response.get('result'))[:300]}", cost
            )
        return output, cost


@dataclass
class Ollama:
    model: str = "gemma4:12b"
    url: str = "http://localhost:11434"
    timeout: float = 600.0
    client: httpx.Client = field(default_factory=httpx.Client)

    @property
    def name(self) -> str:
        return f"ollama-{self.model}"

    def complete(
        self, system: str, user: str, schema: Mapping[str, Any] = SCHEMA
    ) -> tuple[dict[str, Any], float]:
        response = self.client.post(
            f"{self.url}/api/chat",
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "format": schema,
                "stream": False,
                "think": False,
                "options": {"temperature": 0, "num_ctx": 16384},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = response.json()["message"]["content"]
        try:
            output = json.loads(content)
        except json.JSONDecodeError as error:
            raise LLMError(f"Ollama returned no JSON: {content[:300]}") from error
        if not isinstance(output, dict):
            raise LLMError(f"Ollama returned {content[:300]}")
        return output, 0.0
