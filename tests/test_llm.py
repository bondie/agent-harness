import json
import subprocess
from pathlib import Path

import httpx
import pytest

from agent_harness.dataset import load_targets
from agent_harness.llm import (
    AGENT_FILE,
    SCHEMA,
    ClaudeCode,
    LLMError,
    LLMEstimator,
    Ollama,
    agent_prompt,
    parse_estimate,
    redact_estimates,
)

ANSWER = {
    "median_hours": 1.5,
    "p25_hours": 1.0,
    "p75_hours": 2.5,
    "cited": ["SHOP-4"],
    "reasoning": "Like SHOP-4.",
}


class Recorder:
    name = "fake"

    def __init__(self, answer=None):
        self.answer = answer or ANSWER
        self.calls = []

    def complete(self, system, user):
        self.calls.append((system, user))
        return self.answer, 0.01


def target(store, key):
    return next(t for t in load_targets(store, "2025-01-01", "2027-01-01") if t.key == key)


def test_agent_prompt_is_the_shipped_estimator_without_front_matter():
    prompt = agent_prompt()
    assert prompt.startswith("You estimate the effort")
    assert "name: estimator" in AGENT_FILE.read_text()
    assert "name: estimator" not in prompt


def test_agent_prompt_without_front_matter(tmp_path: Path):
    path = tmp_path / "plain.md"
    path.write_text("Just a prompt.\n")
    assert agent_prompt(path) == "Just a prompt."


def test_redaction_masks_numbers_only_on_estimate_lines():
    text = "Fix the export in 3 steps.\nOdhad: 1,5 h\nEstimate 2 hours incl. review\n#4 retry"

    redacted, changed = redact_estimates(text)

    assert changed
    assert redacted.splitlines() == [
        "Fix the export in 3 steps.",
        "Odhad: [redacted]",
        "Estimate [redacted] incl. review",
        "#4 retry",
    ]
    assert redact_estimates("Nacenit prosím") == ("Nacenit prosím", False)


@pytest.mark.parametrize("context", ["none", "keyword", "hybrid"])
def test_llm_estimator_sends_the_ticket_and_its_reference_class(store, searcher, context):
    backend = Recorder()
    estimator = LLMEstimator(backend, searcher, context=context, process_note="AI since 2026")

    estimate = estimator.estimate(target(store, "SHOP-6"))

    assert estimate is not None
    assert (estimate.p25, estimate.median, estimate.p75) == (1.0, 1.5, 2.5)
    assert estimate.cited == ("SHOP-4",)
    assert estimate.cost_usd == 0.01
    assert estimator.name == f"llm-{context}@fake"
    system, user = backend.calls[0]
    assert "Offline evaluation" in system
    assert "Ticket SHOP-6 (Bug), created 2026-03-01" in user
    assert "Process change: AI since 2026" in user
    assert "SHOP-7" not in user
    if context == "none":
        assert "estimate from the ticket alone" in user
        return
    assert "SHOP-4 · Bug · created 2026-01-05 · logged 1.00 h" in user
    assert "1.5 h" not in user.split("Reference class")[0]


def test_recent_reference_class(store, searcher):
    backend = Recorder()
    estimator = LLMEstimator(backend, searcher, since="2026-01-01")

    estimator.estimate(target(store, "SHOP-6"))

    assert estimator.name == "llm-hybrid-recent@fake"
    user = backend.calls[0][1]
    assert "SHOP-4" in user
    assert "SHOP-1 " not in user


def test_reference_class_can_be_empty(store, searcher):
    backend = Recorder()
    LLMEstimator(backend, searcher).estimate(target(store, "SHOP-1"))
    assert "no similar ticket was finished" in backend.calls[0][1]


def test_redacted_tickets_are_marked(store, searcher):
    shop = target(store, "SHOP-6")
    shop = type(shop)(**{**shop.__dict__, "description": "Odhad 1 h"})
    estimate = LLMEstimator(Recorder(), searcher, context="none").estimate(shop)
    assert estimate is not None
    assert estimate.note == "redacted"


def test_parse_estimate_orders_the_range_and_rejects_garbage():
    estimate = parse_estimate({"median_hours": 3, "p25_hours": 4, "p75_hours": 1}, 0.0)
    assert (estimate.p25, estimate.median, estimate.p75) == (1, 3, 4)
    assert estimate.cited == ()
    with pytest.raises(LLMError, match="Malformed"):
        parse_estimate({"median_hours": "lots"}, 0.0)


def completed(stdout: str, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr=stderr)


def test_claude_code_runs_headless_without_tools_or_settings():
    calls = []

    def runner(command, **options):
        calls.append((command, options))
        return completed(json.dumps({"structured_output": ANSWER, "total_cost_usd": 0.02}))

    output, cost = ClaudeCode(model="haiku", runner=runner).complete("system", "ticket")

    assert output == ANSWER
    assert cost == 0.02
    command, options = calls[0]
    assert command[:4] == ["claude", "-p", "--model", "haiku"]
    assert json.loads(command[command.index("--json-schema") + 1]) == SCHEMA
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in command
    assert options["input"] == "ticket"
    assert "harness-eval-" in options["cwd"]


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("not json", "failed: boom"),
        (json.dumps({"is_error": True, "result": "limit"}), "no estimate: limit"),
        (json.dumps({"result": "prose"}), "no estimate: prose"),
    ],
)
def test_claude_code_failures(stdout, message):
    backend = ClaudeCode(runner=lambda command, **options: completed(stdout, "boom"))
    with pytest.raises(LLMError, match=message):
        backend.complete("system", "ticket")


def ollama_with(content: str, requests: list[httpx.Request]) -> Ollama:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"message": {"content": content}})

    return Ollama(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_ollama_uses_the_schema_and_no_thinking():
    requests: list[httpx.Request] = []

    output, cost = ollama_with(json.dumps(ANSWER), requests).complete("system", "ticket")

    assert output == ANSWER
    assert cost == 0.0
    body = json.loads(requests[0].content)
    assert body["format"] == SCHEMA
    assert body["think"] is False
    assert body["options"]["temperature"] == 0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


@pytest.mark.parametrize("content", ["no json", "[1, 2]"])
def test_ollama_failures(content):
    with pytest.raises(LLMError, match="Ollama"):
        ollama_with(content, []).complete("system", "ticket")


def test_backend_names():
    assert ClaudeCode(model="sonnet").name == "claude-sonnet"
    assert Ollama(model="gemma4:12b").name == "ollama-gemma4:12b"
