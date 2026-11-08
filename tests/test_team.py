import json

import numpy as np
import pytest

from agent_harness.team import (
    SEVERITIES,
    as_json,
    by_severity,
    collect,
    finding_counts,
    markdown,
    matched,
    plan_check,
    plan_totals,
)

NAMES = ["a.py", "b.php", "Cart.ts"]
DIRS = ["src", "src/Cart", "tests", "repo/src"]
DISPOSITION_VALUES = ["accepted", "rejected", "deferred", None, "nonsense"]


def brute_match(first, second):
    return first == second or first.endswith("/" + second) or second.endswith("/" + first)


def pick(rng, items):
    return items[int(rng.integers(len(items)))]


def random_paths(rng, count):
    return {
        f"{pick(rng, DIRS)}/{pick(rng, NAMES)}" if rng.random() < 0.8 else pick(rng, NAMES)
        for _ in range(count)
    }


@pytest.mark.parametrize("seed", range(8))
def test_matched_is_the_pairwise_suffix_match(seed):
    rng = np.random.default_rng(seed)
    plan, changed = random_paths(rng, 6), random_paths(rng, 6)
    expected = {p for p in plan if any(brute_match(p, c) for c in changed)}
    assert matched(plan, changed) == expected


def test_absolute_and_prefixed_paths_match_relative_ones():
    assert matched(["src/a.py"], ["/home/dev/repo/src/a.py"]) == {"src/a.py"}
    assert matched(["repo/src/a.py"], ["src/a.py"]) == {"repo/src/a.py"}
    assert matched(["src/a.py"], ["lib/src/b.py", "xsrc/a.py"]) == set()


@pytest.mark.parametrize("seed", range(8))
def test_plan_precision_and_recall_match_their_definitions(seed):
    rng = np.random.default_rng(seed)
    plan = random_paths(rng, int(rng.integers(1, 7)))
    changed = random_paths(rng, int(rng.integers(1, 7)))

    check = plan_check("SHOP-412", "git", plan, changed)

    hits = [p for p in plan if any(brute_match(p, c) for c in changed)]
    covered = [c for c in changed if any(brute_match(p, c) for p in plan)]
    assert check.precision == pytest.approx(len(hits) / len(plan))
    assert check.recall == pytest.approx(len(covered) / len(changed))
    assert set(check.missed) == changed - set(covered)
    assert set(check.unneeded) == plan - set(hits)


def test_plan_check_normalizes_paths_and_handles_empty_sides():
    check = plan_check("SHOP-412", "git", ["./src/a.py", "src\\b.py", " "], ["src/a.py"])
    assert check.planned == ("src/a.py", "src/b.py")
    assert check.precision == 0.5
    assert check.recall == 1.0
    empty = plan_check("SHOP-412", "git", [], [])
    assert (empty.precision, empty.recall) == (None, None)


@pytest.mark.parametrize("seed", range(5))
def test_plan_totals_pool_and_average_like_the_definitions(seed):
    rng = np.random.default_rng(seed)
    checks = [
        plan_check(f"SHOP-{i}", "git", random_paths(rng, 4), random_paths(rng, 4))
        for i in range(int(rng.integers(1, 9)))
    ]
    totals = plan_totals(checks)

    hits = covered = planned = changed = 0
    precisions, recalls = [], []
    for check in checks:
        plan, diff = set(check.planned), set(check.changed)
        ticket_hits = sum(1 for p in plan if any(brute_match(p, c) for c in diff))
        ticket_covered = sum(1 for c in diff if any(brute_match(p, c) for p in plan))
        hits, covered = hits + ticket_hits, covered + ticket_covered
        planned, changed = planned + len(plan), changed + len(diff)
        precisions.append(ticket_hits / len(plan))
        recalls.append(ticket_covered / len(diff))
    assert totals.precision == pytest.approx(hits / planned)
    assert totals.recall == pytest.approx(covered / changed)
    assert totals.mean_precision == pytest.approx(sum(precisions) / len(precisions))
    assert totals.mean_recall == pytest.approx(sum(recalls) / len(recalls))


