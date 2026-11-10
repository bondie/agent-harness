from __future__ import annotations

import hashlib
import subprocess
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
    source: str = "acme-jira",
) -> Ticket:
    return Ticket(
        source=source,
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

SHOP_SOURCES = frozenset({"acme-jira", "acme-hd"})
HELPDESK = [
    ticket("HD-1", "2025-05-01", 3, "2025-05-02", project="HD", source="acme-hd"),
    ticket("HD-2", "2026-01-15", 2.5, "2026-01-16", project="HD", source="acme-hd"),
]
LEGACY = [
    ticket("LEG-1", "2025-02-01", 20, "2025-02-05", project="LEG", source="legacy-jira"),
    ticket("LEG-2", "2026-01-20", 30, "2026-01-25", project="LEG", source="legacy-jira"),
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


@pytest.fixture
def mixed_store(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path / "mixed.db")
    sync(store, FakeEmbedder(), ListConnector(TICKETS), "acme-jira", ChunkingConfig())
    sync(store, FakeEmbedder(), ListConnector(HELPDESK), "acme-hd", ChunkingConfig())
    sync(store, FakeEmbedder(), ListConnector(LEGACY), "legacy-jira", ChunkingConfig())
    yield store
    store.close()


@pytest.fixture
def mixed_searcher(mixed_store: Store) -> Searcher:
    return Searcher(mixed_store, FakeEmbedder())


class GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(self.path), *args],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def commit(self, message: str, files: dict[str, str] | None = None) -> str:
        for name, content in (files or {}).items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GitRepo:
    """A repository whose git ignores the user's global configuration (signing, hooks)."""
    empty = tmp_path / "gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Dev")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "dev@example.com")
    return GitRepo(tmp_path / "repo")
