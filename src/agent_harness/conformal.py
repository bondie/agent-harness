"""Conformal calibration of the estimator's P25-P75 range and of a one-sided P90 bound.

Asymmetric split-conformal in log space (Romano et al. 2019, asymmetric variant). For a
central range of nominal `coverage`, each tail may hold (1 - coverage) / 2 of the actual
times, so each side takes the conformal quantile at level (1 + coverage) / 2 of its score:

- `s_lo = log(p25) - log(actual)`, `s_hi = log(actual) - log(p75)`;
- `p25' = p25 * exp(-q_lo)`, `p75' = p75 * exp(q_hi)`.

The P90 bound takes the 0.90 quantile of `s90 = log(actual) - log(base)`, with base the P75 or
the median, and `p90' = base * exp(q90)`. A conformal quantile at level `l` of `n` scores is
their `ceil(l * (n + 1))`-th smallest; it needs at least that many scores.

The evaluation is time-ordered: a ticket is calibrated only on tickets whose time was all
logged before it was created (`COALESCE(last_logged, updated) < created`, as in
`dataset.finished_hours`), the last `window` of them by that finish time.
"""

from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from .memory import ESTIMATOR, agent_memory_root, memory_dir_name
from .runner import Record

FLOOR_HOURS = 0.05
"""A P25 or P75 of zero or less has no logarithm; it counts as three minutes."""
MINIMUM = 30
WINDOWS: tuple[int | None, ...] = (50, 100, 200, 400, None)
P90 = 0.9
P90_MINIMUM = 50
"""Five tickets in the 10% tail; fewer gives a bound that jumps with every new ticket."""
P90_TOLERANCE = 0.025
TOLERANCE = 1e-9
Base = Literal["p75", "median"]
BASES: tuple[Base, ...] = ("p75", "median")
P90_BASE: Base = "p75"
"""Stable from window 50 with even halves in the backtest; the median base is barely so."""
BELOW, INSIDE, ABOVE = -1, 0, 1
CALIBRATION_FILE = "interval-calibration.json"


def default_path() -> Path:
    """The estimator reads its calibration from its own memory directory."""
    return agent_memory_root() / memory_dir_name(ESTIMATOR) / CALIBRATION_FILE


@dataclass(frozen=True)
class Pair:
    """A past prediction with its outcome; `finished` is None when the index has no row."""

    id: str
    created: str
    finished: str | None
    actual: float
    median: float
    p25: float
    p75: float


@dataclass(frozen=True)
class Calibration:
    lower_factor: float
    upper_factor: float
    n: int
    coverage: float
    finished_from: str
    finished_to: str
    p90_factor: float | None = None
    p90_base: Base = P90_BASE


@dataclass(frozen=True)
class Calibrated:
    p25: float
    p75: float
    p90: float | None


def floored(hours: float) -> float:
    return max(hours, FLOOR_HOURS)


def tail_level(coverage: float) -> float:
    return (1 + coverage) / 2


def rank(level: float, n: int) -> int:
    """1-based order statistic of the conformal quantile; the epsilon absorbs float error."""
    return math.ceil(level * (n + 1) - TOLERANCE)


def needed(level: float) -> int:
    """The fewest scores whose conformal quantile at `level` is finite."""
    n = 1
    while rank(level, n) > n:
        n += 1
    return n


def conformal_quantile(scores: Sequence[float], level: float) -> float:
    position = rank(level, len(scores))
    if position > len(scores):
        raise ValueError(
            f"{len(scores)} calibration tickets are too few for the {level:.0%} quantile: "
            f"it needs at least {needed(level)}"
        )
    return sorted(scores)[position - 1]


def lower_score(pair: Pair) -> float:
    return math.log(floored(pair.p25)) - math.log(pair.actual)


def upper_score(pair: Pair) -> float:
    return math.log(pair.actual) - math.log(floored(pair.p75))


def base_of(median: float, p75: float, base: Base) -> float:
    return floored(p75 if base == "p75" else median)


def p90_score(pair: Pair, base: Base) -> float:
    return math.log(pair.actual) - math.log(base_of(pair.median, pair.p75, base))


def fit(
    pairs: Sequence[Pair],
    coverage: float = 0.5,
    p90_base: Base = P90_BASE,
    p90_minimum: int | None = None,
) -> Calibration:
    """Factors from `pairs`; the P90 factor only from at least `p90_minimum` of them."""
    if not 0 < coverage < 1:
        raise ValueError(f"Coverage must be between 0 and 1, got {coverage}")
    if not pairs:
        raise ValueError("Nothing to calibrate on")
    level = tail_level(coverage)
    q_lo = conformal_quantile([lower_score(pair) for pair in pairs], level)
    q_hi = conformal_quantile([upper_score(pair) for pair in pairs], level)
    finished = sorted(pair.finished for pair in pairs if pair.finished is not None)
    p90_factor = None
    if len(pairs) >= max(p90_minimum or 0, needed(P90)):
        q90 = conformal_quantile([p90_score(pair, p90_base) for pair in pairs], P90)
        p90_factor = math.exp(q90)
    return Calibration(
        lower_factor=math.exp(-q_lo),
        upper_factor=math.exp(q_hi),
        n=len(pairs),
        coverage=coverage,
        finished_from=finished[0] if finished else "",
        finished_to=finished[-1] if finished else "",
        p90_factor=p90_factor,
        p90_base=p90_base,
    )


