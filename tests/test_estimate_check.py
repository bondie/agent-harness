import json
from pathlib import Path
from typing import Any

import pytest

from agent_harness.estimate_check import (
    MISSING,
    NO_EVIDENCE,
    UNCITED,
    check_report,
    find_block,
    is_citation,
    is_estimate,
    round_half,
    validate_and_total,
)

ROOT = Path(__file__).resolve().parents[1]


def reference(median: Any = 1.5, *adjustments: dict[str, Any], **fields: Any) -> dict[str, Any]:
    return {
        "ticket": "SHOP-412",
        "mode": "reference",
        "reference_median": median,
        "parts": [],
        "adjustments": list(adjustments),
        "total": 99,
        **fields,
    }


def part(hours: Any = 1.0, kind: str = "implementation", evidence: Any = None) -> dict[str, Any]:
    return {
        "name": kind,
        "kind": kind,
        "hours": hours,
        "evidence": ["SHOP-4"] if evidence is None else evidence,
    }


def bottom_up(*parts: Any, adjustments: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
    return {
        "mode": "bottom-up",
        "reference_median": None,
        "parts": list(parts),
        "adjustments": list(adjustments),
    }


def adjustment(hours: Any = 0.5, citation: Any = "SHOP-398") -> dict[str, Any]:
    return {"hours": hours, "reason": "a migration on top", "citation": citation}


def test_the_hook_and_the_package_share_one_module():
    hook = ROOT / "hooks" / "estimate_check.py"
    package = ROOT / "src" / "agent_harness" / "estimate_check.py"
    assert hook.read_bytes() == package.read_bytes(), (
        "src/agent_harness/estimate_check.py must be a copy of hooks/estimate_check.py"
    )


def brute_force_round(hours: float) -> float:
    grid = [step / 2 for step in range(1, 400)]
    return min(grid, key=lambda candidate: (abs(candidate - hours), -candidate))


@pytest.mark.parametrize(
    "hours", [-3, -0.2, 0, 0.1, 0.25, 0.26, 0.74, 0.75, 1.0, 1.24, 1.25, 1.26, 2.6, 7.75, 40.1]
)
def test_rounding_matches_the_nearest_half_hour_with_ties_up(hours):
    assert round_half(hours) == brute_force_round(hours)


def test_rounding_is_not_fooled_by_float_error():
    assert round_half(1.15 + 0.1) == 1.5


def test_reference_mode_is_the_median_plus_the_adjustments():
    block = reference(1.25, adjustment(0.5), adjustment(-0.25, "measured:CI step 3"))
    assert validate_and_total(block) == (1.5, [])


def test_reference_mode_without_adjustments_keeps_the_median():
    assert validate_and_total(reference(1.25)) == (1.5, [])
    assert validate_and_total(reference(0.2)) == (0.5, [])


def test_bottom_up_is_the_sum_of_the_parts_plus_the_adjustments():
    block = bottom_up(
        part(1.0),
        part(0.25, "verification", ["src/shop/export.py"]),
        part(0.5, "release", ["measured:release of 2 bundles"]),
        adjustments=(adjustment(0.5),),
    )
    assert validate_and_total(block) == (2.5, [])


def test_the_models_total_is_ignored():
    assert validate_and_total(reference(2, total=7))[0] == 2.0
    assert validate_and_total(reference(2, total=None))[0] == 2.0


@pytest.mark.parametrize("citation", [None, "", "   "])
def test_an_adjustment_without_a_citation_is_rejected(citation):
    total, errors = validate_and_total(reference(1.5, adjustment(citation=citation)))
    assert total is None
    assert len(errors) == 1
    assert UNCITED in errors[0]
    assert 'adjustments[0] ("a migration on top")' in errors[0]


def test_an_adjustment_with_no_citation_field_is_rejected():
    block = reference(1.5, {"hours": 1, "reason": "bigger"})
    assert UNCITED in validate_and_total(block)[1][0]


@pytest.mark.parametrize(
    "citation",
    [
        "see above",
        "/abs/path.py",
        "~/notes.md",
        "https://tracker.example/SHOP-1",
        "../up.py",
        "SHOP-4 and SHOP-5",
        "measured:",
        "C:\\repo\\file.py",
        "assumption",
        4,
        ["SHOP-4"],
    ],
)
def test_a_citation_must_look_like_a_key_a_path_or_a_measurement(citation):
    total, errors = validate_and_total(reference(1.5, adjustment(citation=citation)))
    assert total is None
    assert "is not a ticket key" in errors[0]
    assert UNCITED not in errors[0]


@pytest.mark.parametrize(
    "citation",
    [
        "SHOP-412",
        " PROJ-1 ",
        "src/shop/export.py",
        "README.md",
        "src/a.py:12",
        "docs/",
        "measured:CI step 3",
        "measured: 9 min",
    ],
)
def test_citation_shapes(citation):
    assert is_citation(citation)


@pytest.mark.parametrize("mode", ["reference", "bottom-up"])
@pytest.mark.parametrize("evidence", [[], ["assumption"], [""], "SHOP-4", None])
def test_every_part_needs_evidence_in_both_modes(mode, evidence):
    item = part()
    item["evidence"] = evidence
    block = {**reference(1.5), "mode": mode, "parts": [item]}
    total, errors = validate_and_total(block)
    assert total is None
    assert len(errors) == 1
    assert errors[0].startswith(f'parts[0] ("implementation") {NO_EVIDENCE}: cite a ticket key')


def test_evidence_counts_when_any_item_is_a_citation():
    item = part(evidence=["about a quarter of a 300-line ticket", "SHOP-4"])
    assert validate_and_total(bottom_up(item)) == (1.0, [])


@pytest.mark.parametrize("hours", [-1, "1", None, True, float("nan"), float("inf")])
def test_part_hours_must_be_a_number_of_zero_or_more(hours):
    errors = validate_and_total(bottom_up(part(hours)))[1]
    assert errors == ['parts[0] ("implementation"): hours must be a number of 0 or more.']


def test_part_kind_is_one_of_three():
    errors = validate_and_total(bottom_up(part(kind="testing")))[1]
    assert errors == [
        'parts[0] ("testing"): kind must be one of implementation, verification, release.'
    ]


@pytest.mark.parametrize("hours", ["0.5", None, False, float("nan")])
def test_adjustment_hours_must_be_a_number(hours):
    errors = validate_and_total(reference(1.5, adjustment(hours)))[1]
    assert errors == [
        'adjustments[0] ("a migration on top"): hours must be a number, negative to go down.'
    ]


@pytest.mark.parametrize("median", [None, -1, "1.5", float("inf")])
def test_reference_mode_needs_a_median(median):
    total, errors = validate_and_total(reference(median))
    assert total is None
    assert errors[0].startswith("reference mode needs reference_median")


def test_bottom_up_needs_a_part():
    assert validate_and_total(bottom_up())[1] == ["bottom-up mode needs at least one part."]


@pytest.mark.parametrize("mode", [None, "guess", "Reference"])
def test_the_mode_is_bottom_up_or_reference(mode):
    total, errors = validate_and_total(reference(1.5, mode=mode))
    assert total is None
    assert errors == ['mode must be "bottom-up" or "reference".']


@pytest.mark.parametrize("block", [None, [], "estimate", 3])
def test_the_block_is_an_object(block):
    assert validate_and_total(block) == (
        None,
        ["The harness-estimate block must be one JSON object."],
    )


def test_lists_and_items_have_the_right_shape():
    block = reference(1.5, parts="one part", adjustments=[3])
    errors = validate_and_total(block)[1]
    assert errors == [
        "parts must be a list.",
        "adjustments[0] must be an object with hours, reason and citation.",
    ]
    errors = validate_and_total(bottom_up("part"))[1]
    assert errors == ["parts[0] must be an object with name, kind, hours and evidence."]


def test_missing_lists_are_empty():
    block = {"mode": "reference", "reference_median": 2}
    assert validate_and_total(block) == (2.0, [])


def test_every_broken_rule_is_reported():
    block = bottom_up(part(-1, "testing", []), adjustments=(adjustment(None, ""),))
    errors = validate_and_total(block)[1]
    rules = ["hours must be a number of 0", "kind must be", NO_EVIDENCE, "negative", UNCITED]
    assert len(errors) == len(rules)
    for error, rule in zip(errors, rules, strict=True):
        assert rule in error


def report(block: object, fence: str = "```", prose: str = "- **Estimate:** 2 h") -> str:
    body = block if isinstance(block, str) else json.dumps(block)
    return f"{prose}\n\n{fence}json harness-estimate\n{body}\n{fence}\n"


def test_find_block_reads_the_last_block():
    text = report(reference(1)) + report(reference(3), fence="````")
    assert find_block(text) == (reference(3), [])


def test_find_block_reports_a_missing_or_malformed_block():
    assert find_block("- **Estimate:** 2 h\n```json\n{}\n```") == (None, [MISSING])
    block, errors = find_block(report("{not json"))
    assert block is None
    assert errors[0].startswith("The harness-estimate block is not valid JSON")


def test_check_report_accepts_a_report_whose_total_matches():
    assert check_report(report(reference(1.5, adjustment(), total=2.0))) == (2.0, [])


def test_check_report_rejects_a_stated_total_that_differs():
    total, errors = check_report(report(reference(1.5, adjustment(), total=3)))
    assert total == 2.0
    assert errors == [
        "total is 3, but the block computes to 2 h. Set total to 2 and give 2 h as the Estimate."
    ]


def test_check_report_passes_on_block_errors():
    assert check_report("no block") == (None, [MISSING])
    assert UNCITED in check_report(report(reference(1.5, adjustment(citation=""))))[1][0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("- **Estimate:** 2 h\n- **Basis:** …", True),
        ("**Estimate: 2 h**", True),
        ("Estimate: 2 h", True),
        ("```json harness-estimate\n{}\n```", True),
        ("Added to memory:\n- Same ticket: 4 h holds when trimmed", False),
        ("| SHOP-1 | Estimate 4 | Logged 2 |", False),
        ("", False),
    ],
)
def test_only_estimates_are_checked(text, expected):
    assert is_estimate(text) is expected


def test_the_example_in_the_estimator_prompt_passes_the_check():
    prompt = (ROOT / "agents" / "estimator.md").read_text()
    assert is_estimate(prompt)
    assert check_report(prompt) == (2.0, [])
