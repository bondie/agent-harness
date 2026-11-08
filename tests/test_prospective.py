import json

import numpy as np
import pytest

from agent_harness.estimate_check import round_half as round_half_hour
from agent_harness.prospective import (
    KEPT,
    MODES,
    PART_KINDS,
    actual,
    errors,
    estimate_total,
    eval_case,
    hours,
    load_cases,
    part_pairs,
    report,
)

GRID = [k / 2 for k in range(1, 400)]


def brute_round(value):
    return min(GRID, key=lambda candidate: (abs(candidate - value), -candidate))


def brute_errors(pairs):
    signed = [estimate - real for estimate, real in pairs]
    absolute = sorted(abs(error) for error in signed)
    n = len(pairs)
    middle = n // 2
    median = absolute[middle] if n % 2 else (absolute[middle - 1] + absolute[middle]) / 2
    return {
        "n": n,
        "mae": sum(absolute) / n,
        "mdae": median,
        "bias": sum(signed) / n,
        "over": sum(1 for estimate, real in pairs if estimate > real),
        "under": sum(1 for estimate, real in pairs if estimate < real),
    }


def half_hours(rng, high=16):
    return int(rng.integers(0, high + 1)) / 2


def some(rng, items):
    return [items[i] for i in rng.permutation(len(items))[: int(rng.integers(0, len(items) + 1))]]


@pytest.mark.parametrize("value", [0.0, 0.1, 0.25, 0.26, 0.75, 1.25, 2.24, 2.25, 7.74, 13.0])
def test_round_half_hour_matches_the_nearest_grid_point(value):
    assert round_half_hour(value) == brute_round(value)


def test_round_half_hour_on_random_values():
    rng = np.random.default_rng(3)
    for _ in range(500):
        value = float(rng.uniform(0, 30))
        assert round_half_hour(value) == brute_round(value)


def part(kind, value):
    return {"name": kind, "kind": kind, "hours": value, "evidence": ["src/a.py"]}


def adjustment(value):
    return {"hours": value, "reason": "one line", "citation": "SHOP-1"}


def test_bottom_up_total_is_the_parts_plus_adjustments_rounded():
    estimate = {
        "mode": "bottom-up",
        "parts": [part("implementation", 1.2), part("verification", 0.6)],
        "adjustments": [adjustment(0.4)],
        "total": 99,
    }
    assert estimate_total(estimate) == brute_round(1.2 + 0.6 + 0.4)


def test_reference_total_is_the_median_plus_adjustments_rounded():
    estimate = {
        "mode": "reference",
        "reference_median": 3.5,
        "parts": [part("implementation", 9)],
        "adjustments": [adjustment(-1.25)],
        "total": 99,
    }
    assert estimate_total(estimate) == brute_round(3.5 - 1.25)


def test_the_total_is_the_one_the_estimate_check_computes():
    estimate = {"mode": "bottom-up", "parts": [part("release", 1.15), part("release", 0.1)]}
    assert estimate_total(estimate) == 1.5


def test_a_total_is_never_below_half_an_hour():
    estimate = {"mode": "reference", "reference_median": 1.0, "adjustments": [adjustment(-2)]}
    assert estimate_total(estimate) == 0.5


@pytest.mark.parametrize(
    ("estimate", "message"),
    [
        ({"mode": "gut"}, "mode"),
        ({"mode": "reference", "reference_median": None}, "reference_median"),
        ({"mode": "bottom-up", "parts": [{"hours": "two"}]}, "number"),
        ({"mode": "bottom-up", "parts": [{"hours": float("nan")}]}, "number"),
        ({"mode": "reference", "reference_median": 1, "adjustments": [{"hours": 1}]}, "citation"),
        ({"mode": "bottom-up", "parts": [{"kind": "release", "hours": 1}]}, "evidence"),
    ],
)
def test_estimate_total_rejects_malformed_estimates(estimate, message):
    with pytest.raises(ValueError, match=message):
        estimate_total(estimate)


@pytest.mark.parametrize("seed", range(6))
def test_errors_match_their_definitions(seed):
    rng = np.random.default_rng(seed)
    pairs = [(half_hours(rng), half_hours(rng)) for _ in range(int(rng.integers(1, 26)))]
    result = errors(pairs)
    expected = brute_errors(pairs)
    assert result is not None
    assert result.n == expected["n"]
    assert result.mae == pytest.approx(expected["mae"])
    assert result.mdae == pytest.approx(expected["mdae"])
    assert result.bias == pytest.approx(expected["bias"])
    assert (result.over, result.under) == (expected["over"], expected["under"])


def test_no_pairs_no_errors():
    assert errors([]) is None


def random_case(rng, index, failures=False):
    mode = MODES[int(rng.integers(len(MODES)))]
    kinds = some(rng, PART_KINDS) or (list(PART_KINDS[:1]) if mode == "bottom-up" else [])
    estimate = {
        "ticket": f"SHOP-{index}",
        "mode": mode,
        "reference_median": half_hours(rng) + 0.5 if mode == "reference" else None,
        "parts": [part(kind, half_hours(rng, 8)) for kind in kinds for _ in range(2)],
        "adjustments": [adjustment(half_hours(rng, 2)) for _ in range(int(rng.integers(0, 3)))],
        "total": half_hours(rng) + 0.5,
    }
    if failures and rng.random() < 0.3:
        estimate["adjustments"].append({"hours": 1.0, "reason": "gut feeling", "citation": ""})
    measured = {kind: half_hours(rng, 8) for kind in some(rng, PART_KINDS)}
    total = sum(measured.values()) or half_hours(rng) + 0.5
    return eval_case(
        f"SHOP-{index}", estimate, None, actual(total, list(measured.items()), "measured"), None
    )