def apply(p25: float, median: float, p75: float, calibration: Calibration) -> Calibrated:
    """The calibrated range, kept around the unchanged median; P90 never below P75'."""
    low = min(floored(p25) * calibration.lower_factor, median)
    high = max(floored(p75) * calibration.upper_factor, median)
    if calibration.p90_factor is None:
        return Calibrated(low, high, None)
    base = base_of(median, p75, calibration.p90_base)
    return Calibrated(low, high, max(base * calibration.p90_factor, high))


def position(actual: float, low: float, high: float) -> int:
    """Below, inside or above an inclusive range; a boundary within float error is inside."""
    if actual < low and not math.isclose(actual, low, rel_tol=TOLERANCE):
        return BELOW
    if actual > high and not math.isclose(actual, high, rel_tol=TOLERANCE):
        return ABOVE
    return INSIDE


@dataclass(frozen=True)
class Outcome:
    id: str
    created: str
    actual: float
    median: float
    before: Calibrated
    after: Calibrated


@dataclass(frozen=True)
class Shares:
    n: int
    below: float
    inside: float
    above: float
    relative_width: float
    width: float


@dataclass(frozen=True)
class Bound:
    n: int
    above: float
    relative_height: float
    height: float


@dataclass(frozen=True)
class Evaluation:
    window: int | None
    coverage: float
    p90_base: Base
    outcomes: tuple[Outcome, ...]
    skipped: int
    latest: Calibration | None

    def shares(self, after: bool) -> Shares:
        return shares(self.outcomes, after)

    def bound(self, after: bool) -> Bound | None:
        return bound(self.outcomes, after)


def shares(outcomes: Sequence[Outcome], after: bool) -> Shares:
    if not outcomes:
        return Shares(0, math.nan, math.nan, math.nan, math.nan, math.nan)
    ranges = [outcome.after if after else outcome.before for outcome in outcomes]
    places = [
        position(outcome.actual, r.p25, r.p75) for outcome, r in zip(outcomes, ranges, strict=True)
    ]
    return Shares(
        n=len(outcomes),
        below=places.count(BELOW) / len(places),
        inside=places.count(INSIDE) / len(places),
        above=places.count(ABOVE) / len(places),
        relative_width=float(
            np.median([(r.p75 - r.p25) / o.median for o, r in zip(outcomes, ranges, strict=True)])
        ),
        width=float(np.median([r.p75 - r.p25 for r in ranges])),
    )


def bound(outcomes: Sequence[Outcome], after: bool) -> Bound | None:
    """Share of actual times above the P90 bound, and its median height; before calibration
    the bound is the uncalibrated base."""
    pairs = [
        (outcome, (outcome.after if after else outcome.before).p90)
        for outcome in outcomes
        if outcome.after.p90 is not None
    ]
    known = [(outcome, value) for outcome, value in pairs if value is not None]
    if not known:
        return None
    above = [position(o.actual, 0.0, value) == ABOVE for o, value in known]
    return Bound(
        n=len(known),
        above=sum(above) / len(known),
        relative_height=float(np.median([value / o.median for o, value in known])),
        height=float(np.median([value for _, value in known])),
    )


def rolling(
    pairs: Sequence[Pair],
    window: int | None,
    coverage: float = 0.5,
    minimum: int = MINIMUM,
    p90_base: Base = P90_BASE,
    fit_on: Sequence[Pair] | None = None,
) -> Evaluation:
    """Calibrate each ticket on the last `window` tickets finished before it was created.

    The calibration tickets come from `pairs` themselves, or from `fit_on`: another
    estimator's predictions, whose factors are then applied to these ranges. A ticket with
    fewer than `minimum` calibration tickets is skipped, and gets no P90 below `P90_MINIMUM`.
    The rules do not depend on the window, so every window scores the same tickets.
    """
    smallest = max(minimum, needed(tail_level(coverage)))
    if window is not None and window < smallest:
        raise ValueError(f"A window of {window} is below the minimum of {smallest} tickets")
    pool = sorted(
        (pair for pair in (pairs if fit_on is None else fit_on) if pair.finished is not None),
        key=lambda pair: (pair.finished or "", pair.id),
    )
    ends = [pair.finished or "" for pair in pool]
    outcomes: list[Outcome] = []
    skipped = 0
    for target in sorted(pairs, key=lambda pair: (pair.created, pair.id)):
        end = bisect_left(ends, target.created)
        available = [pair for pair in pool[:end] if pair.id != target.id]
        if len(available) < smallest:
            skipped += 1
            continue
        used = available[-window:] if window else available
        calibration = fit(used, coverage, p90_base, P90_MINIMUM)
        base = base_of(target.median, target.p75, p90_base)
        outcomes.append(
            Outcome(
                id=target.id,
                created=target.created,
                actual=target.actual,
                median=target.median,
                before=Calibrated(target.p25, target.p75, base),
                after=apply(target.p25, target.median, target.p75, calibration),
            )
        )
    latest = None
    if len(pool) >= smallest:
        latest = fit(pool[-window:] if window else pool, coverage, p90_base, P90_MINIMUM)
    return Evaluation(window, coverage, p90_base, tuple(outcomes), skipped, latest)


