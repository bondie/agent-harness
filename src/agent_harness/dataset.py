"""Backtest targets: finished tickets with logged time, read from a ticket-precedent index."""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from ticket_precedent.store import Store

DONE_STATUSES = ("Done", "Closed", "Resolved", "Invoiced", "Invoicing", "Production")


@dataclass(frozen=True)
class Target:
    ticket_id: int
    source: str
    key: str
    project: str
    issue_type: str
    title: str
    description: str
    created: str
    actual_hours: float
    human_hours: float | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Target:
        estimate = row["initial_estimate_seconds"]
        return cls(
            ticket_id=row["id"],
            source=row["source"],
            key=row["key"],
            project=row["project"],
            issue_type=row["issue_type"],
            title=row["title"],
            description=row["description"],
            created=row["created"],
            actual_hours=row["time_spent_seconds"] / 3600,
            human_hours=estimate / 3600 if estimate else None,
        )


def load_targets(
    store: Store,
    created_from: str,
    created_to: str,
    max_hours: float = 40.0,
    statuses: Sequence[str] = DONE_STATUSES,
) -> list[Target]:
    """Finished tickets created in [created_from, created_to) with logged time up to max_hours.

    Tickets above max_hours are usually epics or long-running buckets, not estimable tasks.
    """
    placeholders = ",".join("?" * len(statuses))
    rows = store.db.execute(
        f"""
        SELECT * FROM tickets
        WHERE time_spent_seconds > 0 AND time_spent_seconds <= ?
          AND created >= ? AND created < ?
          AND status IN ({placeholders})
        ORDER BY created, key
        """,
        (max_hours * 3600, created_from, created_to, *statuses),
    ).fetchall()
    return [Target.from_row(row) for row in rows]


def sample(targets: Sequence[Target], size: int | None, seed: int = 7) -> list[Target]:
    """A reproducible random subset in creation order, for the variants that cost money."""
    if size is None or size >= len(targets):
        return list(targets)
    picked = random.Random(seed).sample(list(targets), size)  # noqa: S311
    return sorted(picked, key=lambda target: (target.created, target.key))


def finished_hours(
    store: Store,
    as_of: str,
    created_from: str | None = None,
    created_to: str | None = None,
) -> list[float]:
    """Logged hours of every ticket whose time was final before `as_of`."""
    rows = store.db.execute(
        """
        SELECT time_spent_seconds FROM tickets
        WHERE time_spent_seconds > 0
          AND COALESCE(last_logged, updated) < :as_of
          AND (:created_from IS NULL OR created >= :created_from)
          AND (:created_to IS NULL OR created < :created_to)
        """,
        {"as_of": as_of, "created_from": created_from, "created_to": created_to},
    )
    return [row["time_spent_seconds"] / 3600 for row in rows]
