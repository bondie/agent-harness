import json
import subprocess
from pathlib import Path

import httpx
import pytest

from agent_harness.baselines import NeighbourMedian
from agent_harness.dataset import load_targets
from agent_harness.llm import (
    AGENT_FILE,
    ESTIMATE_SCHEMA,
    REJECTED,
    REJECTED_UNCITED,
    SCHEMA,
    ClaudeCode,
    LLMError,
    LLMEstimator,
    Ollama,
    agent_prompt,
    parse_estimate,
    parse_structured,
    redact_estimates,
)
from agent_harness.memory import Memory, lessons

ANSWER = {
    "median_hours": 1.5,
    "p25_hours": 1.0,
    "p75_hours": 2.5,
    "cited": ["SHOP-4"],
    "reasoning": "Like SHOP-4.",
}


BLOCK = {
    "ticket": "SHOP-6",
    "mode": "reference",
    "reference_median": 1.0,
    "parts": [],
    "adjustments": [{"hours": 0.5, "reason": "two exports", "citation": "SHOP-5"}],
    "total": 9,
}


class Recorder:
    name = "fake"

    def __init__(self, answer=None):
        self.answer = answer or ANSWER
        self.calls = []
        self.schemas = []

    def complete(self, system, user, schema=SCHEMA):
        self.calls.append((system, user))
        self.schemas.append(schema)
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


def recent_median(searcher, ticket):
    baseline = NeighbourMedian(searcher, since="2026-01-01").estimate(ticket)
    assert baseline is not None
    return baseline.median


def test_the_old_variant_records_the_reference_median(store, searcher):
    shop = target(store, "SHOP-6")
    estimate = LLMEstimator(Recorder(), searcher, since="2026-01-01").estimate(shop)
    assert estimate is not None
    assert estimate.reference_median == recent_median(searcher, shop)
    assert estimate.lessons is None
    assert LLMEstimator(Recorder(), searcher, context="none").estimate(
        target(store, "SHOP-6")
    ) == parse_estimate(ANSWER, 0.01)


def test_the_structured_variant_lets_code_compute_the_estimate(store, searcher):
    backend = Recorder(BLOCK)
    estimator = LLMEstimator(backend, searcher, since="2026-01-01", structured=True)

    shop = target(store, "SHOP-6")
    estimate = estimator.estimate(shop)

    assert estimate is not None
    assert estimator.name == "llm-hybrid-recent-structured@fake"
    assert (estimate.median, estimate.p25, estimate.p75) == (1.5, None, None)
    assert estimate.cited == ("SHOP-5",)
    assert estimate.reference_median == recent_median(searcher, shop)
    assert backend.schemas == [ESTIMATE_SCHEMA]
    system = backend.calls[0][0]
    assert "harness-estimate block only" in system
    assert "median_hours" not in system


def test_an_uncited_adjustment_fails_with_its_cost():
    block = {**BLOCK, "adjustments": [{"hours": 1, "reason": "bigger", "citation": ""}]}
    with pytest.raises(LLMError, match=f"^{REJECTED_UNCITED}") as failure:
        parse_structured(block, 0.02)
    assert failure.value.cost_usd == 0.02


@pytest.mark.parametrize(
    "payload",
    [
        {**BLOCK, "reference_median": None},
        {**BLOCK, "mode": "bottom-up", "parts": []},
        {**BLOCK, "adjustments": [{"hours": 1, "reason": "x", "citation": "see above"}]},
        {"median_hours": 2},
    ],
)
def test_other_invalid_blocks_fail_as_rejected(payload):
    with pytest.raises(LLMError, match=f"^{REJECTED}"):
        parse_structured(payload, 0.0)


def test_structured_citations_come_from_parts_and_adjustments():
    block = {
        **BLOCK,
        "parts": [{"name": "a", "kind": "release", "hours": 0.5, "evidence": ["SHOP-4", "a note"]}],
        "adjustments": [
            {"hours": 0.5, "reason": "x", "citation": "SHOP-4"},
            {"hours": 0.5, "reason": "y", "citation": " measured:deploy "},
        ],
    }
    assert parse_structured(block, 0.0).cited == ("SHOP-4", "measured:deploy")


def load_memory_text(text: str) -> Memory:
    return Memory(tuple(lessons(text)))


def test_the_memory_variant_adds_only_lessons_from_before_the_ticket(store, searcher):
    memory = load_memory_text(
        "- Release is minutes (source: retro, 2026-02-01)\n"
        "- Learned on the day (source: retro, 2026-03-01)\n"
        "- Expired (source: retro, 2026-01-02, expires 2026-02-01)\n"
        "- No date\n"
    )
    backend = Recorder(BLOCK)
    estimator = LLMEstimator(backend, searcher, structured=True, memory=memory)

    estimate = estimator.estimate(target(store, "SHOP-6"))

    assert estimate is not None
    assert estimator.name == "llm-hybrid-structured+memory@fake"
    assert (estimate.lessons, estimate.lessons_dated, estimate.lessons_undated) == (1, 3, 1)
    system = backend.calls[0][0]
    assert system.endswith("\n\n- Release is minutes (source: retro, 2026-02-01)")
    assert "Learned on the day" not in system
    assert "No date" not in system


def test_a_memory_without_usable_lessons_leaves_the_prompt_alone(store, searcher):
    backend = Recorder(BLOCK)
    memory = load_memory_text("- No date\n")
    LLMEstimator(backend, searcher, structured=True, memory=memory).estimate(
        target(store, "SHOP-6")
    )
    plain = Recorder(BLOCK)
    LLMEstimator(plain, searcher, structured=True).estimate(target(store, "SHOP-6"))
    assert backend.calls == plain.calls


def test_backends_send_the_schema_they_are_given():
    calls = []

    def runner(command, **options):
        calls.append(command)
        return completed(json.dumps({"structured_output": BLOCK}))

    ClaudeCode(runner=runner).complete("system", "ticket", ESTIMATE_SCHEMA)
    assert json.loads(calls[0][calls[0].index("--json-schema") + 1]) == ESTIMATE_SCHEMA
    requests: list[httpx.Request] = []
    ollama_with(json.dumps(BLOCK), requests).complete("system", "ticket", ESTIMATE_SCHEMA)
    assert json.loads(requests[0].content)["format"] == ESTIMATE_SCHEMA


def test_a_failed_claude_call_keeps_its_cost():
    stdout = json.dumps({"is_error": True, "result": "limit", "total_cost_usd": 0.03})
    backend = ClaudeCode(runner=lambda command, **options: completed(stdout))
    with pytest.raises(LLMError) as failure:
        backend.complete("system", "ticket")
    assert failure.value.cost_usd == 0.03
