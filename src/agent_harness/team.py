"""How the analyst and the reviewer did, from the artifacts of the tickets.

Analyst: the files of the implementation plan against the files that changed. Precision is the
share of planned files that changed, recall the share of changed files the plan named. Two
paths match when they are equal or one ends with the other after a slash, so a repository
prefix or an absolute path still matches a repository-relative one.

Reviewer: the findings by severity and by the disposition the retro gave them, and the defects
that reached production after a review. Each review round counts its own findings: F1 of round 1
and F1 of round 2 are two findings.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .artifacts import DISPOSITIONS, Json, read_json, read_object, report_blocks, review_rounds

SEVERITIES = ("blocker", "major", "minor")
UNSET = "unset"
OTHER = "other"


def normalize(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./")


def suffixes(path: str) -> set[str]:
    parts = path.split("/")
    return {"/".join(parts[i:]) for i in range(len(parts)) if parts[i]}


def matched(paths: Iterable[str], others: Iterable[str]) -> set[str]:
    """The paths that match at least one of the others."""
    other_set = set(others)
    other_suffixes: set[str] = set()
    for other in other_set:
        other_suffixes |= suffixes(other)
    return {path for path in paths if path in other_suffixes or suffixes(path) & other_set}


@dataclass(frozen=True)
class PlanCheck:
    ticket: str
    source: str
    planned: tuple[str, ...]
    changed: tuple[str, ...]
    planned_changed: tuple[str, ...]
    covered: tuple[str, ...]

    @property
    def missed(self) -> tuple[str, ...]:
        return tuple(path for path in self.changed if path not in self.covered)

    @property
    def unneeded(self) -> tuple[str, ...]:
        return tuple(path for path in self.planned if path not in self.planned_changed)

    @property
    def precision(self) -> float | None:
        return len(self.planned_changed) / len(self.planned) if self.planned else None

    @property
    def recall(self) -> float | None:
        return len(self.covered) / len(self.changed) if self.changed else None


def plan_check(
    ticket: str, source: str, planned: Iterable[str], changed: Iterable[str]
) -> PlanCheck:
    plan = sorted({normalize(path) for path in planned if normalize(path)})
    diff = sorted({normalize(path) for path in changed if normalize(path)})
    return PlanCheck(
        ticket=ticket,
        source=source,
        planned=tuple(plan),
        changed=tuple(diff),
        planned_changed=tuple(sorted(matched(plan, diff))),
        covered=tuple(sorted(matched(diff, plan))),
    )


@dataclass(frozen=True)
class PlanTotals:
    tickets: int
    planned: int
    changed: int
    planned_changed: int
    covered: int
    precision: float | None
    recall: float | None
    mean_precision: float | None
    mean_recall: float | None


def plan_totals(checks: Sequence[PlanCheck]) -> PlanTotals:
    planned = sum(len(check.planned) for check in checks)
    changed = sum(len(check.changed) for check in checks)
    planned_changed = sum(len(check.planned_changed) for check in checks)
    covered = sum(len(check.covered) for check in checks)
    return PlanTotals(
        tickets=len(checks),
        planned=planned,
        changed=changed,
        planned_changed=planned_changed,
        covered=covered,
        precision=planned_changed / planned if planned else None,
        recall=covered / changed if changed else None,
        mean_precision=_mean(check.precision for check in checks),
        mean_recall=_mean(check.recall for check in checks),
    )


def _mean(values: Iterable[float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return sum(known) / len(known) if known else None


def plan_files(analysis: Json | None) -> list[str]:
    plan = analysis.get("plan") if analysis else None
    if not isinstance(plan, list):
        return []
    return [str(item["file"]) for item in plan if isinstance(item, dict) and item.get("file")]


ChangedFiles = Callable[[str], list[str]]


def changed_files(directory: Path, from_git: ChangedFiles | None) -> tuple[str, list[str]]:
    """The files the ticket changed and where they come from: git, the exported outcome, or
    the implementer's reports."""
    if from_git is not None:
        files = from_git(directory.name)
        if files:
            return "git", files
    outcome = read_object(directory / "outcome.json") or {}
    diff = outcome.get("diff") or {}
    if diff.get("files"):
        return "outcome", [str(path) for path in diff["files"]]
    reported = [
        str(path)
        for block in report_blocks(directory, "implementation")
        for path in block.get("files") or []
    ]
    if reported:
        return "implementation", reported
    return "", []


