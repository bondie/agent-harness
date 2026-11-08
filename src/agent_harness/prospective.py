"""Prospective evaluation: the estimates given at the gate against the time the work took.

Each eval case is one finished ticket exported by `harness-eval export-case`. Errors are in
hours, signed as estimate minus actual, so a positive bias means the estimates were too high.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .artifacts import Json, read_json
from .estimate_check import number, validate_and_total
from .metrics import Prediction, absolute_errors

PART_KINDS = ("implementation", "verification", "release")
MODES = ("bottom-up", "reference")
ACTUAL_SOURCES = ("measured", "worklog")
EPSILON = 1e-9


def estimate_total(estimate: Json) -> float:
    """The total the estimate check computes, never the model's own number."""
    total, problems = validate_and_total(estimate)
    if total is None:
        raise ValueError(
            "The estimate does not pass the harness-estimate check: " + " ".join(problems)
        )
    return total


def _hours(item: object) -> float:
    value = item.get("hours") if isinstance(item, dict) else None
    if not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"Hours must be a number: {item!r}")
    return float(value)


def hours(text: str) -> float:
    try:
        value = float(text)
    except ValueError as error:
        raise ValueError(f"Not a number of hours: {text!r}") from error
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Hours must be zero or more: {text!r}")
    return value


def actual(total: float | None, parts: Sequence[tuple[str, float]], source: str) -> Json:
    if source not in ACTUAL_SOURCES:
        raise ValueError(f"Source must be one of {', '.join(ACTUAL_SOURCES)}")
    by_kind: dict[str, float] = {}
    for kind, value in parts:
        if kind not in PART_KINDS:
            raise ValueError(f"Part kind must be one of {', '.join(PART_KINDS)}, not {kind!r}")
        if kind in by_kind:
            raise ValueError(f"Actual time for {kind} given twice")
        by_kind[kind] = value
    if total is None and not by_kind:
        raise ValueError("Give the actual time: --actual-total, --actual-part or both")
    return {
        "total": total if total is not None else sum(by_kind.values()),
        "parts": by_kind,
        "source": source,
    }


def stated_total(estimate: Json) -> float | None:
    total = number(estimate.get("total"))
    return total if total is not None and total >= 0 else None


def eval_case(
    key: str, estimate: Json, analysis: Json | None, actual: Json, diff: Json | None
) -> Json:
    """The case of a finished ticket. An estimate the check still rejected when the hook let it
    through after its retries keeps its stated total and the check's errors."""
    case = {
        "ticket": key,
        "estimate": estimate,
        "analysis": analysis,
        "actual": actual,
        "diff": diff,
    }
    total, problems = validate_and_total(estimate)
    if total is not None:
        return case
    if stated_total(estimate) is None:
        raise ValueError(
            "The estimate does not pass the harness-estimate check and states no total: "
            + " ".join(problems)
        )
    return {**case, "check_errors": problems}


def check_failed(case: Json) -> bool:
    return bool(case.get("check_errors"))


def case_total(case: Json) -> float:
    """The code's total, or the stated one of an estimate kept after the check failed."""
    if not check_failed(case):
        return estimate_total(case["estimate"])
    total = stated_total(case["estimate"])
    if total is None:
        raise ValueError(f"The estimate of {case.get('ticket')} states no total")
    return total


def load_cases(directory: Path) -> list[Json]:
    cases = []
    for path in sorted(directory.glob("*.json")):
        case = read_json(path)
        if not isinstance(case, dict) or not isinstance(case.get("estimate"), dict):
            raise ValueError(f"{path} is not an eval case: it has no estimate")
        if not isinstance(case.get("actual"), dict) or not isinstance(
            case["actual"].get("total"), int | float
        ):
            raise ValueError(f"{path} is not an eval case: it has no actual total")
        if check_failed(case) and stated_total(case["estimate"]) is None:
            raise ValueError(f"{path} is not an eval case: its failed estimate states no total")
        cases.append(case)
    if not cases:
        raise ValueError(f"No eval cases in {directory}")
    return cases


@dataclass(frozen=True)
class Errors:
    n: int
    mae: float
    mdae: float
    bias: float
    over: int
    under: int


Pair = tuple[float, float]


def errors(pairs: Sequence[Pair]) -> Errors | None:
    """Errors of (estimate, actual) pairs; None without pairs."""
    if not pairs:
        return None
    predictions = [Prediction(actual=actual, median=estimate) for estimate, actual in pairs]
    absolute = absolute_errors(predictions)
    signed = np.array([estimate - actual for estimate, actual in pairs], dtype=np.float64)
    return Errors(
        n=len(pairs),
        mae=float(absolute.mean()),
        mdae=float(np.median(absolute)),
        bias=float(signed.mean()),
        over=int((signed > EPSILON).sum()),
        under=int((signed < -EPSILON).sum()),
    )


def total_pairs(cases: Iterable[Json]) -> list[Pair]:
    return [(case_total(case), float(case["actual"]["total"])) for case in cases]


def part_pairs(cases: Iterable[Json], kind: str) -> list[Pair]:
    """Per kind, the hours of the estimate's parts of that kind (adjustments have no kind)
    against the actual time of that kind, for cases with both a breakdown and that actual."""
    pairs = []
    for case in cases:
        parts = case["estimate"].get("parts") or []
        measured = case["actual"].get("parts") or {}
        if not parts or kind not in measured:
            continue
        estimated = sum(
            _hours(part) for part in parts if isinstance(part, dict) and part.get("kind") == kind
        )
        pairs.append((estimated, float(measured[kind])))
    return pairs


def by_mode(cases: Iterable[Json]) -> dict[str, list[Json]]:
    grouped: dict[str, list[Json]] = {mode: [] for mode in MODES}
    for case in cases:
        grouped.setdefault(str(case["estimate"].get("mode")), []).append(case)
    return grouped


KEPT = "kept after the check failed"
HEADER = (
    "| {label} | n | MAE h | MdAE h | Bias h | Over | Under |\n|---|---:|---:|---:|---:|---:|---:|"
)


def report(cases: Sequence[Json]) -> str:
    checked = [case for case in cases if not check_failed(case)]
    failed = [case for case in cases if check_failed(case)]
    totals = {"all": errors(total_pairs(checked))}
    totals |= {mode: errors(total_pairs(group)) for mode, group in by_mode(checked).items()}
    totals[KEPT] = errors(total_pairs(failed))
    parts = {kind: errors(part_pairs(checked, kind)) for kind in PART_KINDS}
    sources = sorted({str(case["actual"].get("source")) for case in cases})
    sections = [
        "## Total, by estimate mode",
        _table("Estimates", totals),
        "## By part kind",
        "Hours of the estimate's parts of each kind against the actual time of that kind; "
        "adjustments have no kind and are left out.",
        _table("Part", parts),
        f"Bias is estimate minus actual: positive means too high. {len(cases)} cases, "
        f"{len(failed)} of them kept after the estimate check failed (their stated total, in "
        f"the row {KEPT!r} only), actual time from: {', '.join(sources)}.",
    ]
    return "\n\n".join(sections) + "\n"


def _table(label: str, rows: Mapping[str, Errors | None]) -> str:
    lines = [HEADER.format(label=label)]
    for name, row in rows.items():
        if row is None:
            lines.append(f"| {name} | 0 | - | - | - | - | - |")
            continue
        lines.append(
            f"| {name} | {row.n} | {row.mae:.2f} | {row.mdae:.2f} | {row.bias:+.2f} "
            f"| {row.over} | {row.under} |"
        )
    return "\n".join(lines)
