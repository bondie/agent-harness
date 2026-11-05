"""Run estimators over backtest targets, one JSON line per prediction, resumable."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from .baselines import Estimator
from .dataset import Target
from .llm import LLMError

RECOVERABLE = (LLMError, httpx.HTTPError, subprocess.TimeoutExpired)


@dataclass(frozen=True)
class Record:
    variant: str
    id: str
    project: str
    issue_type: str
    created: str
    actual: float
    human: float | None
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    cited: tuple[str, ...] = ()
    cost_usd: float = 0.0
    seconds: float = 0.0
    note: str = ""
    error: str = ""

    @classmethod
    def load(cls, line: str) -> Record:
        data: dict[str, Any] = json.loads(line)
        data["cited"] = tuple(data.get("cited") or ())
        return cls(**data)


def target_id(target: Target) -> str:
    return f"{target.source}/{target.key}"


def run_file(directory: Path, variant: str) -> Path:
    return directory / f"{re.sub(r'[^A-Za-z0-9.@-]+', '_', variant)}.jsonl"


def run(
    estimators: Sequence[Estimator],
    targets: Sequence[Target],
    directory: Path,
    progress: Callable[[str, int, int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for estimator in estimators:
        path = run_file(directory, estimator.name)
        done = {record.id for record in load_records(path) if not record.error}
        todo = [target for target in targets if target_id(target) not in done]
        with path.open("a") as out:
            for position, target in enumerate(todo, start=1):
                out.write(json.dumps(asdict(predict(estimator, target, clock))) + "\n")
                out.flush()
                if progress:
                    progress(estimator.name, position, len(todo))


def predict(estimator: Estimator, target: Target, clock: Callable[[], float]) -> Record:
    base = Record(
        variant=estimator.name,
        id=target_id(target),
        project=target.project,
        issue_type=target.issue_type,
        created=target.created,
        actual=target.actual_hours,
        human=target.human_hours,
    )
    started = clock()
    try:
        estimate = estimator.estimate(target)
    except RECOVERABLE as error:
        return _replace(base, seconds=clock() - started, error=str(error)[:500])
    seconds = clock() - started
    if estimate is None:
        return _replace(base, seconds=seconds, error="no estimate")
    return _replace(
        base,
        median=estimate.median,
        p25=estimate.p25,
        p75=estimate.p75,
        cited=estimate.cited,
        cost_usd=estimate.cost_usd,
        seconds=seconds,
        note=estimate.note,
    )


def _replace(record: Record, **changes: Any) -> Record:
    return Record(**{**asdict(record), **changes})


def load_records(path: Path) -> list[Record]:
    """The latest record per ticket: a retried error is superseded by its success."""
    if not path.exists():
        return []
    latest: dict[str, Record] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            record = Record.load(line)
            latest[record.id] = record
    return list(latest.values())


def load_run(directory: Path) -> dict[str, list[Record]]:
    runs: dict[str, list[Record]] = {}
    for path in sorted(directory.glob("*.jsonl")):
        records = load_records(path)
        if records:
            runs[records[0].variant] = records
    return runs