def brute_failed(case):
    return any(not item["citation"] for item in case["estimate"]["adjustments"])


def brute_case_total(case):
    estimate = case["estimate"]
    if brute_failed(case):
        return estimate["total"]
    extra = sum(item["hours"] for item in estimate["adjustments"])
    if estimate["mode"] == "reference":
        return brute_round(estimate["reference_median"] + extra)
    return brute_round(sum(item["hours"] for item in estimate["parts"]) + extra)


def row(label, expected):
    if expected is None:
        return f"| {label} | 0 | - | - | - | - | - |"
    return (
        f"| {label} | {expected['n']} | {expected['mae']:.2f} | {expected['mdae']:.2f} "
        f"| {expected['bias']:+.2f} | {expected['over']} | {expected['under']} |"
    )


@pytest.mark.parametrize("seed", range(4))
def test_report_rows_match_brute_force_groups(seed):
    rng = np.random.default_rng(seed)
    cases = [random_case(rng, index, failures=True) for index in range(30)]
    failed = [case for case in cases if brute_failed(case)]
    checked = [case for case in cases if not brute_failed(case)]

    text = report(cases)

    def total_errors(subset):
        pairs = [(brute_case_total(case), case["actual"]["total"]) for case in subset]
        return brute_errors(pairs) if pairs else None

    assert failed
    assert all(case["check_errors"] for case in failed)
    assert not any("check_errors" in case for case in checked)
    assert row("all", total_errors(checked)) in text
    assert row(KEPT, total_errors(failed)) in text
    for mode in MODES:
        assert (
            row(mode, total_errors([c for c in checked if c["estimate"]["mode"] == mode])) in text
        )
    for kind in PART_KINDS:
        pairs = []
        for case in checked:
            if case["estimate"]["parts"] and kind in case["actual"]["parts"]:
                estimated = 0.0
                for item in case["estimate"]["parts"]:
                    if item["kind"] == kind:
                        estimated += item["hours"]
                pairs.append((estimated, case["actual"]["parts"][kind]))
        assert row(kind, brute_errors(pairs) if pairs else None) in text
    assert f"30 cases, {len(failed)} of them kept after the estimate check failed" in text
    assert "actual time from: measured" in text


def test_a_case_the_check_rejects_keeps_its_stated_total_and_errors():
    estimate = {
        "mode": "reference",
        "reference_median": 2.0,
        "adjustments": [{"hours": 1, "reason": "gut", "citation": ""}],
        "total": 3.0,
    }
    case = eval_case("SHOP-1", estimate, None, actual(2.0, [], "worklog"), None)
    assert case["check_errors"]
    assert "citation" in case["check_errors"][0]
    assert f"| {KEPT} | 1 | 1.00 |" in report([case])


def test_a_rejected_estimate_without_a_stated_total_is_not_a_case(tmp_path):
    estimate = {"mode": "gut", "total": None}
    with pytest.raises(ValueError, match="states no total"):
        eval_case("SHOP-1", estimate, None, actual(2.0, [], "worklog"), None)
    case = {"estimate": estimate, "actual": {"total": 1.0}, "check_errors": ["mode"]}
    (tmp_path / "SHOP-1.json").write_text(json.dumps(case))
    with pytest.raises(ValueError, match="states no total"):
        load_cases(tmp_path)


def test_part_pairs_count_a_missing_kind_in_the_estimate_as_zero():
    case = random_case(np.random.default_rng(0), 1)
    case["estimate"]["parts"] = [part("implementation", 2.0)]
    case["actual"]["parts"] = {"implementation": 1.0, "release": 0.5}
    assert part_pairs([case], "release") == [(0.0, 0.5)]
    assert part_pairs([case], "verification") == []


def test_actual_time():
    assert actual(None, [("implementation", 1.0), ("release", 0.5)], "worklog") == {
        "total": 1.5,
        "parts": {"implementation": 1.0, "release": 0.5},
        "source": "worklog",
    }
    assert actual(3.0, [("implementation", 1.0)], "measured")["total"] == 3.0


@pytest.mark.parametrize(
    ("total", "parts", "source", "message"),
    [
        (None, [], "measured", "Give the actual time"),
        (1.0, [], "guess", "Source"),
        (None, [("design", 1.0)], "measured", "Part kind"),
        (None, [("release", 1.0), ("release", 2.0)], "measured", "twice"),
    ],
)
def test_actual_time_errors(total, parts, source, message):
    with pytest.raises(ValueError, match=message):
        actual(total, parts, source)


@pytest.mark.parametrize("text", ["-1", "nan", "inf", "two"])
def test_hours_must_be_a_finite_non_negative_number(text):
    with pytest.raises(ValueError, match=r"[Hh]ours"):
        hours(text)


def test_load_cases(tmp_path):
    with pytest.raises(ValueError, match="No eval cases"):
        load_cases(tmp_path)
    (tmp_path / "SHOP-1.json").write_text(json.dumps({"estimate": {"mode": "bottom-up"}}))
    with pytest.raises(ValueError, match="no actual total"):
        load_cases(tmp_path)
    (tmp_path / "SHOP-1.json").write_text(json.dumps({"actual": {"total": 1}}))
    with pytest.raises(ValueError, match="no estimate"):
        load_cases(tmp_path)