def disposition(finding: Json) -> str:
    value = finding.get("disposition")
    return value if value in DISPOSITIONS else UNSET


def severity(finding: Json) -> str:
    value = finding.get("severity")
    return value if value in SEVERITIES else OTHER


@dataclass(frozen=True)
class ReviewCheck:
    ticket: str
    reviewed: bool
    verdict: str | None
    findings: tuple[tuple[str, str], ...]
    """(severity, disposition) of each finding of every round."""
    escaped: int
    rounds: int = 1

    @property
    def escaped_after_review(self) -> bool:
        return self.reviewed and self.escaped > 0


def review_check(directory: Path) -> ReviewCheck | None:
    reviews = [read_object(path) or {} for path in review_rounds(directory)]
    escaped = read_json(directory / "escaped.json")
    escaped = [] if escaped is None else escaped
    if not isinstance(escaped, list):
        raise ValueError(f"{directory / 'escaped.json'} must hold a JSON list")
    if not reviews and not escaped:
        return None
    latest = reviews[-1] if reviews else None
    return ReviewCheck(
        ticket=directory.name,
        reviewed=latest is not None,
        verdict=str(latest["verdict"]) if latest and latest.get("verdict") else None,
        findings=tuple(
            (severity(finding), disposition(finding))
            for review in reviews
            for finding in _findings(review)
        ),
        escaped=len(escaped),
        rounds=len(reviews),
    )


def _findings(review: Json) -> list[Json]:
    findings = review.get("findings")
    return (
        [item for item in findings if isinstance(item, dict)] if isinstance(findings, list) else []
    )


@dataclass(frozen=True)
class FindingCounts:
    findings: int
    by_disposition: dict[str, int] = field(default_factory=dict)

    @property
    def acceptance_rate(self) -> float | None:
        """Accepted / (accepted + rejected): deferred and unset findings are left out."""
        accepted = self.by_disposition.get("accepted", 0)
        decided = accepted + self.by_disposition.get("rejected", 0)
        return accepted / decided if decided else None


def finding_counts(findings: Iterable[tuple[str, str]]) -> FindingCounts:
    dispositions = Counter(item[1] for item in findings)
    return FindingCounts(
        findings=sum(dispositions.values()),
        by_disposition={name: dispositions.get(name, 0) for name in (*DISPOSITIONS, UNSET)},
    )


def by_severity(checks: Sequence[ReviewCheck]) -> dict[str, FindingCounts]:
    findings = [finding for check in checks for finding in check.findings]
    rows = {name: finding_counts(f for f in findings if f[0] == name) for name in SEVERITIES}
    if any(finding[0] == OTHER for finding in findings):
        rows[OTHER] = finding_counts(f for f in findings if f[0] == OTHER)
    rows["all"] = finding_counts(findings)
    return rows


@dataclass(frozen=True)
class TeamMetrics:
    plans: list[PlanCheck]
    without_changes: list[str]
    reviews: list[ReviewCheck]


def collect(directories: Sequence[Path], from_git: ChangedFiles | None = None) -> TeamMetrics:
    plans: list[PlanCheck] = []
    without_changes: list[str] = []
    reviews: list[ReviewCheck] = []
    for directory in directories:
        review = review_check(directory)
        if review is not None:
            reviews.append(review)
        planned = plan_files(read_object(directory / "analysis.json"))
        if not planned:
            continue
        source, changed = changed_files(directory, from_git)
        if not changed:
            without_changes.append(directory.name)
            continue
        plans.append(plan_check(directory.name, source, planned, changed))
    return TeamMetrics(plans, without_changes, reviews)


def as_json(metrics: TeamMetrics) -> Json:
    severity_rows = by_severity(metrics.reviews)
    return {
        "analyst": {
            "tickets": [
                {
                    "ticket": check.ticket,
                    "source": check.source,
                    "planned": list(check.planned),
                    "changed": list(check.changed),
                    "planned_and_changed": list(check.planned_changed),
                    "missed": list(check.missed),
                    "unneeded": list(check.unneeded),
                    "precision": check.precision,
                    "recall": check.recall,
                }
                for check in metrics.plans
            ],
            "totals": asdict(plan_totals(metrics.plans)),
            "without_changed_files": metrics.without_changes,
        },
        "reviewer": {
            "by_severity": {
                name: {**asdict(row), "acceptance_rate": row.acceptance_rate}
                for name, row in severity_rows.items()
            },
            "tickets": [_review_json(check) for check in metrics.reviews],
            "escaped": sum(check.escaped for check in metrics.reviews),
            "escaped_after_review": sum(
                check.escaped for check in metrics.reviews if check.escaped_after_review
            ),
        },
    }