def test_plan_totals_without_tickets():
    totals = plan_totals([])
    assert (totals.precision, totals.recall, totals.mean_precision) == (None, None, None)


def random_findings(rng, count):
    severities = [*SEVERITIES, "trivial"]
    return [
        {
            "id": f"F{number}",
            "severity": pick(rng, severities),
            "disposition": pick(rng, DISPOSITION_VALUES),
        }
        for number in range(1, count + 1)
    ]


def brute_counts(findings):
    def bucket(finding):
        value = finding["disposition"]
        return value if value in ("accepted", "rejected", "deferred") else "unset"

    counts = dict.fromkeys(("accepted", "rejected", "deferred", "unset"), 0)
    for finding in findings:
        counts[bucket(finding)] += 1
    decided = counts["accepted"] + counts["rejected"]
    return counts, (counts["accepted"] / decided if decided else None)


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.mark.parametrize("seed", range(6))
def test_finding_counts_and_acceptance_rate_by_severity(tmp_path, seed):
    rng = np.random.default_rng(seed)
    tickets = []
    rounds = {}
    for i in range(int(rng.integers(1, 7))):
        directory = tmp_path / f"SHOP-{i}"
        rounds[directory.name] = int(rng.integers(1, 4))
        for number in range(1, rounds[directory.name] + 1):
            findings = random_findings(rng, int(rng.integers(0, 9)))
            name = (
                "review.json" if number == rounds[directory.name] else f"review.{number:02d}.json"
            )
            write(directory / name, {"ticket": directory.name, "findings": findings})
            tickets.append(findings)

    reviews = collect(sorted(tmp_path.iterdir())).reviews
    rows = by_severity(reviews)

    assert {check.ticket: check.rounds for check in reviews} == rounds
    everything = [finding for findings in tickets for finding in findings]
    for name in SEVERITIES:
        subset = [f for f in everything if f["severity"] == name]
        counts, rate = brute_counts(subset)
        assert rows[name].findings == len(subset)
        assert rows[name].by_disposition == counts
        assert rows[name].acceptance_rate == rate
    other = [f for f in everything if f["severity"] not in SEVERITIES]
    assert ("other" in rows) is bool(other)
    counts, rate = brute_counts(everything)
    assert rows["all"].by_disposition == counts
    assert rows["all"].acceptance_rate == rate


def test_acceptance_rate_leaves_deferred_and_unset_out():
    counts = finding_counts([("major", "deferred"), ("minor", "unset")])
    assert counts.acceptance_rate is None
    counts = finding_counts([("major", "accepted"), ("minor", "rejected"), ("minor", "deferred")])
    assert counts.acceptance_rate == 0.5


def implementer_report(files):
    block = json.dumps({"ticket": "SHOP-3", "files": files, "checks": [], "time_sinks": []})
    return f"Done.\n\n```json harness-implementation\n{block}\n```\n"