def recommend(evaluations: Sequence[Evaluation]) -> Evaluation:
    """Closest to nominal coverage in whole percent, as printed; then the narrower range."""
    return min(
        evaluations,
        key=lambda e: (
            round(abs(e.shares(after=True).inside - e.coverage) * 100),
            e.shares(after=True).relative_width,
        ),
    )


def p90_stable(evaluation: Evaluation, tolerance: float = P90_TOLERANCE) -> bool:
    """At most `tolerance` from 10% above P90', overall and in each half of the scored
    tickets in time order, so that a lucky average over a drift does not pass."""
    outcomes = evaluation.outcomes
    half = len(outcomes) // 2
    parts = [outcomes, outcomes[:half], outcomes[half:]]
    for part in parts:
        found = bound(part, after=True)
        if found is None or abs(found.above - (1 - P90)) > tolerance:
            return False
    return True


def size(window: int | None) -> float:
    return math.inf if window is None else window


def smallest_stable(evaluations: Iterable[Evaluation]) -> Evaluation | None:
    """The evaluation with the smallest window whose P90 coverage is stable, if any."""
    stable = [evaluation for evaluation in evaluations if p90_stable(evaluation)]
    if not stable:
        return None
    return min(stable, key=lambda evaluation: size(evaluation.window))


def wilson(share: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval of a share observed on `n` tickets."""
    if n == 0:
        return math.nan, math.nan
    centre = (share + z * z / (2 * n)) / (1 + z * z / n)
    spread = z * math.sqrt(share * (1 - share) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - spread, centre + spread


@dataclass(frozen=True)
class Finishes:
    at: dict[str, str]
    from_updated: frozenset[str]


def open_index(path: Path) -> sqlite3.Connection:
    """Read-only: the live index may be syncing, and `Store` would write its schema."""
    return sqlite3.connect(f"{path.expanduser().resolve().as_uri()}?mode=ro", uri=True)


def finish_times(db: sqlite3.Connection, ids: Iterable[str]) -> Finishes:
    """When each `source/key` had all its time logged, as `dataset.finished_hours` reads it."""
    wanted = sorted(set(ids))
    rows = db.execute(
        """
        SELECT source || '/' || key, COALESCE(last_logged, updated), last_logged IS NULL
        FROM tickets
        WHERE time_spent_seconds > 0
          AND source || '/' || key IN (SELECT value FROM json_each(?))
        """,
        (json.dumps(wanted),),
    ).fetchall()
    return Finishes(
        at={row[0]: row[1] for row in rows if row[1]},
        from_updated=frozenset(row[0] for row in rows if row[1] and row[2]),
    )


@dataclass(frozen=True)
class Prepared:
    pairs: list[Pair]
    records: int
    without_interval: int
    without_finish: int
    from_updated: int


def prepare(records: Sequence[Record], finishes: Finishes) -> Prepared:
    """Pairs from the records with a usable range; a record missing from the index stays a
    target but is never used to calibrate another."""
    usable = [record for record in records if _usable(record)]
    pairs = [
        Pair(
            id=record.id,
            created=record.created,
            finished=finishes.at.get(record.id),
            actual=record.actual,
            median=record.median or 0.0,
            p25=record.p25 or 0.0,
            p75=record.p75 or 0.0,
        )
        for record in usable
    ]
    return Prepared(
        pairs=pairs,
        records=len(records),
        without_interval=len(records) - len(usable),
        without_finish=sum(1 for pair in pairs if pair.finished is None),
        from_updated=sum(1 for pair in pairs if pair.id in finishes.from_updated),
    )


def _usable(record: Record) -> bool:
    if record.error or record.median is None or record.p25 is None or record.p75 is None:
        return False
    return record.actual > 0 and record.median > 0 and record.p25 <= record.median <= record.p75


def as_json(calibration: Calibration, window: int | None) -> dict[str, object]:
    return {
        "lower_factor": round(calibration.lower_factor, 4),
        "upper_factor": round(calibration.upper_factor, 4),
        "p90_factor": None if calibration.p90_factor is None else round(calibration.p90_factor, 4),
        "p90_base": calibration.p90_base,
        "n": calibration.n,
        "window": window if window is not None else "all",
        "coverage": calibration.coverage,
        "finished_from": calibration.finished_from,
        "finished_to": calibration.finished_to,
    }


def measured(evaluation: Evaluation) -> dict[str, object]:
    after = evaluation.shares(after=True)
    found = evaluation.bound(after=True)
    return {
        "tickets": after.n,
        "skipped": evaluation.skipped,
        "below": round(after.below, 4),
        "inside": round(after.inside, 4),
        "above": round(after.above, 4),
        "relative_width": round(after.relative_width, 4),
        "p90_above": None if found is None else round(found.above, 4),
        "p90_stable": p90_stable(evaluation),
    }
