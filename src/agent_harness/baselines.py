"""Estimators without a language model: the floor an LLM estimator has to beat."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from ticket_precedent.effort import ReferenceClass, quartiles, reference_for_ticket
from ticket_precedent.search import Searcher

from .dataset import Target, finished_hours

MIN_RECENT = 20


@dataclass(frozen=True)
class Estimate:
    median: float
    p25: float | None = None
    p75: float | None = None
    cited: tuple[str, ...] = ()
    cost_usd: float = 0.0
    note: str = ""
    reference_median: float | None = None
    lessons: int | None = None
    lessons_dated: int | None = None
    lessons_undated: int | None = None


class Estimator(Protocol):
    @property
    def name(self) -> str: ...

    def estimate(self, target: Target) -> Estimate | None: ...


def from_hours(values: Sequence[float], cited: Sequence[str] = (), note: str = "") -> Estimate:
    p25, median, p75 = quartiles(values)
    return Estimate(median=median, p25=p25, p75=p75, cited=tuple(cited), note=note)


@dataclass
class NeighbourMedian:
    """Median and P25-P75 of the logged hours of the k most similar finished tickets."""

    searcher: Searcher
    k: int = 10
    since: str | None = None
    name: str = "neighbour-median"

    def estimate(self, target: Target) -> Estimate | None:
        found = self.reference(target)
        if not found.hours:
            return None
        return from_hours(found.hours, self.cited(found))

    def reference(self, target: Target) -> ReferenceClass:
        row = self.searcher.store.tickets_by_id([target.ticket_id])[target.ticket_id]
        return reference_for_ticket(self.searcher, row, self.k, self.since)

    def cited(self, found: ReferenceClass) -> list[str]:
        rows = self.searcher.store.tickets_by_id(hit.ticket_id for hit in found.hits)
        return [rows[hit.ticket_id]["key"] for hit in found.hits]


@dataclass
class EraScaledMedian(NeighbourMedian):
    """Neighbour median with tickets from before a process change scaled to the new pace.

    The factor is the ratio of median logged hours after and before the change, computed
    only from tickets finished before the target, so the backtest does not see the future.
    """

    change: str = "2026-01-01"
    name: str = "era-scaled-median"
    _factors: dict[str, float] = field(default_factory=dict, repr=False)

    def estimate(self, target: Target) -> Estimate | None:
        found = self.reference(target)
        if not found.hours:
            return None
        factor = self.factor(target.created)
        rows = self.searcher.store.tickets_by_id(hit.ticket_id for hit in found.hits)
        scaled = [
            rows[hit.ticket_id]["time_spent_seconds"]
            / 3600
            * (factor if rows[hit.ticket_id]["created"] < self.change else 1.0)
            for hit in found.hits
        ]
        return from_hours(scaled, self.cited(found), note=f"factor {factor:.2f}")

    def factor(self, as_of: str) -> float:
        day = as_of[:10]
        if day not in self._factors:
            self._factors[day] = self._factor(f"{day}T00:00:00+00:00")
        return self._factors[day]

    def _factor(self, as_of: str) -> float:
        store = self.searcher.store
        recent = finished_hours(store, as_of, created_from=self.change)
        older = finished_hours(store, as_of, created_to=self.change)
        if len(recent) < MIN_RECENT or not older:
            return 1.0
        return statistics.median(recent) / statistics.median(older)


@dataclass
class HumanEstimate:
    """The original estimate as it stood when the first time was logged."""

    name: str = "human"

    def estimate(self, target: Target) -> Estimate | None:
        if target.human_hours is None:
            return None
        return Estimate(median=target.human_hours)