def _review_json(check: ReviewCheck) -> Json:
    counts = finding_counts(check.findings)
    return {
        "ticket": check.ticket,
        "reviewed": check.reviewed,
        "verdict": check.verdict,
        "rounds": check.rounds,
        **asdict(counts),
        "acceptance_rate": counts.acceptance_rate,
        "escaped": check.escaped,
        "reviewer_passed_defect_escaped": check.escaped_after_review,
    }


def markdown(metrics: TeamMetrics) -> str:
    return "\n\n".join([*_analyst(metrics), *_reviewer(metrics)]) + "\n"


def _analyst(metrics: TeamMetrics) -> list[str]:
    sections = ["## Analyst: the plan against the files that changed"]
    if not metrics.plans:
        sections.append("_No ticket has both a plan and known changed files._")
    if metrics.plans:
        rows = [
            "| Ticket | Changed files from | Planned | Changed | Planned and changed | Missed "
            "| Precision | Recall |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for check in metrics.plans:
            rows.append(
                f"| {check.ticket} | {check.source} | {len(check.planned)} | {len(check.changed)} "
                f"| {len(check.planned_changed)} | {len(check.missed)} "
                f"| {_share(check.precision)} | {_share(check.recall)} |"
            )
        totals = plan_totals(metrics.plans)
        rows.append(
            f"| All {totals.tickets} (pooled) | | {totals.planned} | {totals.changed} "
            f"| {totals.planned_changed} | {totals.changed - totals.covered} "
            f"| {_share(totals.precision)} | {_share(totals.recall)} |"
        )
        rows.append(
            f"| Mean per ticket | | | | | | {_share(totals.mean_precision)} "
            f"| {_share(totals.mean_recall)} |"
        )
        sections += [
            "\n".join(rows),
            "Precision: planned files that changed. Recall: changed files the plan named.",
        ]
    if metrics.without_changes:
        sections.append(
            "With a plan but no known changed files (give `--repo` here or to `export-case`): "
            + ", ".join(metrics.without_changes)
        )
    return sections


def _reviewer(metrics: TeamMetrics) -> list[str]:
    sections = ["## Reviewer: findings by severity and disposition"]
    if not metrics.reviews:
        return [*sections, "_No reviews or escaped defects recorded._"]
    columns = "| Findings | Accepted | Rejected | Deferred | Unset | Acceptance rate |"
    rows = [f"| Severity {columns}", "|---|---:|---:|---:|---:|---:|---:|"]
    rows += [f"| {name} {_counts(row)}" for name, row in by_severity(metrics.reviews).items()]
    sections += [
        "\n".join(rows),
        "Acceptance rate: accepted / (accepted + rejected); deferred and unset are left out.",
        "## Reviewer: per ticket",
    ]
    rows = [
        f"| Ticket | Verdict {columns} Escaped defects | |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for check in metrics.reviews:
        rows.append(
            f"| {check.ticket} | {check.verdict or '-'} {_counts(finding_counts(check.findings))}"
            f" {check.escaped} | {_note(check)} |"
        )
    escaped = sum(check.escaped for check in metrics.reviews)
    after_review = sum(check.escaped for check in metrics.reviews if check.escaped_after_review)
    sections += [
        "\n".join(rows),
        f"Escaped defects: {escaped}, of them {after_review} on reviewed tickets.",
    ]
    return sections


def _note(check: ReviewCheck) -> str:
    if check.escaped_after_review:
        return "reviewer passed, defect escaped"
    if not check.reviewed:
        return "no review"
    return ""


def _counts(row: FindingCounts) -> str:
    cells = [row.findings, *(row.by_disposition[name] for name in (*DISPOSITIONS, UNSET))]
    return "| " + " | ".join(str(cell) for cell in cells) + f" | {_share(row.acceptance_rate)} |"


def _share(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"