@pytest.fixture
def tickets(tmp_path):
    root = tmp_path / "tickets"
    plan = {
        "ticket": "SHOP-1",
        "repositories": ["shop"],
        "plan": [
            {"repo": "shop", "file": "src/Price.php", "change": "round", "tests": True},
            {"repo": "shop", "file": "src/Cart.php", "change": "use it", "tests": False},
        ],
    }
    write(root / "SHOP-1" / "analysis.json", plan)
    write(
        root / "SHOP-1" / "outcome.json",
        {"diff": {"files": ["src/Price.php", "tests/PriceTest.php"]}},
    )
    write(
        root / "SHOP-1" / "review.json",
        {
            "ticket": "SHOP-1",
            "verdict": "pass",
            "findings": [
                {"id": "F1", "severity": "major", "disposition": "accepted"},
                {"id": "F2", "severity": "minor", "disposition": "rejected"},
            ],
        },
    )
    write(root / "SHOP-1" / "escaped.json", [{"commit": "abc", "ticket": "SHOP-1"}])
    write(root / "SHOP-2" / "analysis.json", {**plan, "ticket": "SHOP-2"})
    write(root / "SHOP-2" / "escaped.json", [{"commit": "def", "ticket": "SHOP-2"}])
    write(root / "SHOP-3" / "analysis.json", {**plan, "ticket": "SHOP-3"})
    (root / "SHOP-3" / "implementer.01.md").write_text(implementer_report(["src/Price.php"]))
    (root / "SHOP-3" / "implementer.02.md").write_text(implementer_report(["src/Tax.php"]))
    write(root / "SHOP-4" / "review.json", {"ticket": "SHOP-4", "verdict": "pass", "findings": []})
    return sorted(root.iterdir())


def test_collect_takes_changed_files_from_outcome_and_implementer_reports(tickets):
    metrics = collect(tickets)

    sources = {check.ticket: (check.source, check.changed) for check in metrics.plans}
    assert sources == {
        "SHOP-1": ("outcome", ("src/Price.php", "tests/PriceTest.php")),
        "SHOP-3": ("implementation", ("src/Price.php", "src/Tax.php")),
    }
    assert metrics.without_changes == ["SHOP-2"]


def test_git_wins_over_the_other_sources(tickets):
    metrics = collect(tickets, from_git=lambda key: ["src/Cart.php"] if key == "SHOP-2" else [])
    sources = {check.ticket: check.source for check in metrics.plans}
    assert sources == {"SHOP-1": "outcome", "SHOP-2": "git", "SHOP-3": "implementation"}
    assert metrics.without_changes == []


def test_escaped_defects_after_a_review(tickets):
    reviews = {check.ticket: check for check in collect(tickets).reviews}
    assert set(reviews) == {"SHOP-1", "SHOP-2", "SHOP-4"}
    assert reviews["SHOP-1"].escaped_after_review
    assert not reviews["SHOP-2"].reviewed
    assert not reviews["SHOP-2"].escaped_after_review
    assert not reviews["SHOP-4"].escaped_after_review


def test_markdown_and_json(tickets):
    metrics = collect(tickets)
    text = markdown(metrics)

    assert "| SHOP-1 | outcome | 2 | 2 | 1 | 1 | 50% | 50% |" in text
    assert "| All 2 (pooled) | | 4 | 4 | 2 | 2 | 50% | 50% |" in text
    assert "With a plan but no known changed files" in text
    escaped = "| SHOP-1 | pass | 2 | 1 | 1 | 0 | 0 | 50% | 1 | reviewer passed, defect escaped |"
    assert escaped in text
    assert "| SHOP-2 | - | 0 | 0 | 0 | 0 | 0 | - | 1 | no review |" in text
    assert "| all | 2 | 1 | 1 | 0 | 0 | 50% |" in text
    assert "Escaped defects: 2, of them 1 on reviewed tickets." in text

    data = as_json(metrics)
    assert data["analyst"]["totals"]["precision"] == 0.5
    assert data["analyst"]["tickets"][0]["missed"] == ["tests/PriceTest.php"]
    assert data["reviewer"]["escaped_after_review"] == 1
    assert data["reviewer"]["tickets"][0]["reviewer_passed_defect_escaped"] is True
    json.dumps(data)


def test_markdown_without_data():
    text = markdown(collect([]))
    assert "No ticket has both a plan and known changed files" in text
    assert "No reviews or escaped defects recorded" in text


def test_escaped_must_be_a_list(tmp_path):
    write(tmp_path / "SHOP-1" / "escaped.json", {"commit": "abc"})
    with pytest.raises(ValueError, match="JSON list"):
        collect([tmp_path / "SHOP-1"])
