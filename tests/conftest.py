from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ticket_precedent.chunking import ChunkingConfig
from ticket_precedent.embeddings import normalize
from ticket_precedent.ingest import sync
from ticket_precedent.models import Ticket
from ticket_precedent.search import Searcher
from ticket_precedent.store import Store
from ticket_precedent.text import tokens

EXPORT = "The nightly price export to the marketplace times out on large catalogues."


class FakeEmbedder:
    """Hashed bag of words: deterministic and offline."""

    name = "fake:bow"
    dimension = 64

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in tokens(text):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            vector[int.from_bytes(digest) % self.dimension] += 1.0
        return normalize(vector)


def at(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def ticket(
    key: str,
    created: str,
    hours: float | None,
    logged: str | None = None,
    estimate: float | None = None,
    title: str = "Price export times out",
    description: str = EXPORT,
    status: str = "Done",
    project: str = "SHOP",
) -> Ticket:
    return Ticket(
        source="acme-jira",
        key=key,
        project=project,
        title=title,
        description=description,
        url=f"https://tracker.example/{key}",
        status=status,
        created=at(created),
        updated=at(logged or created),
        time_spent_seconds=int(hours * 3600) if hours else None,
        issue_type="Bug",
        initial_estimate_seconds=int(estimate * 3600) if estimate else None,
        last_logged=at(logged) if logged else None,
    )


TICKETS = [
    ticket("SHOP-1", "2025-01-10", 4, "2025-01-12", estimate=3),
    ticket("SHOP-2", "2025-03-10", 6, "2025-03-15", estimate=6),
    ticket("SHOP-3", "2025-06-01", 2, "2025-06-02"),
    ticket("SHOP-4", "2026-01-05", 1, "2026-01-06", estimate=1),
    ticket("SHOP-5", "2026-02-01", 2, "2026-02-03", estimate=1.5),
    ticket("SHOP-6", "2026-03-01", 1.5, "2026-03-02", estimate=1.5),
    ticket("SHOP-7", "2026-03-05", 3, "2026-03-06", status="In Progress"),
    ticket("SHOP-8", "2026-04-01", 60, "2026-04-20"),
    ticket("OPS-1", "2026-02-10", 0.5, "2026-02-11", project="OPS", title="Restart the queue"),
]


@dataclass
class ListConnector:
    tickets: Sequence[Ticket]

    def iter_tickets(self, since: datetime | None = None) -> Iterator[Ticket]:
        return iter(self.tickets)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path / "index.db")
    sync(store, FakeEmbedder(), ListConnector(TICKETS), "acme-jira", ChunkingConfig())
    yield store
    store.close()


@pytest.fixture
def searcher(store: Store) -> Searcher:
    return Searcher(store, FakeEmbedder())
